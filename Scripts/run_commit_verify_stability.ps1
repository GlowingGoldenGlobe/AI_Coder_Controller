$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir
$log = Join-Path $scriptDir '..\logs\actions\commit_verify_stability.log'
if (Test-Path $log) { Remove-Item -Force $log }
for ($i = 1; $i -le 10; $i++) {
    Write-Host "--- Run $i ---"
    $commitScript = Join-Path $scriptDir 'commit_and_verify_2plus2.ps1'
    & powershell -NoProfile -ExecutionPolicy Bypass -File $commitScript -Mode vscode -StartAfterSeconds 1 -WaitAfterCommitSeconds 6 -LogPath $log
    Start-Sleep -Seconds 2
}
Write-Host 'Stability run complete.'
