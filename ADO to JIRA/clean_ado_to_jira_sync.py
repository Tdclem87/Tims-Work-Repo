import csv
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from html.parser import HTMLParser
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from urllib.parse import unquote, urlparse

import requests
from azure.devops.connection import Connection
from msrest.authentication import BasicAuthentication
from requests_toolbelt.multipart.encoder import MultipartEncoder


CONFIG_PATH = Path(__file__).resolve().parent / "config.json"
LOG_DIR = Path(__file__).resolve().parent / "logs"
STATE_PATH = Path(__file__).resolve().parent / "run_state.json"
DEFAULT_MAX_ATTACHMENT_SIZE_MB = 100
ADO_CONFIGS = {}
JIRA_BASE_URL = ""
JIRA_EMAIL = ""
MAX_ATTACHMENT_BYTES = DEFAULT_MAX_ATTACHMENT_SIZE_MB * 1024 * 1024
SESSION_ID = uuid.uuid4().hex[:12]
SESSION = requests.Session()
RUN_STATE = {}
LOGGER = logging.getLogger("ado_to_jira_sync")


class SessionLogFilter(logging.Filter):
    def filter(self, record):
        record.session_id = SESSION_ID
        return True


class ConsoleVisibilityFilter(logging.Filter):
    def filter(self, record):
        return getattr(record, "show_console", True)


def update_run_state(status, **updates):
    RUN_STATE.update(
        {
            "status": status,
            "session_id": SESSION_ID,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            **updates,
        }
    )
    temporary_path = STATE_PATH.with_suffix(".tmp")
    temporary_path.write_text(json.dumps(RUN_STATE, indent=2), encoding="utf-8")
    temporary_path.replace(STATE_PATH)


def configure_logging():
    LOG_DIR.mkdir(exist_ok=True)
    if LOGGER.handlers:
        return

    handler = TimedRotatingFileHandler(
        LOG_DIR / "activity.log",
        when="midnight",
        interval=1,
        backupCount=89,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(levelname)s] [session=%(session_id)s] %(message)s"
        )
    )
    handler.addFilter(SessionLogFilter())
    LOGGER.addHandler(handler)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    console_handler.addFilter(ConsoleVisibilityFilter())
    LOGGER.addHandler(console_handler)
    LOGGER.setLevel(logging.INFO)
    LOGGER.propagate = False
    LOGGER.info("Logging started.")


class CommentTextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)

    def handle_starttag(self, tag, attrs):
        if tag in {"br", "p", "div", "li"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"p", "div", "li"}:
            self.parts.append("\n")

    def plain_text(self):
        text = "".join(self.parts)
        lines = (line.strip() for line in text.splitlines())
        return "\n".join(line for line in lines if line)


class AttachmentLinkParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.urls = {}

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "img" and attributes.get("src"):
            self.urls[attributes["src"]] = "screenshot.png"
        elif tag == "a" and attributes.get("href"):
            self.urls.setdefault(attributes["href"], None)


def remove_html_formatting(comment_text):
    parser = CommentTextParser()
    parser.feed(comment_text or "")
    parser.close()
    return parser.plain_text()


def get_comment_attachment_urls(comment_text):
    parser = AttachmentLinkParser()
    parser.feed(comment_text or "")
    parser.close()
    return [
        {"url": url, "name": name}
        for url, name in parser.urls.items()
        if urlparse(url).scheme in {"http", "https"}
        and "/attachments/" in urlparse(url).path.lower()
    ]


def get_attachment_filename(attachment):
    if attachment.get("name"):
        return attachment["name"]
    filename = unquote(os.path.basename(urlparse(attachment["url"]).path))
    if re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        filename,
        re.IGNORECASE,
    ):
        return "attachment.bin"
    return filename or "attachment.bin"


def format_ado_timestamp(timestamp):
    if not timestamp:
        return "Unknown"
    try:
        parsed_timestamp = datetime.fromisoformat(
            timestamp.replace("Z", "+00:00")
        )
        return parsed_timestamp.strftime("%Y-%m-%d %H:%M:%S %Z")
    except ValueError:
        return timestamp


