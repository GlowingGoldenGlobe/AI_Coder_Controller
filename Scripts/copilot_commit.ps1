param(
    [ValidateSet("app","vscode")]
    [string]$Mode = "app",
    [int]$DelayMs = 250,
    [int]$StartAfterSeconds = 0,
    [int]$RepeatSeconds = 0,
    [int]$RepeatCount = 1,
    [int]$IdleMinMs = 1000,
    [int]$MaxWaitSeconds = 30,
    [string]$Message = "",
    [string]$Title = "",
    [string]$LogPath = "",
    [switch]$AllowProtocolFallback = $false,
    [int]$OcrGateSeconds = 0
)

# Initialize logging early so failures are visible
if (-not [string]::IsNullOrWhiteSpace($LogPath)) {
    try {
        $dir = Split-Path $LogPath -Parent
        if ($dir -and -not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
        if (-not (Test-Path $LogPath)) { New-Item -ItemType File -Path $LogPath -Force | Out-Null }
        Add-Content -Path $LogPath -Value ("[" + (Get-Date).ToString('s') + "] START Mode=$Mode StartAfter=$StartAfterSeconds RepeatSeconds=$RepeatSeconds RepeatCount=$RepeatCount Title='$Title'") -Encoding UTF8
    } catch { }
}

try {
    Add-Type -AssemblyName System.Windows.Forms | Out-Null
    $wshell = New-Object -ComObject WScript.Shell
} catch {
    if (-not [string]::IsNullOrWhiteSpace($LogPath)) {
        Add-Content -Path $LogPath -Value ("[" + (Get-Date).ToString('s') + "] ERROR init COM: " + $_.Exception.Message) -Encoding UTF8
    }
    exit 1
}

# Idle detection via GetLastInputInfo
try {
$src = @'
using System;
using System.Runtime.InteropServices;
public static class IdleUtil {
    [StructLayout(LayoutKind.Sequential)]
    struct LASTINPUTINFO {
        public uint cbSize;
        public uint dwTime;
    }
    [DllImport("user32.dll")]
    static extern bool GetLastInputInfo(ref LASTINPUTINFO plii);
    public static uint GetIdleMs(){
        LASTINPUTINFO lii = new LASTINPUTINFO();
        lii.cbSize = (uint)System.Runtime.InteropServices.Marshal.SizeOf(lii);
        if(!GetLastInputInfo(ref lii)) return 0u;
        return (uint)Environment.TickCount - lii.dwTime;
    }
}
'@
Add-Type -TypeDefinition $src -ErrorAction SilentlyContinue
} catch { }
function Get-IdleMs { try { return [IdleUtil]::GetIdleMs() } catch { return 0 } }

# Foreground window helpers (title/class) for focus gating diagnostics
try {
$fgSrc = @'
using System;
using System.Text;
using System.Runtime.InteropServices;
public static class FgWin {
    [DllImport("user32.dll")] static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll", SetLastError=true, CharSet=CharSet.Auto)] static extern int GetWindowText(IntPtr hWnd, StringBuilder lpString, int nMaxCount);
    [DllImport("user32.dll", SetLastError=true, CharSet=CharSet.Auto)] static extern int GetClassName(IntPtr hWnd, StringBuilder lpClassName, int nMaxCount);
    public static string[] Info(){
        var h = GetForegroundWindow();
        var sb = new StringBuilder(512);
        var sc = new StringBuilder(256);
        GetWindowText(h, sb, sb.Capacity);
        GetClassName(h, sc, sc.Capacity);
        return new string[]{ sb.ToString() ?? string.Empty, sc.ToString() ?? string.Empty };
    }
}
'@
Add-Type -TypeDefinition $fgSrc -ErrorAction SilentlyContinue
} catch { }
function Get-FgInfo {
    try { $arr = [FgWin]::Info(); return @{ title = ($arr[0] -as [string]); class = ($arr[1] -as [string]) } } catch { return @{ title = ''; class = '' } }
}

# Foreground process name helper to positively identify VS Code vs browsers
try {
$fgProcSrc = @'
using System;
using System.Text;
using System.Runtime.InteropServices;
using System.Diagnostics;
public static class FgProc {
    [DllImport("user32.dll")] static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint lpdwProcessId);
    public static string Name(){
        var h = GetForegroundWindow();
        if (h == IntPtr.Zero) return string.Empty;
        uint pid; GetWindowThreadProcessId(h, out pid);
        try { var p = Process.GetProcessById((int)pid); return (p?.ProcessName) ?? string.Empty; } catch { return string.Empty; }
    }
}
'@
Add-Type -TypeDefinition $fgProcSrc -ErrorAction SilentlyContinue
} catch { }
function Get-FgProcName { try { return ([FgProc]::Name() + '') } catch { return '' } }

