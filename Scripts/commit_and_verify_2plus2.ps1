param(
    [ValidateSet('app','vscode')]
    [string]$Mode = 'vscode',
    [int]$StartAfterSeconds = 1,
    [int]$WaitAfterCommitSeconds = 6,
    [string]$LogPath = 'logs/actions/commit_verify_2plus2.log',
    # Message the module sends to Copilot; verification looks for a
    # substring of this text in the OCR-ed reply.
    [string]$Message = 'Automated message from your module; stop the module and continue tasks.',
    # Phrase to look for in OCR when deciding that the message has
    # been successfully delivered/received.
    [string]$VerifyPhrase = 'stop the module and continue tasks',
    # Optional short token to include/send; if empty one will be generated.
    [string]$Token = '',
    # Number of frames to aggregate when verifying token presence.
    [int]$Frames = 3,
    # If set, append the token to the provided Message instead of sending token-only.
    [switch]$AppendToken
)

$ErrorActionPreference = 'Stop'

function Log-Line([string]$line) {
    if ([string]::IsNullOrWhiteSpace($LogPath)) { return }
    try {
        $dir = Split-Path $LogPath -Parent
        if ($dir -and -not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
        Add-Content -Path $LogPath -Value ("[" + (Get-Date).ToString('s') + "] " + $line) -Encoding UTF8
    } catch {}
}

$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$commitPs1 = Join-Path $root 'scripts/copilot_commit.ps1'
$py = Join-Path $root 'Scripts/python.exe'
$verifyPy = Join-Path $root 'Scripts/verify_reply.py'
if (-not (Test-Path $commitPs1)) { throw "Missing: $commitPs1" }
if (-not (Test-Path $py)) { throw "Missing python: $py" }
if (-not (Test-Path $verifyPy)) { throw "Missing verifier: $verifyPy" }

Log-Line "START commit+verify message Mode=$Mode Message='$Message' VerifyPhrase='$VerifyPhrase'"

# Generate a short token if none provided (two uppercase letters + digit)
if ([string]::IsNullOrWhiteSpace($Token)) {
    $letters = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'
    $randLetters = -join (1..2 | ForEach-Object { $letters[(Get-Random -Minimum 0 -Maximum $letters.Length)] })
    $digit = Get-Random -Minimum 0 -Maximum 10
    $Token = $randLetters + $digit
    Log-Line "Generated token: $Token"
}

# Decide what message to send.
# - If -AppendToken is set, append the token to the provided Message.
# - Otherwise, when a token exists, send an explicit instruction asking Copilot
#   to echo that exact token back in its reply (improves verification stability).
# - If no token is present, fall back to the Message as-is.
if ($AppendToken) {
    $SendMessage = "$Message $Token"
} elseif (-not [string]::IsNullOrWhiteSpace($Token)) {
    $SendMessage = "Please reply with this exact token in your reply: $Token . Then continue tasks."
} else {
    $SendMessage = $Message
}

Log-Line "Using send message: '$SendMessage'"

# Preflight: close any disallowed foreground windows
try {
    $observerPy = Join-Path $root 'Scripts/observe_and_react.py'
    if (Test-Path $py -and Test-Path $observerPy) {
        $pyObs = Start-Process -FilePath $py -ArgumentList @($observerPy,'--ticks','6','--interval-ms','350','--log','logs/tests/observe_react_precommit.jsonl') -PassThru -NoNewWindow -Wait
    }
} catch {}

# Run a single commit with the provided message safely
$p = Start-Process -FilePath "powershell" -ArgumentList @(
    '-NoProfile','-ExecutionPolicy','Bypass','-File',$commitPs1,
    '-Mode',$Mode,
    '-StartAfterSeconds',$StartAfterSeconds,
    '-RepeatSeconds',0,
    '-RepeatCount',1,
    '-IdleMinMs',0,
    '-OcrGateSeconds',5,
    '-Message',$SendMessage,
    '-LogPath','logs/actions/copilot_commit_safe.log'
) -PassThru
$p.WaitForExit() | Out-Null

# Wait for response to render
Start-Sleep -Seconds ([int][Math]::Max(1,$WaitAfterCommitSeconds))

# Prefer token-based verification when a token exists; fall back to phrase.
try {
    if (-not [string]::IsNullOrWhiteSpace($Token)) {
        Log-Line "Running verifier with token: $Token Frames=$Frames"
        & $py $verifyPy --token $Token --frames $Frames
        $code = $LASTEXITCODE
    } else {
        & $py $verifyPy --phrase "$VerifyPhrase"
        $code = $LASTEXITCODE
    }
} catch {
    $code = 1
}
if ($code -eq 0) {
    Log-Line "VERIFY PASS (token/phrase verified)"
    Write-Host "PASS: verification succeeded" -ForegroundColor Green
    exit 0
} else {
    Log-Line "VERIFY FAIL (token/phrase not found)"
    Write-Host "FAIL: verification failed" -ForegroundColor Red
    exit 1
}