def comment_timestamp_sort_key(comment):
    timestamp = comment["CommentCreatedDate"]
    if not timestamp:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        parsed_timestamp = datetime.fromisoformat(
            timestamp.replace("Z", "+00:00")
        )
        if parsed_timestamp.tzinfo is None:
            parsed_timestamp = parsed_timestamp.replace(tzinfo=timezone.utc)
        return parsed_timestamp.astimezone(timezone.utc)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


def get_comment_author(comment):
    created_by = comment.get("createdBy", {})
    return (
        created_by.get("displayName")
        or created_by.get("uniqueName")
        or created_by.get("id")
        or "Unknown"
    )


def get_required_input(prompt):
    value = input(prompt).strip()
    if not value:
        raise ValueError(f"{prompt.rstrip(': ')} is required.")
    return value


def get_required_environment_variable(name):
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"Required environment variable '{name}' is not set."
        )
    return value


def load_configuration():
    global ADO_CONFIGS, JIRA_BASE_URL, JIRA_EMAIL, MAX_ATTACHMENT_BYTES

    if not CONFIG_PATH.exists():
        raise RuntimeError(
            f"Configuration file not found: {CONFIG_PATH}. "
            "Copy config.example.json to config.json and update it."
        )

    try:
        configuration = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise RuntimeError(
            f"Configuration file is not valid JSON: {CONFIG_PATH}"
        ) from error

    try:
        ADO_CONFIGS = configuration["ado_configs"]
        jira_configuration = configuration["jira"]
        JIRA_BASE_URL = jira_configuration["base_url"].rstrip("/")
        JIRA_EMAIL = jira_configuration["email"]
        max_attachment_size_mb = configuration.get(
            "max_attachment_size_mb", DEFAULT_MAX_ATTACHMENT_SIZE_MB
        )
        if not isinstance(max_attachment_size_mb, (int, float)) or max_attachment_size_mb <= 0:
            raise ValueError("max_attachment_size_mb must be greater than 0.")
        MAX_ATTACHMENT_BYTES = int(max_attachment_size_mb * 1024 * 1024)
    except (KeyError, TypeError, AttributeError) as error:
        raise RuntimeError(
            "Configuration must include ado_configs and jira settings."
        ) from error
    except ValueError as error:
        raise RuntimeError(str(error)) from error
    LOGGER.info("Loaded configuration for %d Azure DevOps organization(s).", len(ADO_CONFIGS))


def view_readme():
    readme_path = Path(__file__).resolve().parent.parent / "README.md"
    if not readme_path.exists():
        print(f"README not found: {readme_path}")
        return

    print("\n" + readme_path.read_text(encoding="utf-8"))
    input("Press Enter to continue...")


def get_jira_issue_key():
    while True:
        valid_prefixes = " / ".join(
            sorted(prefix.upper() for prefix in ADO_CONFIGS.keys())
        )
        value = get_required_input(
            f"Jira issue key ({valid_prefixes}, or R to view README): "
        )
        if value.lower() == "r":
            view_readme()
            continue
        return value


def launch_windows_environment_variables():
    if os.name != "nt":
        print("Windows environment variables can only be opened on Windows.")
        return

    try:
        subprocess.run(
            ["rundll32.exe", "sysdm.cpl,EditEnvironmentVariables"],
            check=False,
        )
        print("Opened the Windows Environment Variables window.")
    except OSError as error:
        print(f"Could not open the environment variables editor: {error}")


def prompt_for_configuration():
    LOGGER.info("Initial configuration setup started.")
    print("\nInitial setup is required.")
    print("This will create a local config file and save it next to this script.")
    print("Your PATs and Jira token will stay in Windows environment variables.")

    while True:
        try:
            org_count = int(
                get_required_input(
                    "How many Azure DevOps organizations do you want to configure? "
                )
            )
            if org_count <= 0:
                raise ValueError("Organization count must be at least 1.")
            break
        except ValueError:
            print("Please enter a whole number greater than 0.")

    ado_configs = {}
    for index in range(1, org_count + 1):
        while True:
            prefix = get_required_input(
                f"Organization {index} Jira prefix (for example MBSD): "
            ).strip().upper()
            if prefix:
                break
        while True:
            organization_url = get_required_input(
                f"Organization {index} Azure DevOps URL (for example https://dev.azure.com/your-org): "
            )
            if organization_url:
                break
        ado_configs[prefix] = {"organization_url": organization_url}

    jira_base_url = get_required_input(
        "Jira base URL (for example https://your-company.atlassian.net): "
    )
    jira_email = get_required_input("Jira email address: ")

    configuration = {
        "ado_configs": ado_configs,
        "jira": {
            "base_url": jira_base_url,
            "email": jira_email,
        },
    }
    CONFIG_PATH.write_text(json.dumps(configuration, indent=2), encoding="utf-8")
    LOGGER.info("Initial configuration saved.")
    print(f"Configuration saved to: {CONFIG_PATH}")