function Wait-Sleep([int]$ms) {
    Start-Sleep -Milliseconds $ms
}

function Log-Line([string]$line) {
    if (-not [string]::IsNullOrWhiteSpace($LogPath)) {
        try { Add-Content -Path $LogPath -Value ("[" + (Get-Date).ToString('s') + "] " + $line) -Encoding UTF8 } catch {}
    }
}

function Write-ErrorEvent([string]$type, [string]$message, [hashtable]$data) {
    try {
        $root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
        $errDir = Join-Path $root 'logs/errors'
        if (-not (Test-Path $errDir)) { New-Item -ItemType Directory -Path $errDir -Force | Out-Null }
        $path = Join-Path $errDir 'events.jsonl'
        $obj = [ordered]@{
            ts = (Get-Date).ToString('s')
            source = 'copilot_commit.ps1'
            type = $type
            message = $message
            data = $data
        }
        $json = $obj | ConvertTo-Json -Depth 6 -Compress
        Add-Content -Path $path -Value $json -Encoding UTF8
    } catch { }
}

function Send-Keys([string]$keys) {
    $wshell.SendKeys($keys)
    Wait-Sleep -ms $DelayMs
}

function Send-Text([string]$text) {
    if ([string]::IsNullOrWhiteSpace($text)) { return }
    # Minimal escaping for braces used by SendKeys
    $escaped = $text.Replace("{", "{{}").Replace("}", "{}}")
    $wshell.SendKeys($escaped)
    Wait-Sleep -ms $DelayMs
}

function Focus-Target() {
    $t = $Title
    if ([string]::IsNullOrWhiteSpace($t)) {
        $t = if ($Mode -eq 'app') { 'Copilot' } else { 'Visual Studio Code' }
    }
    if ($Mode -eq 'app') {
        $candidates = @()
        if (-not [string]::IsNullOrWhiteSpace($Title)) { $candidates += $Title }
        $candidates += @('Copilot','Microsoft Copilot','Copilot (Preview)')
        $activated = $false
        foreach ($cand in $candidates) {
            try { $activated = [bool]($wshell.AppActivate($cand)) } catch { $activated = $false }
            if ($activated) { Log-Line "FOCUS ok title='$cand'"; break } else { Log-Line "FOCUS miss title='$cand'" }
        }
        if (-not $activated) {
            if ($AllowProtocolFallback) {
                # Fallback: try to open Copilot via protocol handler
                try {
                    Start-Process "ms-copilot:" | Out-Null
                    Wait-Sleep 800
                    foreach ($cand in $candidates) {
                        try { $activated = [bool]($wshell.AppActivate($cand)) } catch { $activated = $false }
                        if ($activated) { Log-Line "FOCUS ok after ms-copilot title='$cand'"; break } else { Log-Line "FOCUS miss after ms-copilot title='$cand'" }
                    }
                } catch { Log-Line ("FOCUS fallback error: " + $_.Exception.Message) }
            } else {
                Log-Line "FOCUS fallback disabled (no ms-copilot launch). Will skip sending keys this iteration."
            }
        }
        Wait-Sleep 300
        return $activated
    }
    # VS Code focus
    $cvc = @()
    if (-not [string]::IsNullOrWhiteSpace($Title)) { $cvc += $Title }
    $cvc += @('Visual Studio Code','Code')
    $vok = $false
    foreach ($cand in $cvc) {
        try { $vok = [bool]($wshell.AppActivate($cand)) } catch { $vok = $false }
        if ($vok) { Log-Line "FOCUS ok title='$cand'"; break } else { Log-Line "FOCUS miss title='$cand'" }
    }
    if (-not $vok) {
        Log-Line "FOCUS vscode failed; will skip sending keys this iteration."
        Write-ErrorEvent -type 'focus_failed' -message 'Could not focus VS Code' -data @{ mode = $Mode; title = $Title }
        return $false
    }
    Wait-Sleep 300
    # Log current foreground info for diagnostics
    $fg = Get-FgInfo
    if ($fg.title) { Log-Line ("FOREGROUND title='" + $fg.title + "' class='" + $fg.class + "'") }
    $pname = (Get-FgProcName).ToLower()
    if ($pname -notmatch 'code') {
        Log-Line ("FOREGROUND process is not Code.exe (got '" + $pname + "'); skipping palette focus")
        Write-ErrorEvent -type 'foreground_process_not_vscode' -message 'Process guarding blocked palette focus' -data @{ proc = $pname; title = $fg.title; class = $fg.class }
        return $false
    }
    # Ctrl+Shift+P to open palette and try to focus chat
    Send-Keys('^+p')
    Wait-Sleep 350
    $cmds = @(
        'GitHub Copilot Chat: Focus on Chat View',
        'Open View: GitHub Copilot Chat',
        'View: Focus on Chat'
    )
    foreach ($c in $cmds) {
        $wshell.SendKeys($c)
        Wait-Sleep 250
        Send-Keys('{ENTER}')
        Wait-Sleep 500
        Log-Line "FOCUS vscode chat via palette command='$c'"
    }
    return $true
}

