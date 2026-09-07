$ErrorActionPreference = "Stop"
$env:PYTHONIOENCODING = "utf-8"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $projectRoot

Write-Host "Project: $projectRoot"

Write-Host "Checking Python files..."
python -m py_compile monitor.py
python -c "import json; json.load(open('portfolio.json', encoding='utf-8')); print('portfolio ok')"

Write-Host "Installing local Windows monitor tasks..."
$taskInstaller = Join-Path $projectRoot "install_windows_tasks_schtasks.ps1"
if (Test-Path -LiteralPath $taskInstaller) {
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File $taskInstaller
} else {
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $projectRoot "setup_windows_0910_task.ps1")
}

Write-Host "Synchronizing public GitHub changes..."
& (Join-Path $projectRoot "sync_github.ps1") -CommitMessage "chore: sync short-term stock expansion controls"

Write-Host "Running monitor once now to generate and email the latest report..."
powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $projectRoot "run_monitor_local.ps1")

Write-Host "Done. Check your QQ mailbox and the logs folder."