def ensure_environment_variables():
    required_vars = {"JIRA_API_TOKEN": "Jira API token"}
    for prefix in ADO_CONFIGS.keys():
        required_vars[f"AZDO_{prefix.upper()}_PAT"] = f"{prefix} Azure DevOps PAT"

    missing = [name for name in required_vars if not os.environ.get(name)]
    if not missing:
        LOGGER.info("All required environment variables are available.")
        return

    LOGGER.warning("Missing %d required environment variable(s).", len(missing))
    print("\nMissing environment variables:")
    for name in missing:
        print(f"- {name} ({required_vars[name]})")

    print("\nCreate the required tokens if you do not already have them:")
    print("Jira API token:")
    print("1. Open https://id.atlassian.com/manage-profile/security/api-tokens")
    print("2. Select Create API token, enter a label, and create the token.")
    print("3. Copy it immediately; Atlassian will not show it again.")
    print("4. Store it as JIRA_API_TOKEN in Windows environment variables.")
    print("Azure DevOps PAT, once for each organization:")
    print("1. Sign in to the Azure DevOps organization.")
    print("2. Select your profile icon, then User settings > Personal access tokens.")
    print("3. Select New Token, enter a name and expiration date.")
    print("4. Choose the organization and use the minimum Work Items read access needed by this tool.")
    print("5. Create the PAT and copy it immediately; Azure DevOps will not show it again.")
    print("6. Store it as AZDO_<PREFIX>_PAT, using each configured prefix.")

    print("\nAdd the missing values in Windows:")
    print("1. Press the Windows key and search for 'environment variables'.")
    print("2. Open 'Edit the system environment variables'.")
    print("3. Select 'Environment Variables'.")
    print("4. Under User variables, select 'New'.")
    print("5. Enter each missing variable name exactly as listed above.")
    print("6. Enter the matching Jira API token or Azure DevOps PAT as its value.")
    print("7. Select OK on each window to save the changes.")
    print("8. Close and reopen PowerShell, Command Prompt, or VS Code.")

    response = input(
        "Open Windows Environment Variables now to add them? (y/n): "
    ).strip().lower()
    if response in {"y", "yes"}:
        launch_windows_environment_variables()

    input("After you add the missing values, press Enter to continue...")


def ensure_setup():
    if not CONFIG_PATH.exists():
        LOGGER.info("Configuration file is missing; starting guided setup.")
        prompt_for_configuration()
        load_configuration()
        return

    try:
        load_configuration()
    except RuntimeError as error:
        LOGGER.error("Configuration check failed: %s", error)
        print(f"Configuration check failed: {error}")
        prompt_for_configuration()
        load_configuration()


def get_work_item_comments(work_item_tracking_client, work_item_id, pat, work_item=None):
    work_item = work_item or work_item_tracking_client.get_work_item(work_item_id)
    links = work_item._links.additional_properties
    comments_link = links.get("workItemComments", {}).get("href")

    if not comments_link:
        return []

    comments = []
    continuation_token = None
    while True:
        params = {"$top": 100}
        if continuation_token:
            params["continuationToken"] = continuation_token
        response = SESSION.get(
            comments_link,
            params=params,
            auth=("", pat),
            timeout=30,
        )
        response.raise_for_status()
        comments.extend(response.json().get("comments", []))
        continuation_token = response.headers.get("x-ms-continuationtoken")
        if not continuation_token:
            return comments


def get_work_item_status(work_item, work_item_id):
    fields = getattr(work_item, "fields", {}) or {}
    changed_by = fields.get("System.ChangedBy") or "Unknown"
    if isinstance(changed_by, dict):
        changed_by = (
            changed_by.get("displayName")
            or changed_by.get("uniqueName")
            or changed_by.get("id")
            or "Unknown"
        )
    return {
        "work_item_id": work_item_id,
        "state": fields.get("System.State") or "Unknown",
        "changed_by": changed_by,
        "changed_date": fields.get("System.ChangedDate") or "Unknown",
    }


