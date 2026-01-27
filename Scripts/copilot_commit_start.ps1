param(
    [ValidateSet("app","vscode")]
    [string]$Mode = "app",
    [int]$StartAfterSeconds = 0,
    [int]$RepeatSeconds = 10,
    [int]$RepeatCount = 0,
    [int]$IdleMinMs = 0,
    [int]$MaxWaitSeconds = 0,
    [string]$Message = "",
    [string]$Title = "",
    [string]$LogPath = "",
    [switch]$AllowProtocolFallback = $false,
    [int]$OcrGateSeconds = 0
)

# Launch an external PowerShell that runs copilot_commit.ps1 with the given args
try {
    $scriptPath = Join-Path $PSScriptRoot 'copilot_commit.ps1'
    $argsList = @(
        '-NoProfile',
        '-ExecutionPolicy','Bypass',
        '-File', $scriptPath,
        '-Mode', $Mode,
        '-StartAfterSeconds', $StartAfterSeconds,
        '-RepeatSeconds', $RepeatSeconds,
        '-RepeatCount', $RepeatCount
    )
        # Prevent duplicate loops: if a powershell already runs copilot_commit.ps1, skip
        try {
            $existing = Get-CimInstance Win32_Process -Filter "Name = 'powershell.exe'" | Where-Object { $_.CommandLine -match 'copilot_commit\.ps1' }
            if ($existing) {
                # Still launch only if caller explicitly set RepeatCount (non-zero) to create a bounded run
                if ($RepeatCount -le 0) {
                    if ($LogPath) { Add-Content -Path $LogPath -Value ("[" + (Get-Date).ToString('s') + "] SKIP duplicate loop detected") -Encoding UTF8 }
                    exit 0
                }
            }
        } catch {}

        $quotedScript = '"' + $scriptPath + '"'
        $argStr = "-NoProfile -ExecutionPolicy Bypass -File $quotedScript -Mode $Mode -StartAfterSeconds $StartAfterSeconds -RepeatSeconds $RepeatSeconds -RepeatCount $RepeatCount"
    if (-not [string]::IsNullOrWhiteSpace($Message)) {
        $argsList += @('-Message', $Message)
    }
    if (-not [string]::IsNullOrWhiteSpace($Title)) {
        $argsList += @('-Title', $Title)
    }
    if ([string]::IsNullOrWhiteSpace($LogPath)) {
        $logs = Resolve-Path (Join-Path (Split-Path $PSScriptRoot -Parent) 'logs\actions') -ErrorAction SilentlyContinue
        if (-not $logs) { $logs = Join-Path (Split-Path $PSScriptRoot -Parent) 'logs\actions' }
        try { New-Item -ItemType Directory -Path $logs -Force | Out-Null } catch {}
        $LogPath = Join-Path $logs 'copilot_commit.log'
    } else {
        if (-not [System.IO.Path]::IsPathRooted($LogPath)) {
            $LogPath = Join-Path (Split-Path $PSScriptRoot -Parent) $LogPath
        }
        $lpDir = Split-Path $LogPath -Parent
        try { if (-not (Test-Path $lpDir)) { New-Item -ItemType Directory -Path $lpDir -Force | Out-Null } } catch {}
    }
    $argsList += @('-LogPath', $LogPath)
        $qLog = '"' + $LogPath + '"'
        $argStr = $argStr + " -LogPath $qLog"
        if ($OcrGateSeconds -gt 0) { $argStr = $argStr + " -OcrGateSeconds $OcrGateSeconds" }
        if ($IdleMinMs -gt 0) { $argStr = $argStr + " -IdleMinMs $IdleMinMs" }
        if ($MaxWaitSeconds -gt 0) { $argStr = $argStr + " -MaxWaitSeconds $MaxWaitSeconds" }
        if ($AllowProtocolFallback) { $argStr = $argStr + " -AllowProtocolFallback" }
    # Write a LAUNCH line so we know the starter ran
    try { Add-Content -Path $LogPath -Value ("[" + (Get-Date).ToString('s') + "] LAUNCH Mode=$Mode StartAfter=$StartAfterSeconds RepeatSeconds=$RepeatSeconds RepeatCount=$RepeatCount Title='$Title'") -Encoding UTF8 } catch {}
        try { Add-Content -Path $LogPath -Value ("[" + (Get-Date).ToString('s') + "] LAUNCH Mode=$Mode StartAfter=$StartAfterSeconds RepeatSeconds=$RepeatSeconds RepeatCount=$RepeatCount Title='$Title'") -Encoding UTF8 } catch {}
        $p = Start-Process -FilePath 'powershell.exe' -ArgumentList $argStr -WindowStyle Normal -PassThru
    try { Add-Content -Path $LogPath -Value ("[" + (Get-Date).ToString('s') + "] LAUNCHED pid=" + $p.Id) -Encoding UTF8 } catch {}
    exit 0
} catch {
    Write-Host "Error launching external PowerShell: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
