param(
    [string]$CommitMessage = "chore: sync public workspace",
    [switch]$PushOnly
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $projectRoot

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
    if ([string]::IsNullOrWhiteSpace($branch)) { throw "当前目录不在 Git 分支上。" }

    # Scan the complete tracked publish surface before staging anything.
    python tools/pre_publish_check.py
    if ($LASTEXITCODE -ne 0) { throw "发布前敏感信息检查失败，已停止同步。" }

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
    if ($LASTEXITCODE -ne 0) { throw "拉取远程并 rebase 失败；工作区已保留，请解决冲突后重试。" }

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
            if ($LASTEXITCODE -ne 0) { throw "重试前 rebase 失败；工作区已保留。" }
        }
    }
    throw "推送重试 $maxRetry 次仍失败。"
} finally {
    if ($null -ne $lockStream) { $lockStream.Dispose() }
    Remove-Item -LiteralPath $lockPath -Force -ErrorAction SilentlyContinue
}