def get_work_item_attachments(organization_url, work_item_id, pat):
    response = SESSION.get(
        f"{organization_url}/_apis/wit/workitems/{work_item_id}",
        params={"$expand": "relations", "api-version": "7.1"},
        auth=("", pat),
        timeout=30,
    )
    response.raise_for_status()
    relations = response.json().get("relations", [])
    return [
        {
            "url": relation["url"],
            "name": relation.get("attributes", {}).get("name")
            or "attachment",
            "work_item_id": work_item_id,
        }
        for relation in relations
        if relation.get("rel") == "AttachedFile" and relation.get("url")
    ]


def get_jira_issue_data(issue_key, jira_api_token):
    response = SESSION.get(
        f"{JIRA_BASE_URL}/rest/api/3/issue/{issue_key}",
        params={"fields": "summary,attachment,comment"},
        auth=(JIRA_EMAIL, jira_api_token),
        headers={"Accept": "application/json"},
        timeout=30,
    )
    response.raise_for_status()
    fields = response.json().get("fields", {})
    return (
        fields.get("summary", ""),
        fields.get("attachment", []),
        fields.get("comment", {}).get("comments", []),
    )


def extract_ado_work_item_ids(summary):
    match = re.search(r"(?<!\d)\d{4,}(?!\d)", summary or "")
    if not match:
        raise ValueError(
            "No ADO work item number was found in the Jira issue summary."
        )
    return [match.group(0)]


def upload_jira_attachment(
    issue_key, attachment, pat, jira_api_token, existing_filenames
):
    filename = get_attachment_filename(attachment)
    if filename in existing_filenames:
        return None, f"Skipped existing Jira attachment: {filename}", None

    temporary_path = None
    try:
        with SESSION.get(
            attachment["url"],
            auth=("", pat),
            stream=True,
            timeout=30,
        ) as response:
            response.raise_for_status()
            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > MAX_ATTACHMENT_BYTES:
                message = (
                    f"Stored link for oversized attachment: {filename} "
                    f"(limit {MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB)"
                )
                return None, message, {
                    "filename": filename,
                    "url": attachment["url"],
                }

            with tempfile.NamedTemporaryFile(delete=False) as temporary_file:
                temporary_path = temporary_file.name
                total_bytes = 0
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    total_bytes += len(chunk)
                    if total_bytes > MAX_ATTACHMENT_BYTES:
                        message = (
                            f"Stored link for oversized attachment: {filename} "
                            f"(limit {MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB)"
                        )
                        return None, message, {
                            "filename": filename,
                            "url": attachment["url"],
                        }
                    temporary_file.write(chunk)
            content_type = response.headers.get(
                "Content-Type", "application/octet-stream"
            )

        with open(temporary_path, "rb") as attachment_file:
            encoder = MultipartEncoder(
                fields={
                    "file": (filename, attachment_file, content_type),
                }
            )
            upload_response = SESSION.post(
                f"{JIRA_BASE_URL}/rest/api/3/issue/{issue_key}/attachments",
                auth=(JIRA_EMAIL, jira_api_token),
                headers={
                    "X-Atlassian-Token": "no-check",
                    "Content-Type": encoder.content_type,
                },
                data=encoder,
                timeout=30,
            )
            upload_response.raise_for_status()
        uploaded_attachment = upload_response.json()[0]
        existing_filenames.add(filename)
        return uploaded_attachment, f"Uploaded attachment: {filename}", None
    finally:
        if temporary_path:
            Path(temporary_path).unlink(missing_ok=True)


def jira_text_nodes(text):
    return [{"type": "text", "text": text}] if text else []


