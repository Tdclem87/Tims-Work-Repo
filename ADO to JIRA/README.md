# Azure DevOps to Jira Sync

A Python utility that copies Azure DevOps work item comments and attachments into Jira.

## Requirements

- Python 3.9 or newer
- Access to the configured Azure DevOps organizations
- Jira Cloud access
- Azure DevOps PATs and a Jira API token

## Install

```powershell
python -m pip install requests azure-devops msrest
```

## Configure

### 1. Create the local config file

Copy `config.example.json` to `config.json`. Open `config.json` and update:

- Azure DevOps organization URLs for `ORG1` and `ORG2`
- Jira Cloud site URL and account email

Keep `config.json` local. Commit `config.example.json`, but never commit `config.json` or token values.

### 2. Add credentials to Windows

1. Press the Windows key and search for `environment variables`.
2. Select **Edit the system environment variables**.
3. In the **System Properties** window, select **Environment Variables**.
4. Under **User variables for [your username]**, select **New**.
5. Add the first variable:
	- Variable name: `AZDO_ORG1_PAT`
	- Variable value: your PAT for Azure DevOps organization 1
6. Select **New** again and add:
	- Variable name: `AZDO_ORG2_PAT`
	- Variable value: your PAT for Azure DevOps organization 2
7. Select **New** again and add:
	- Variable name: `JIRA_API_TOKEN`
	- Variable value: your Jira API token
8. Select **OK** on each window to save the variables.
9. Close and reopen PowerShell, Command Prompt, or VS Code so the new variables are available.

To verify a variable exists without displaying its secret, run this in PowerShell:

```powershell
@("AZDO_ORG1_PAT", "AZDO_ORG2_PAT", "JIRA_API_TOKEN") | ForEach-Object {
	 "$_ set: " + [bool](Get-Item "Env:$_" -ErrorAction SilentlyContinue)
}
```

Never put token values in the script, `config.json`, or Git.

## Run

```powershell
python clean_ado_to_jira_sync.py
```

Enter the Jira issue key and comma-separated Azure DevOps work item IDs. Use preview mode before making Jira changes. Type `R` at the Jira prompt to view this README.

The CSV export is written next to the script as `work_item_comments.csv`.

## Security

- Never commit PATs, API tokens, `.env` files, or generated CSV files.
- Rotate any credential that may have been exposed.
- Review the destination Jira issue before running without preview mode.
