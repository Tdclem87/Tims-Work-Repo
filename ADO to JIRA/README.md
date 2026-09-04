# Azure DevOps to Jira Sync

This project copies Azure DevOps work item comments and attachments into Jira issues.

## Repository layout

This repo is intentionally split into public-safe and local-only files:

- `clean_ado_to_jira_sync.py` - public-safe version for GitHub
- `config.example.json` - example configuration template
- `README.md` - public documentation
- `ADO to Jira Sync.cmd` - Windows launcher and Command Prompt fallback
- `ADO to Jira Sync.ps1` - preferred persistent PowerShell launcher
- `ado_to_jira_sync.py` - local working copy kept on your machine
- `config.json` - local configuration file with your actual org URLs and Jira email

Keep the local working script and local config file off Git. The public repo should only contain the safe, shareable versions.

## Requirements

- Python 3.9 or newer
- Access to the Azure DevOps organizations you want to sync
- Jira Cloud access
- One Jira API token
- One Azure DevOps PAT per configured organization prefix

## Install dependencies

```powershell
python -m pip install -r requirements.txt
```

The dependency list is stored in `requirements.txt` next to the scripts.

## What the sync does

- Reads comments and attachments from Azure DevOps work items.
- Converts HTML comment content into readable Jira text.
- Uploads work item attachments and comment-linked screenshots to Jira.
- Streams attachment downloads and uploads instead of loading entire files into memory.
- Reuses one HTTP session per run for connection pooling.
- Stores a clickable Jira link when an attachment exceeds the configured size limit.
- Avoids reposting matching Jira comments and existing attachment filenames.
- Supports multiple Azure DevOps organizations using configurable Jira prefixes.
- Writes the retrieved comments to `work_item_comments.csv` for review.

## First-time setup

When you run the script for the first time, it will detect whether a configuration file is missing and prompt you to set it up.

The script asks for:

- how many Azure DevOps organizations you want to configure
- the Jira prefix for each organization, such as `MBSD` or `ONSD`
- the Azure DevOps URL for each organization
- the Jira base URL
- the Jira email address

It then creates a local `config.json` file next to the script.

Example format:

```json
{
	"ado_configs": {
		"MBSD": {
			"organization_url": "https://dev.azure.com/your-org"
		},
		"ONSD": {
			"organization_url": "https://dev.azure.com/another-org"
		}
	},
	"jira": {
		"base_url": "https://your-company.atlassian.net",
		"email": "you@example.com"
	}
}
```

The optional `max_attachment_size_mb` setting controls the upload limit. It defaults to `100` when omitted. Attachments larger than this limit are not uploaded; their Azure DevOps links are posted to Jira instead.

The organization prefix is used to build the expected environment variable name for that PAT.

Example:

- `MBSD` => `AZDO_MBSD_PAT`
- `ONSD` => `AZDO_ONSD_PAT`

## Add credentials to Windows

After creating or loading `config.json`, the script checks whether the required environment variables are available. If any are missing, it displays:

1. Press the Windows key and search for `environment variables`.
2. Open **Edit the system environment variables**.
3. Select **Environment Variables**.
4. Under User variables, select **New**.
5. Enter each missing variable name exactly as shown by the script.
6. Enter the matching Jira API token or Azure DevOps PAT as the value.
7. Select **OK** on each window to save the changes.
8. Close and reopen PowerShell, Command Prompt, or VS Code.

The script then asks whether it should open the Windows Environment Variables editor automatically. Answer `Y` or `yes` to open it, or `N` to open it yourself.

### Create a Jira API token