def add_jira_comment(
    issue_key,
    comment_header,
    comment_body,
    attachment_links,
    jira_api_token,
):
    content = [
        {
            "type": "paragraph",
            "content": jira_text_nodes(comment_header),
        }
    ]
    if comment_body:
        content.append(
            {
                "type": "paragraph",
                "content": [
                    {
                        "type": "text",
                        "text": comment_body,
                        "marks": [{"type": "strong"}],
                    }
                ],
            }
        )
    if attachment_links:
        content.append(
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": "Attachments:"}],
            }
        )
        for attachment in attachment_links:
            content.append(
                {
                    "type": "paragraph",
                    "content": [
                        {
                            "type": "text",
                            "text": attachment["filename"],
                            "marks": [
                                {
                                    "type": "link",
                                    "attrs": {"href": attachment["url"]},
                                }
                            ],
                        }
                    ],
                }
            )
    response = SESSION.post(
        f"{JIRA_BASE_URL}/rest/api/3/issue/{issue_key}/comment",
        auth=(JIRA_EMAIL, jira_api_token),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        json={
            "body": {
                "type": "doc",
                "version": 1,
                "content": content,
            },
            "properties": [
                {
                    "key": "sd.public.comment",
                    "value": {"internal": True},
                }
            ],
        },
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def jira_body_contains_marker(body, marker):
    if isinstance(body, dict):
        return any(
            jira_body_contains_marker(value, marker)
            for value in body.values()
        )
    if isinstance(body, list):
        return any(jira_body_contains_marker(value, marker) for value in body)
    return isinstance(body, str) and marker in body


def get_jira_comment_text(body):
    if isinstance(body, dict):
        if body.get("type") == "text":
            return body.get("text", "")
        return "".join(get_jira_comment_text(value) for value in body.values())
    if isinstance(body, list):
        return "".join(get_jira_comment_text(value) for value in body)
    return ""


def comment_fingerprint(comment):
    return "\n".join(
        [
            comment["CommentCreatedBy"],
            comment["CommentCreatedDate"],
            comment["PlainText"],
        ]
    )


def write_comments_csv(comments):
    output_path = Path(__file__).resolve().parent / "work_item_comments.csv"
    with output_path.open(
        "w",
        newline="",
        encoding="utf-8-sig",
    ) as output_file:
        fieldnames = [
            "ADO",
            "WorkItemId",
            "CommentId",
            "CommentCreatedDate",
            "CommentCreatedBy",
            "CommentText",
            "PlainText",
        ]
        writer = csv.DictWriter(output_file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(comments)


def main():
    configure_logging()
    update_run_state("running")
    LOGGER.info("Sync session started.")
    ensure_setup()
    ensure_environment_variables()
    jira_api_token = get_required_environment_variable("JIRA_API_TOKEN")
    jira_issue_key = get_jira_issue_key()
    jira_issue_key_upper = jira_issue_key.upper()
    jira_prefix = next(
        (
            prefix
            for prefix in ADO_CONFIGS
            if jira_issue_key_upper.startswith(prefix.upper())
        ),
        None,
    )
    if jira_prefix is None:
        valid_prefixes = ", ".join(sorted(prefix.upper() for prefix in ADO_CONFIGS.keys()))
        raise ValueError(
            f"The Jira issue key must begin with one of: {valid_prefixes}."
        )
    LOGGER.info("Validated Jira issue key prefix.")

    try:
        jira_summary, jira_attachments, jira_comments = get_jira_issue_data(
            jira_issue_key, jira_api_token
        )
    except requests.RequestException as error:
        raise RuntimeError(f"Could not read Jira issue {jira_issue_key}: {error}") from error

    work_item_inputs = extract_ado_work_item_ids(jira_summary)
    LOGGER.info(
        "Found ADO work item ID(s) in Jira summary '%s': %s",
        jira_summary,
        ", ".join(work_item_inputs),
    )
    update_run_state(
        "running",
        jira_issue_key=jira_issue_key,
        work_item_ids=work_item_inputs,
    )
    config = ADO_CONFIGS[jira_prefix]
    LOGGER.info("Target Jira issue: %s", jira_issue_key)
    print("\nADO work item matching:")
    print("The script searched the Jira title/summary for the first standalone number with at least four digits.")
    print(f"Jira title/summary: {jira_summary}")
    print(f"Selected ADO work item ID: {', '.join(work_item_inputs)}")
    work_items = []
    for work_item_input in work_item_inputs:
        if not work_item_input.isdigit():
            raise ValueError(f"'{work_item_input}' must be a numeric work item ID.")
        pat_env_var = f"AZDO_{jira_prefix.upper()}_PAT"
        work_items.append(
            {
                "display_id": work_item_input,
                "work_item_id": int(work_item_input),
                "prefix": jira_prefix,
                "organization_url": config["organization_url"],
                "pat": get_required_environment_variable(pat_env_var),
            }
        )
    preview_first = input("Preview first? (y/n): ").strip().lower() in {"y", "yes"}
    update_run_state("running", preview_first=preview_first)

    comments = []
    attachments = []
    work_item_statuses = []
    failures = []
    for work_item in work_items:
        work_item_id = work_item["work_item_id"]
        try:
            credentials = BasicAuthentication("", work_item["pat"])
            connection = Connection(
                base_url=work_item["organization_url"],
                creds=credentials,
            )
            work_item_tracking_client = connection.clients.get_work_item_tracking_client()
            ado_work_item = work_item_tracking_client.get_work_item(work_item_id)
            work_item_statuses.append(get_work_item_status(ado_work_item, work_item_id))
            for comment in get_work_item_comments(
                work_item_tracking_client,
                work_item_id,
                work_item["pat"],
                work_item=ado_work_item,
            ):
                raw_text = comment.get("text", "")
                comments.append(
                    {
                        "WorkItemId": work_item_id,
                        "ADO": work_item["prefix"],
                        "CommentId": comment.get("id", ""),
                        "CommentCreatedDate": comment.get("createdDate", ""),
                        "CommentCreatedBy": get_comment_author(comment),
                        "CommentText": raw_text,
                        "PlainText": remove_html_formatting(raw_text),
                    }
                )
            ado_attachments = get_work_item_attachments(
                work_item["organization_url"],
                work_item_id,
                work_item["pat"],
            )
            for attachment in ado_attachments:
                attachment["ado"] = work_item["prefix"]
                attachment["pat"] = work_item["pat"]
            attachments.extend(ado_attachments)
        except requests.RequestException as error:
            LOGGER.error("Azure DevOps request failed for work item %s: %s", work_item_id, error)
            failures.append(f"ADO work item {work_item_id}: {error}")
        except Exception as error:
            LOGGER.exception("Unexpected error retrieving work item %s.", work_item_id)
            failures.append(f"ADO work item {work_item_id}: {error}")

    comments.sort(key=comment_timestamp_sort_key)
    write_comments_csv(comments)
    LOGGER.info(
        "Retrieved %d comment(s) and %d attachment(s); CSV export written.",
        len(comments),
        len(attachments),
    )

    existing_comment_text = {
        get_jira_comment_text(comment.get("body", {}))
        for comment in jira_comments
    }
    existing_filenames = {
        attachment.get("filename")
        for attachment in jira_attachments
        if attachment.get("filename")
    }
    unique_attachments = {
        attachment["url"]: attachment for attachment in attachments
    }
    for comment in comments:
        for comment_attachment in get_comment_attachment_urls(comment["CommentText"]):
            url = comment_attachment["url"]
            unique_attachments.setdefault(
                url,
                {
                    "url": url,
                    "name": comment_attachment["name"] or "screenshot.png",
                    "work_item_id": comment["WorkItemId"],
                    "ado": comment["ADO"],
                    "pat": next(
                        work_item["pat"]
                        for work_item in work_items
                        if work_item["prefix"] == comment["ADO"]
                    ),
                },
            )

    LOGGER.info(
        "Found %d ADO comments.",
        len(comments),
        extra={"show_console": preview_first},
    )
    LOGGER.info(
        "Found %d unique ADO attachments.",
        len(unique_attachments),
        extra={"show_console": preview_first},
    )
    LOGGER.info(
        "Found status information for %d work item(s).",
        len(work_item_statuses),
        extra={"show_console": preview_first},
    )
    LOGGER.info(
        "Target Jira issue: %s",
        jira_issue_key,
        extra={"show_console": preview_first},
    )
    if failures:
        LOGGER.warning(
            "Encountered %d retrieval failure(s).",
            len(failures),
            extra={"show_console": preview_first},
        )
    if preview_first:
        LOGGER.info("Preview displayed before Jira posting.")

    if input("Continue posting to Jira? (y/n): ").strip().lower() not in {"y", "yes"}:
        update_run_state("cancelled", reason="User declined Jira posting.")
        LOGGER.info("Sync cancelled before Jira posting.")
        return

    uploaded_by_url = {}
    fallback_links = {}
    for attachment in unique_attachments.values():
        try:
            uploaded_attachment, message, fallback_link = upload_jira_attachment(
                jira_issue_key,
                attachment,
                attachment["pat"],
                jira_api_token,
                existing_filenames,
            )
            LOGGER.info(message)
            if fallback_link:
                fallback_links[fallback_link["url"]] = fallback_link
            if uploaded_attachment:
                uploaded_by_url[attachment["url"]] = {
                    "filename": uploaded_attachment["filename"],
                    "url": uploaded_attachment["content"],
                }
        except (requests.RequestException, KeyError) as error:
            LOGGER.error("Attachment upload failed for %s: %s", attachment["name"], error)
            failures.append(f"Attachment {attachment['name']}: {error}")

    posted_count = 0
    skipped_count = 0
    for comment in comments:
        comment_header = (
            f"Comment Created by: {comment['CommentCreatedBy']}\n"
            f"Date Created: {format_ado_timestamp(comment['CommentCreatedDate'])}"
        )
        if (
            comment_header + comment["PlainText"]
            in existing_comment_text
        ):
            skipped_count += 1
            continue

        attachment_links = [
            uploaded_by_url[comment_attachment["url"]]
            for comment_attachment in get_comment_attachment_urls(
                comment["CommentText"]
            )
            if comment_attachment["url"] in uploaded_by_url
        ]
        attachment_links.extend(
            fallback_links[comment_attachment["url"]]
            for comment_attachment in get_comment_attachment_urls(
                comment["CommentText"]
            )
            if comment_attachment["url"] in fallback_links
        )
        try:
            add_jira_comment(
                jira_issue_key,
                comment_header,
                comment["PlainText"],
                attachment_links,
                jira_api_token,
            )
            posted_count += 1
            LOGGER.info("Posted one Jira comment.")
        except requests.RequestException as error:
            LOGGER.error("Jira comment post failed for %s: %s", comment['CommentCreatedBy'], error)
            failures.append(f"Comment from {comment['CommentCreatedBy']}: {error}")

    status_posted_count = 0
    seen_status_markers = set()
    for work_item_status in work_item_statuses:
        changed_date = work_item_status["changed_date"]
        status_marker = (
            f"[ADO_TO_JIRA_STATE:{work_item_status['work_item_id']}:"
            f"{work_item_status['state']}:{changed_date}]"
        )
        if status_marker in seen_status_markers or any(
            status_marker in text for text in existing_comment_text
        ):
            continue
        status_header = (
            f"{status_marker} ADO work item {work_item_status['work_item_id']} status"
        )
        status_body = (
            f"State: {work_item_status['state']}\n"
            f"Last changed by: {work_item_status['changed_by']}\n"
            f"Last changed: {format_ado_timestamp(changed_date)}"
        )
        try:
            add_jira_comment(
                jira_issue_key,
                status_header,
                status_body,
                [],
                jira_api_token,
            )
            status_posted_count += 1
            seen_status_markers.add(status_marker)
            existing_comment_text.add(status_marker)
            LOGGER.info(
                "Posted status for ADO work item %s.",
                work_item_status["work_item_id"],
            )
        except requests.RequestException as error:
            failures.append(
                f"Status for work item {work_item_status['work_item_id']}: {error}"
            )
            LOGGER.error(
                "Jira status post failed for work item %s: %s",
                work_item_status["work_item_id"],
                error,
            )

    if fallback_links:
        link_header = "[ADO_TO_JIRA_LINKS] Attachments not uploaded"
        link_body = (
            "These Azure DevOps attachments exceeded the configured upload limit "
            "and were stored as links instead."
        )
        if link_header + link_body not in existing_comment_text:
            add_jira_comment(
                jira_issue_key,
                link_header,
                link_body,
                list(fallback_links.values()),
                jira_api_token,
            )
            LOGGER.info("Posted %d oversized attachment link(s).", len(fallback_links))

    LOGGER.info(
        "Sync run completed: posted %d comment(s), skipped %d duplicate(s), failures %d.",
        posted_count,
        skipped_count,
        len(failures),
    )
    update_run_state(
        "completed",
        posted_comments=posted_count,
        posted_status_comments=status_posted_count,
        skipped_duplicates=skipped_count,
        failures=len(failures),
    )
    if failures:
        for failure in failures:
            LOGGER.error("Failure: %s", failure)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        configure_logging()
        update_run_state("failed", error=str(error))
        LOGGER.exception("Run stopped with error: %s", error)
        raise SystemExit(f"Error: {error}") from error
