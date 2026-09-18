$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$projectPython = Join-Path $PSScriptRoot ".venv-a-stock\Scripts\python.exe"
if ($env:A_STOCK_PYTHON) { $projectPython = $env:A_STOCK_PYTHON }
elseif (-not (Test-Path -LiteralPath $projectPython)) { $projectPython = (Get-Command python -ErrorAction Stop).Source }
& $projectPython -m unittest discover -s tests -v
if ($LASTEXITCODE -ne 0) { throw "Offline decision tests failed." }
& $projectPython -m compileall -q tools tests
if ($LASTEXITCODE -ne 0) { throw "Python compilation failed." }
& $projectPython tools/check_workflow_timezones.py
if ($LASTEXITCODE -ne 0) { throw "Workflow timezone check failed." }
& $projectPython tools/pre_publish_check.py --include-untracked
if ($LASTEXITCODE -ne 0) { throw "Publish-surface check failed." }
& $projectPython tools/workbench_health.py
if ($LASTEXITCODE -ne 0) { throw "Local health report failed." }
& $projectPython tools/local_dashboard.py --write-preview data/local_dashboard_preview.html
if ($LASTEXITCODE -ne 0) { throw "Dashboard preview failed." }
Write-Host "Local checks complete. No email, commit, push or trade performed."
