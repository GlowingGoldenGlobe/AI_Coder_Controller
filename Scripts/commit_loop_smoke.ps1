param(
    [ValidateSet("app","vscode")] [string]$Mode = 'app',
    [string]$Title = 'Copilot'
)

$root = Split-Path $PSScriptRoot -Parent
$logDir = Join-Path $root 'logs\actions'
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }
$logPath = Join-Path $logDir 'copilot_commit_smoke.log'
try { Remove-Item -Path $logPath -Force -ErrorAction SilentlyContinue } catch {}

$scriptPath = Join-Path $PSScriptRoot 'copilot_commit.ps1'

Write-Host "Starting loop smoke..." -ForegroundColor Cyan
$argsList = @(
  '-NoProfile','-ExecutionPolicy','Bypass','-File', $scriptPath,
  '-Mode', $Mode,
  '-Title', $Title,
  '-StartAfterSeconds', '1',
  '-RepeatSeconds', '2',
  '-RepeatCount', '2',
  '-Message', 'Auto message from powershell — see projects/Self-Improve/next_steps.md',
  '-LogPath', $logPath
)
& powershell @argsList

Write-Host "Loop completed. Log:" -ForegroundColor Green
try {
  Get-Content -Path $logPath -ErrorAction Stop
} catch {
  Write-Host '(no log entries)' -ForegroundColor Yellow
}
