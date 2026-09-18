param(
    [string]$CommitMessage = "chore: sync public workspace",
    [switch]$PushOnly
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $projectRoot

# Local maintenance marker: a scheduled sync must never publish a partial edit.
$maintenancePath = Join-Path $projectRoot ".git\codex-maintenance.lock"
if (Test-Path -LiteralPath $maintenancePath) {
    Write-Host "GitHub sync paused for local maintenance; see .git/codex-maintenance.lock."
    exit 0
}

# Prevent overlapping scheduled/manual syncs from sharing a rebase state.
$lockPath = Join-Path $projectRoot ".git\codex-github-sync.lock"
$lockStream = $null
try {
    $lockStream = [System.IO.File]::Open($lockPath, [System.IO.FileMode]::OpenOrCreate, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
} catch {
    Write-Host "GitHub sync already running; exiting without changes."
    exit 0
}

try {
    $branch = (git branch --show-current).Trim()
    if ([string]::IsNullOrWhiteSpace($branch)) { throw "Current directory is not on a Git branch." }

    # git add -u omits new modules. Refuse a partial dependency graph instead of
    # silently publishing callers without their implementation or tests.
    python tools/check_tracked_imports.py
    if ($LASTEXITCODE -ne 0) { throw "Tracked code depends on unpublished local modules; sync stopped." }

    # Scan the complete tracked publish surface before staging anything.
    python tools/pre_publish_check.py
    if ($LASTEXITCODE -ne 0) { throw "Pre-publish check failed; sync stopped." }

    if (-not $PushOnly) {
        git add -u
        git diff --cached --quiet
        $hasStagedChanges = ($LASTEXITCODE -ne 0)
        if ($hasStagedChanges) {
            git commit -m $CommitMessage
        }
    }

    git fetch origin $branch
    git pull --rebase --autostash origin $branch
    if ($LASTEXITCODE -ne 0) { throw "Pull/rebase failed; working tree was kept for manual conflict resolution." }

    $maxRetry = 3
    for ($attempt = 1; $attempt -le $maxRetry; $attempt++) {
        Write-Host "Push attempt $attempt/$maxRetry ..."
        git push origin $branch
        if ($LASTEXITCODE -eq 0) {
            Write-Host "GitHub sync complete."
            exit 0
        }
        if ($attempt -lt $maxRetry) {
            Start-Sleep -Seconds (4 * $attempt)
            git fetch origin $branch
            git pull --rebase --autostash origin $branch
            if ($LASTEXITCODE -ne 0) { throw "Retry rebase failed; working tree was kept." }
        }
    }
    throw "Push failed after $maxRetry attempts."
} finally {
    if ($null -ne $lockStream) { $lockStream.Dispose() }
    Remove-Item -LiteralPath $lockPath -Force -ErrorAction SilentlyContinue
}
