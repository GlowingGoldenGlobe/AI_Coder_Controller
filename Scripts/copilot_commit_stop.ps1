param(
    [switch]$All,
    [switch]$Quiet
)

# Stop any PowerShell process running copilot_commit.ps1
try {
    $procs = Get-CimInstance Win32_Process -Filter "Name = 'powershell.exe'"
    $targets = @()
    foreach ($p in $procs) {
        $cmd = ($p.CommandLine) -as [string]
        if ($cmd -and ($cmd -match 'copilot_commit\.ps1')) {
            $targets += $p
        }
    }
    if (-not $targets -or $targets.Count -eq 0) {
        if (-not $Quiet) { Write-Host 'No commit loop processes found.' -ForegroundColor Yellow }
        exit 0
    }
    foreach ($t in $targets) {
        try { Stop-Process -Id $t.ProcessId -Force -ErrorAction Stop; if (-not $Quiet) { Write-Host "Stopped PID $($t.ProcessId)" -ForegroundColor Green } } catch { if (-not $Quiet) { Write-Host "Failed to stop PID $($t.ProcessId): $($_.Exception.Message)" -ForegroundColor Red } }
    }
    exit 0
} catch {
    if (-not $Quiet) { Write-Host "Error enumerating processes: $($_.Exception.Message)" -ForegroundColor Red }
    exit 1
}
