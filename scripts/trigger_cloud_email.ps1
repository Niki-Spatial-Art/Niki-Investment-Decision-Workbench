[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("intraday", "post_close")]
    [string]$Mode,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$logDir = Join-Path $projectRoot "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logFile = Join-Path $logDir "cloud_email_trigger_$(Get-Date -Format 'yyyyMMdd').log"

function Write-TriggerLog {
    param([string]$Message)
    $line = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $Message"
    $line | Tee-Object -FilePath $logFile -Append
}

if ((Get-Date).DayOfWeek -in @([DayOfWeek]::Saturday, [DayOfWeek]::Sunday)) {
    Write-TriggerLog "skip=non_trading_weekend mode=$Mode"
    exit 0
}

$workflow = if ($Mode -eq "intraday") { "action-audit.yml" } else { "email-preview.yml" }
$repository = "Niki-Spatial-Art/Niki-Investment-Decision-Workbench"
$gh = (Get-Command gh -ErrorAction Stop).Source

if ($DryRun) {
    Write-TriggerLog "dry_run=would_dispatch workflow=$workflow repo=$repository"
    exit 0
}

Write-TriggerLog "dispatching workflow=$workflow repo=$repository"
if ($Mode -eq "intraday") {
    # Local Windows checkpoints are deliberate radar slots. Force the manual
    # workflow path so a closed gate still sends the risk/status card.
    & $gh workflow run $workflow --repo $repository --ref main --field force_send=true 2>&1 | ForEach-Object {
        Write-TriggerLog $_.ToString()
    }
} else {
    & $gh workflow run $workflow --repo $repository --ref main 2>&1 | ForEach-Object {
        Write-TriggerLog $_.ToString()
    }
}
if ($LASTEXITCODE -ne 0) {
    throw "GitHub workflow dispatch failed with exit code $LASTEXITCODE"
}
Write-TriggerLog "dispatch_accepted workflow=$workflow"
