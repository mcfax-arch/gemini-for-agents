# gemini-for-agents installer for Windows
# Run: powershell -c "irm https://raw.githubusercontent.com/mcfax-arch/gemini-for-agents/main/install.ps1 | iex"
$ErrorActionPreference = 'Stop'

$repo = 'https://github.com/mcfax-arch/gemini-for-agents.git'
$installDir = Join-Path $env:USERPROFILE 'Documents\gemini-for-agents'
$taskName = 'Hermes Gemini Web2API'

Write-Host "==> Installing gemini-for-agents to $installDir" -ForegroundColor Cyan

# Clone or pull
if (Test-Path $installDir) {
    Write-Host "==> Updating existing installation..." -ForegroundColor Cyan
    git -C $installDir pull --ff-only
} else {
    git clone --depth=1 $repo $installDir
}

# Find Python
$python = $null
foreach ($candidate in @('python3', 'python')) {
    $p = (Get-Command $candidate -ErrorAction SilentlyContinue).Source
    if ($p) { $python = $p; break }
}
if (-not $python) {
    Write-Warning "Python not found. Install from https://python.org"
    Start-Process 'https://python.org/downloads/'
    exit 1
}

# Verify version
try {
    & $python -c "import sys; exit(0 if sys.version_info >= (3,8) else 1)"
} catch {
    Write-Warning "Python 3.8+ required"
    exit 1
}

Write-Host "==> Python: $(& $python --version)" -ForegroundColor Green

# ── Install Task Scheduler ──────────────────────────────────
$batPath = Join-Path $installDir 'start-gemini-web2api.bat'
$action = New-ScheduledTaskAction -Execute $batPath
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::MaxValue) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -Hidden

try {
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Description 'Start Gemini for Agents at user logon' -Force | Out-Null
    Write-Host "==> Task Scheduler: $taskName created (starts at logon)" -ForegroundColor Green
} catch {
    Write-Warning "Failed to register scheduled task: $_"
}

# ── Start now ───────────────────────────────────────────────
Write-Host "==> Starting server..." -ForegroundColor Cyan
try {
    & $python "$installDir\launch_gemini_web2api.py"
    Write-Host "==> ✅ gemini-for-agents started on http://127.0.0.1:8081" -ForegroundColor Green
    Write-Host "    Logs: $installDir\logs\gemini-web2api.log" -ForegroundColor Gray
} catch {
    Write-Warning "Start failed: $_"
    Write-Host "    Manual start: cd $installDir && python gemini_web2api.py" -ForegroundColor Yellow
}

Write-Host "`n==> Next steps:" -ForegroundColor Cyan
Write-Host "    1. Add to Hermes config.yaml as provider" -ForegroundColor White
Write-Host "    2. Server restarts automatically on next login" -ForegroundColor White
