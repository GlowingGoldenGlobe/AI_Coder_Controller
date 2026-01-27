param(
    # Commit loop params (forwarded to copilot_commit_start.ps1)
    [ValidateSet('app','vscode')]
    [string]$Mode = 'app',
    [int]$StartAfterSeconds = 1,
    [int]$RepeatSeconds = 10,
    [int]$RepeatCount = 1,
    [int]$IdleMinMs = 0,
    [int]$MaxWaitSeconds = 0,
    [string]$Message = 'Monitoring commit loop…',
    [string]$Title = 'Copilot',
    [string]$LogPath = 'logs/actions/copilot_commit.log',
    [int]$OcrGateSeconds = 0,

    # Recording options
    [switch]$Record = $true,
    [int]$RecordSeconds = 0,
    [int]$RecordFps = 12,
    [ValidateSet('auto','dxcam','mss')]
    [string]$RecordBackend = 'mss',
    [double]$RecordScale = 1.0,
    [string]$RecordRegion = '', # format: x,y,w,h
    [string]$RecordOut = '',

    # Control
    [switch]$Wait,
    [switch]$AllowProtocolFallback = $false
)

$ErrorActionPreference = 'Stop'

function New-TimestampString {
    Get-Date -Format "yyyyMMdd_HHmmss"
}

# Resolve paths
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$scriptsDir = Join-Path $root 'scripts'
$commitStart = Join-Path $scriptsDir 'copilot_commit_start.ps1'
$monitorPy = Join-Path $scriptsDir 'monitor_live.py'
$pythonExe = Join-Path $root 'Scripts/python.exe'

if (-not (Test-Path $commitStart)) { throw "Missing commit starter: $commitStart" }
if ($Record -and -not (Test-Path $monitorPy)) { throw "Missing monitor script: $monitorPy" }
if ($Record -and -not (Test-Path $pythonExe)) { throw "Python interpreter not found: $pythonExe" }

# Compute default RecordOut and RecordSeconds when needed
if ($Record) {
    if (-not $RecordOut) {
        $RecordOut = Join-Path $root (Join-Path 'logs/screens' ("commit_rec_" + (New-TimestampString) + ".mp4"))
    }
    if ($RecordSeconds -le 0) {
        if ($RepeatCount -gt 0) {
            $RecordSeconds = [int]($StartAfterSeconds + ($RepeatSeconds * $RepeatCount) + 3)
        } else {
            Write-Warning "RepeatCount is 0 (infinite). Provide -RecordSeconds to enable recording; skipping recording for now."
            $Record = $false
        }
    }
}

# Ensure log and output dirs exist
$logFull = Join-Path $root $LogPath
$logDir = Split-Path -Parent $logFull
if ($logDir -and -not (Test-Path $logDir)) { New-Item -ItemType Directory -Force -Path $logDir | Out-Null }
if ($Record) {
    $recDir = Split-Path -Parent $RecordOut
    if ($recDir -and -not (Test-Path $recDir)) { New-Item -ItemType Directory -Force -Path $recDir | Out-Null }
}

# Build commit args
$commitArgs = @(
    "-Mode", $Mode,
    "-StartAfterSeconds", $StartAfterSeconds,
    "-RepeatSeconds", $RepeatSeconds,
    "-RepeatCount", $RepeatCount,
    "-Message", $Message,
    "-Title", $Title,
    "-LogPath", $LogPath
)
if ($OcrGateSeconds -gt 0) { $commitArgs += @('-OcrGateSeconds', $OcrGateSeconds) }
if ($IdleMinMs -gt 0) { $commitArgs += @('-IdleMinMs', $IdleMinMs) }
if ($MaxWaitSeconds -gt 0) { $commitArgs += @('-MaxWaitSeconds', $MaxWaitSeconds) }
if ($AllowProtocolFallback) { $commitArgs += @('-AllowProtocolFallback') }

# Start commit loop (new PowerShell)
Write-Host "Launching commit loop via $commitStart"
$commitAllArgs = @('-NoProfile','-ExecutionPolicy','Bypass','-File', $commitStart) + $commitArgs
$commitProc = Start-Process -FilePath "powershell" -ArgumentList $commitAllArgs -PassThru

# Start recording (if enabled)
$recProc = $null
if ($Record) {
    $recArgs = @('--seconds', $RecordSeconds, '--fps', $RecordFps, '--out', $RecordOut, '--backend', $RecordBackend, '--mark-assessed')
    if ($RecordScale -ne 1.0) { $recArgs += @('--scale', $RecordScale) }
    if ($RecordRegion) { $recArgs += @('--region', $RecordRegion) }
    Write-Host "Launching recorder: $RecordOut ($RecordSeconds s @ $RecordFps fps, backend=$RecordBackend)"
    $recAllArgs = @($monitorPy) + $recArgs
    $recProc = Start-Process -FilePath $pythonExe -ArgumentList $recAllArgs -PassThru
}

if ($Wait) {
    if ($recProc) { $recProc.WaitForExit() | Out-Null }
    # Create sidecar marker if output exists (redundant safety if recorder didn't mark)
    if ($Record -and $RecordOut -and (Test-Path $RecordOut)) {
        $marker = "$RecordOut.assessed"
        try { Set-Content -Path $marker -Value "assessed" -Encoding UTF8 -Force } catch {}
    }
    if ($commitProc) { $commitProc.WaitForExit() | Out-Null }
}

Write-Host "Started. Commit PID=$($commitProc.Id)"; if ($recProc) { Write-Host " Recorder PID=$($recProc.Id) -> $RecordOut" }