1. Open the [Atlassian API token page](https://id.atlassian.com/manage-profile/security/api-tokens).
2. Select **Create API token**.
3. Enter a label and create the token.
4. Copy the token immediately. Atlassian will not show it again.
5. Save it as `JIRA_API_TOKEN` in Windows environment variables.

### Create an Azure DevOps PAT

Create one PAT for each configured Azure DevOps organization:

1. Sign in to the Azure DevOps organization.
2. Select your profile icon, then **User settings** > **Personal access tokens**.
3. Select **New Token**, then enter a name and expiration date.
4. Choose the organization and grant the minimum **Work Items** read access needed by this tool.
5. Create the PAT and copy it immediately. Azure DevOps will not show it again.
6. Save it as `AZDO_<PREFIX>_PAT`, replacing `<PREFIX>` with the configured organization prefix.

For example, an organization configured with the prefix `MBSD` uses `AZDO_MBSD_PAT`.

Add these values in Windows as user or system environment variables:

- `JIRA_API_TOKEN` - your Jira API token
- `AZDO_<PREFIX>_PAT` - one PAT for each configured organization prefix

Example:

```powershell
$env:JIRA_API_TOKEN = "your-jira-token"
$env:AZDO_MBSD_PAT = "your-azure-devops-pat"
$env:AZDO_ONSD_PAT = "your-other-azure-devops-pat"
```

For a permanent setup, add the same values through the Windows Environment Variables UI:

1. Press the Windows key and search for `environment variables`.
2. Open **Edit the system environment variables**.
3. Select **Environment Variables**.
4. Choose **New** under User variables.
5. Add each variable name/value pair.
6. Select **OK** to save.
7. Close and reopen PowerShell, Command Prompt, or VS Code so the variables are available.

To check whether a variable exists without exposing its value:

```powershell
@("JIRA_API_TOKEN", "AZDO_MBSD_PAT", "AZDO_ONSD_PAT") | ForEach-Object {
	"$_ set: " + [bool](Get-Item "Env:$_" -ErrorAction SilentlyContinue)
}
```

Never put tokens directly in the script, `config.json`, or Git.

## Run the script

From the folder containing the script:

```powershell
python clean_ado_to_jira_sync.py
```

### Use the automatic Windows launcher

Double-click `ADO to Jira Sync.cmd` to launch the application. The launcher prefers Windows PowerShell and falls back to a Command Prompt loop if PowerShell is unavailable. It:

- uses `powershell.exe` when it is available
- prefers the repository virtual environment at `.venv\Scripts\python.exe` when available
- uses the local `ado_to_jira_sync.py` working copy when it is available
- falls back to `clean_ado_to_jira_sync.py` for a public-safe checkout
- keeps the console open after each run
- automatically starts the first prompt again after the run finishes

You can also start it from PowerShell or Command Prompt:

```powershell
& ".\ADO to Jira Sync.cmd"
```

The launcher does not contain credentials. Tokens continue to come from Windows environment variables.

The script will ask for the Jira issue key and comma-separated work item IDs. It then asks `Preview first?`. After retrieval, it displays the number of comments, attachments, and retrieval failures found. Review that output, then answer `Continue posting to Jira?` with `Y` to upload attachments and add comments, or `N` to cancel without Jira changes.

You can also type `R` at the Jira key prompt to view this README from the script.

The CSV export is written next to the script as `work_item_comments.csv`.

## Local activity logs

Each script writes activity and error information to a local `logs` folder next to the script. The log file is named `activity.log` and rotates at midnight. The current file plus up to 89 previous daily files are retained, so logs are kept for no more than 90 daily files. Operational messages also appear in the console through the logging system, while prompts and interactive setup instructions remain direct user prompts.

Logs include setup events, retrieval counts, preview and cancellation decisions, upload/post results, and error details. Tokens, PAT values, and other credential values are never written to the logs. The `logs` folder is ignored by Git and should remain local.

Each script invocation also receives a short session ID. Every log entry includes that ID, making it possible to isolate one run when several runs are present in the same daily log file.

The script also keeps a local `run_state.json` file with the latest session status, target issue, work-item IDs, preview choice, counts, and outcome. It contains no credentials, is updated atomically, and is ignored by Git. A `running` status after an interruption indicates that the previous session did not finish normally.

## Security guidance

- Keep `config.json` local and do not commit it.
- Never commit PATs, API tokens, `.env` files, or generated CSV exports.
- Keep the public repo limited to safe shareable files.
- Rotate any credential that may have been exposed.
- Review the preview and destination Jira issue before answering `Continue posting to Jira?` with `Y`.

## Public-safe workflow

Use the clean, public-safe script for GitHub or other shared environments. Use the local working copy for your real configuration and testing.

This keeps your secrets local while still letting you commit a clean version that others can inspect and reuse.
