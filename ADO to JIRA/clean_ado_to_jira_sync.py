import csv
import json
import os
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlparse

import requests
from azure.devops.connection import Connection
from msrest.authentication import BasicAuthentication


CONFIG_PATH = Path(__file__).resolve().parent / "config.json"
ADO_CONFIGS = {}
JIRA_BASE_URL = ""
JIRA_EMAIL = ""


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
    global ADO_CONFIGS, JIRA_BASE_URL, JIRA_EMAIL

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
    except (KeyError, TypeError, AttributeError) as error:
        raise RuntimeError(
            "Configuration must include ado_configs and jira settings."
        ) from error


def view_readme():
    readme_path = Path(__file__).resolve().parent / "README.md"
    if not readme_path.exists():
        print(f"README not found: {readme_path}")
        return

    print("\n" + readme_path.read_text(encoding="utf-8"))
    input("Press Enter to continue...")


def get_jira_issue_key():
    while True:
        # CHANGE THIS: update the accepted prefixes if your Jira projects differ.
        value = get_required_input(
            "Jira issue key (ORG1- or ORG2-, or R to view README): "
        )
        if value.lower() == "r":
            view_readme()
            continue
        return value


def get_work_item_comments(work_item_tracking_client, work_item_id, pat):
    work_item = work_item_tracking_client.get_work_item(work_item_id)
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
        response = requests.get(
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


def get_work_item_attachments(organization_url, work_item_id, pat):
    response = requests.get(
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
    response = requests.get(
        f"{JIRA_BASE_URL}/rest/api/3/issue/{issue_key}",
        params={"fields": "attachment,comment"},
        auth=(JIRA_EMAIL, jira_api_token),
        headers={"Accept": "application/json"},
        timeout=30,
    )
    response.raise_for_status()
    fields = response.json().get("fields", {})
    return fields.get("attachment", []), fields.get("comment", {}).get("comments", [])


def upload_jira_attachment(
    issue_key, attachment, pat, jira_api_token, existing_filenames
):
    filename = get_attachment_filename(attachment)
    if filename in existing_filenames:
        return None, f"Skipped existing Jira attachment: {filename}"

    response = requests.get(
        attachment["url"],
        auth=("", pat),
        timeout=30,
    )
    response.raise_for_status()
    upload_response = requests.post(
        f"{JIRA_BASE_URL}/rest/api/3/issue/{issue_key}/attachments",
        auth=(JIRA_EMAIL, jira_api_token),
        headers={"X-Atlassian-Token": "no-check"},
        files={
            "file": (
                filename,
                response.content,
                response.headers.get("Content-Type", "application/octet-stream"),
            )
        },
        timeout=30,
    )
    upload_response.raise_for_status()
    uploaded_attachment = upload_response.json()[0]
    existing_filenames.add(filename)
    return uploaded_attachment, f"Uploaded attachment: {filename}"


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
    response = requests.post(
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
    load_configuration()
    jira_api_token = get_required_environment_variable("JIRA_API_TOKEN")
    jira_issue_key = get_jira_issue_key()
    jira_issue_key_upper = jira_issue_key.upper()
    jira_prefix = next(
        (
            prefix
            for prefix in ADO_CONFIGS
            if jira_issue_key_upper.startswith(prefix)
        ),
        None,
    )
    if jira_prefix is None:
        raise ValueError("The Jira issue key must begin with ORG1- or ORG2-.")

    work_item_inputs = [
        work_item_input.strip()
        for work_item_input in get_required_input(
            "Azure DevOps work item IDs (comma-separated): "
        ).split(",")
        if work_item_input.strip()
    ]
    config = ADO_CONFIGS[jira_prefix]
    work_items = []
    for work_item_input in work_item_inputs:
        if not work_item_input.isdigit():
            raise ValueError(f"'{work_item_input}' must be a numeric work item ID.")
        work_items.append(
            {
                "display_id": work_item_input,
                "work_item_id": int(work_item_input),
                "prefix": jira_prefix,
                "organization_url": config["organization_url"],
                "pat": get_required_environment_variable(
                    "AZDO_ORG1_PAT"
                    if jira_prefix == "ORG1"
                    else "AZDO_ORG2_PAT"
                ),
            }
        )
    preview_only = input("Preview only? (y/n): ").strip().lower() in {"y", "yes"}

    comments = []
    attachments = []
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
            for comment in get_work_item_comments(
                work_item_tracking_client, work_item_id, work_item["pat"]
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
            failures.append(f"ADO work item {work_item_id}: {error}")
        except Exception as error:
            failures.append(f"ADO work item {work_item_id}: {error}")

    comments.sort(key=comment_timestamp_sort_key)
    write_comments_csv(comments)

    try:
        jira_attachments, jira_comments = get_jira_issue_data(
            jira_issue_key, jira_api_token
        )
    except requests.RequestException as error:
        raise RuntimeError(f"Could not read Jira issue {jira_issue_key}: {error}") from error

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

    print(f"Found {len(comments)} ADO comments.")
    print(f"Found {len(unique_attachments)} unique ADO attachments.")
    print(f"Target Jira issue: {jira_issue_key}")
    if failures:
        print(f"Encountered {len(failures)} retrieval failure(s).")
    if preview_only:
        print("Preview only: no Jira comments or attachments will be changed.")
        return

    if input("Continue with Jira updates? (y/n): ").strip().lower() not in {"y", "yes"}:
        print("Cancelled; no Jira updates were made.")
        return

    uploaded_by_url = {}
    for attachment in unique_attachments.values():
        try:
            uploaded_attachment, message = upload_jira_attachment(
                jira_issue_key,
                attachment,
                attachment["pat"],
                jira_api_token,
                existing_filenames,
            )
            print(message)
            if uploaded_attachment:
                uploaded_by_url[attachment["url"]] = {
                    "filename": uploaded_attachment["filename"],
                    "url": uploaded_attachment["content"],
                }
        except (requests.RequestException, KeyError) as error:
            failures.append(f"Attachment {attachment['name']}: {error}")
            print(f"Failed attachment {attachment['name']}: {error}")

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
        try:
            add_jira_comment(
                jira_issue_key,
                comment_header,
                comment["PlainText"],
                attachment_links,
                jira_api_token,
            )
            posted_count += 1
        except requests.RequestException as error:
            failures.append(f"Comment from {comment['CommentCreatedBy']}: {error}")
            print(f"Failed comment from {comment['CommentCreatedBy']}: {error}")

    print(f"Posted {posted_count} Jira comments; skipped {skipped_count} duplicates.")
    if failures:
        print("Failures:")
        for failure in failures:
            print(f"- {failure}")


if __name__ == "__main__":
    try:
        main()
    except (KeyError, ValueError, RuntimeError) as error:
        raise SystemExit(f"Error: {error}") from error