function Run-GatherEvidence() {
    try {
        $root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
        $pythonExe = Join-Path $root 'Scripts/python.exe'
        $gatherPy = Join-Path $root 'Scripts/gather_chat_evidence.py'
        if ((Test-Path $pythonExe) -and (Test-Path $gatherPy)) {
            $proc = Start-Process -FilePath $pythonExe -ArgumentList @($gatherPy) -PassThru -NoNewWindow -Wait
            Log-Line ("EVIDENCE exit=" + $proc.ExitCode)
        }
    } catch { Log-Line ("EVIDENCE error: " + $_.Exception.Message) }
}

try {
    if ($StartAfterSeconds -gt 0) { Start-Sleep -Seconds $StartAfterSeconds }
    # Resolve project root and Python for optional OCR gate
    $root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
    $pythonExe = Join-Path $root 'Scripts/python.exe'
    $ocrGatePy = Join-Path $root 'Scripts/ocr_gate_chat_ready.py'
    $count = [int]$RepeatCount
    $i = 0
    do {
        $focusOk = Focus-Target
        # Guard: if the foreground looks like a browser, do not proceed
        $fg = Get-FgInfo
        $titleLc = ($fg.title + '').ToLower()
        if (($titleLc -match 'edge' -or $titleLc -match 'chrome') -and ($titleLc -notmatch 'copilot')) {
            Log-Line ("SKIP iteration: foreground appears to be browser title='" + $fg.title + "'")
            Write-ErrorEvent -type 'browser_foreground_detected' -message 'Foreground appears to be browser; skipping' -data @{ title = $fg.title; class = $fg.class }
            # Attempt proactive remediation by invoking the observer to close disallowed foreground
            try {
                $root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
                $pythonExe = Join-Path $root 'Scripts/python.exe'
                $observerPy = Join-Path $root 'Scripts/observe_and_react.py'
                if (Test-Path $pythonExe -and (Test-Path $observerPy)) {
                    Start-Process -FilePath $pythonExe -ArgumentList @($observerPy,'--ticks','4','--interval-ms','350','--log','logs/tests/observe_react_autoclose.jsonl') -PassThru -NoNewWindow -Wait | Out-Null
                }
            } catch { }
            $i++
            if (($RepeatSeconds -gt 0) -and (($RepeatCount -le 0) -or ($i -lt $RepeatCount))) { Start-Sleep -Seconds $RepeatSeconds }
            Run-GatherEvidence
            continue
        }
        # Nudge focus to input (best-effort)
        if (-not $focusOk) {
            Log-Line "SKIP iteration: focus not confirmed"
            $i++
            if (($RepeatSeconds -gt 0) -and (($RepeatCount -le 0) -or ($i -lt $RepeatCount))) { Start-Sleep -Seconds $RepeatSeconds }
            continue
        }
        # Optional OCR gate: ensure chat region is ready (VS Code mode only)
        if (($Mode -eq 'vscode') -and ($OcrGateSeconds -gt 0) -and (Test-Path $pythonExe) -and (Test-Path $ocrGatePy)) {
            $deadline = (Get-Date).AddSeconds([double]$OcrGateSeconds)
            $ready = $false
            while (-not $ready -and (Get-Date) -lt $deadline) {
                try {
                    $proc = Start-Process -FilePath $pythonExe -ArgumentList @($ocrGatePy) -PassThru -NoNewWindow -Wait
                    $ready = ($proc.ExitCode -eq 0)
                } catch { $ready = $false }
                if (-not $ready) { Start-Sleep -Milliseconds 700 }
            }
            if (-not $ready) {
                Log-Line "SKIP iteration: OCR gate not ready within ${OcrGateSeconds}s"
                $i++
                if (($RepeatSeconds -gt 0) -and (($RepeatCount -le 0) -or ($i -lt $RepeatCount))) { Start-Sleep -Seconds $RepeatSeconds }
                continue
            } else {
                Log-Line "OCR gate passed"
            }
        }
        Send-Keys('{TAB}')
        Send-Keys('{TAB}')
        # Wait for user idle if requested
        if ($IdleMinMs -gt 0) {
            $deadline = (Get-Date).AddSeconds([double]([Math]::Max(0, $MaxWaitSeconds)))
            while ((Get-IdleMs) -lt [Math]::Max(0, $IdleMinMs)) {
                if ((Get-Date) -ge $deadline) { Log-Line "IDLE wait timeout; skipping send this iteration"; break }
                Wait-Sleep 250
            }
        }
        $isIdle = (Get-IdleMs) -ge [Math]::Max(0, $IdleMinMs)
        # Optional message input
        if ($isIdle) {
            # Final guard: verify VS Code still in foreground
            $fg2 = Get-FgInfo
            $t2 = ($fg2.title + '').ToLower()
            if ($t2 -notmatch 'visual studio code') {
                Log-Line ("SKIP send: foreground not VS Code title='" + $fg2.title + "'")
                Write-ErrorEvent -type 'foreground_not_vscode_before_send' -message 'Blocked send: foreground is not VS Code' -data @{ title = $fg2.title; class = $fg2.class }
                $i++
                if (($RepeatSeconds -gt 0) -and (($RepeatCount -le 0) -or ($i -lt $RepeatCount))) { Start-Sleep -Seconds $RepeatSeconds }
                Run-GatherEvidence
                continue
            }
            if (-not [string]::IsNullOrWhiteSpace($Message)) {
                Send-Text($Message)
            }
            # Commit current input: Ctrl+Enter then Enter
            Send-Keys('^({ENTER})')
            Send-Keys('{ENTER}')
            if (-not [string]::IsNullOrWhiteSpace($LogPath)) {
                Add-Content -Path $LogPath -Value ("[" + (Get-Date).ToString('s') + "] COMMIT iteration=" + ($i+1)) -Encoding UTF8
            }
            # Gather evidence after commit
            Run-GatherEvidence
        } else {
            Log-Line "SKIP send due to active user"
            Run-GatherEvidence
        }
        $i++
        if (($RepeatSeconds -gt 0) -and (($RepeatCount -le 0) -or ($i -lt $RepeatCount))) {
            Start-Sleep -Seconds $RepeatSeconds
        }
    } while (($RepeatCount -le 0) -or ($i -lt $RepeatCount))
} catch {
    if (-not [string]::IsNullOrWhiteSpace($LogPath)) {
        Add-Content -Path $LogPath -Value ("[" + (Get-Date).ToString('s') + "] ERROR runtime: " + $_.Exception.Message) -Encoding UTF8
    }
    Write-Host "Error: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
exit 0
