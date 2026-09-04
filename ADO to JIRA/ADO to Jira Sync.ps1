$scriptName = "ado_to_jira_sync.py"
if (-not (Test-Path (Join-Path $PSScriptRoot $scriptName))) {
    $scriptName = "clean_ado_to_jira_sync.py"
}

Set-Location $PSScriptRoot

while ($true) {
    Clear-Host
    Write-Host ""
    Write-Host "===== Azure DevOps to Jira Sync =====" -ForegroundColor Green
    Write-Host "Starting a new run with $scriptName..."
    Write-Host ""

    python $scriptName

    Write-Host ""
    Write-Host "Run completed."
    Read-Host "Press Enter to restart from the first prompt"
}
