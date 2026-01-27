param(
    [int]$Count = 100,
    [int]$WaitAfterCommitSeconds = 6
)
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir
$log = Join-Path $scriptDir "..\logs\actions\commit_verify_stability_100.log"
if (Test-Path $log) { Remove-Item -Force $log }
for ($i = 1; $i -le $Count; $i++) {
    Write-Host "--- Run $i / $Count ---"
    $commitScript = Join-Path $scriptDir 'commit_and_verify_2plus2.ps1'
    $start = Get-Date
    & powershell -NoProfile -ExecutionPolicy Bypass -File $commitScript -Mode vscode -StartAfterSeconds 1 -WaitAfterCommitSeconds $WaitAfterCommitSeconds -LogPath $log
    $end = Get-Date
    $dur = ($end - $start).TotalSeconds
    Add-Content -Path $log -Value ("[" + (Get-Date).ToString('s') + "] Run $i completed in $dur seconds")
    Start-Sleep -Seconds 1
}
Write-Host 'Extended stability run complete.'
