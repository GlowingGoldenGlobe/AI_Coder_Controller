param(
    [string]$Out,
    [string]$OutDir,
    [int]$Seconds = 0,
    [int]$Fps = 1,
    [switch]$StampTime,
    [string]$OutVideo,
    [string]$OutGif,
    [switch]$KeepFrames,
    [string]$FfmpegExe
)

# Ensure required .NET assemblies are available (Windows only)
try {
    Add-Type -AssemblyName System.Drawing -ErrorAction Stop
    Add-Type -AssemblyName System.Windows.Forms -ErrorAction Stop
} catch {
    Write-Error "Failed to load required .NET assemblies (System.Drawing, System.Windows.Forms). This script requires Windows PowerShell on Windows. Error: $_"
    exit 1
}

function New-TimestampString {
    Get-Date -Format "yyyyMMdd_HHmmss_fff"
}

function Get-VirtualScreenBounds {
    $vs = [System.Windows.Forms.SystemInformation]::VirtualScreen
    return [PSCustomObject]@{
        Left   = $vs.Left
        Top    = $vs.Top
        Width  = $vs.Width
        Height = $vs.Height
    }
}

function Save-Screenshot {
    param(
        [string]$Path,
        [switch]$StampTime
    )

    $bounds = Get-VirtualScreenBounds

    $bmp = New-Object System.Drawing.Bitmap $bounds.Width, $bounds.Height
    $gfx = [System.Drawing.Graphics]::FromImage($bmp)
    $gfx.CopyFromScreen($bounds.Left, $bounds.Top, 0, 0, $bmp.Size)

    if ($StampTime) {
        $stamp = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss.fff')
        $font = New-Object System.Drawing.Font('Segoe UI', 16, [System.Drawing.FontStyle]::Bold)
        $brushBg = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(160, 0, 0, 0))
        $brushFg = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::White)
        $padding = 8
        $size = $gfx.MeasureString($stamp, $font)

        $w = [single]($size.Width + ($padding * 2))
        $h = [single]($size.Height + ($padding * 2))
        $rect = [System.Drawing.RectangleF]::new([single]$padding, [single]$padding, $w, $h)
        $gfx.FillRectangle($brushBg, $rect)

        $textX = [single]($padding * 2)
        $textY = [single]($padding * 2)
        $gfx.DrawString($stamp, $font, $brushFg, $textX, $textY)

        $brushBg.Dispose(); $brushFg.Dispose(); $font.Dispose()
    }

    $dir = Split-Path -Parent $Path
    if ($dir -and -not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }

    $bmp.Save($Path, [System.Drawing.Imaging.ImageFormat]::Png)

    $gfx.Dispose(); $bmp.Dispose()
}

# Resolve defaults
if (-not $Out -and $Seconds -le 0) {
    $Out = Join-Path -Path (Join-Path -Path (Get-Location) -ChildPath 'logs/screens') -ChildPath ("screen_" + (New-TimestampString) + ".png")
}

if ($Seconds -gt 0 -and -not $OutDir) {
    $OutDir = Join-Path -Path (Join-Path -Path (Get-Location) -ChildPath 'logs/screens') -ChildPath ("rec_" + (New-TimestampString))
}

if ($Out) {
    $outParent = Split-Path -Parent $Out
    if ($outParent -and -not (Test-Path $outParent)) { New-Item -ItemType Directory -Force -Path $outParent | Out-Null }
}

if ($OutDir) {
    if (-not (Test-Path $OutDir)) { New-Item -ItemType Directory -Force -Path $OutDir | Out-Null }
}

if ($Seconds -le 0) {
    # Single screenshot mode
    Write-Host "Capturing full virtual screen to: $Out"
    Save-Screenshot -Path $Out -StampTime:$StampTime
    Write-Host "Saved: $Out"
    exit 0
}

# Recording mode (time-lapse frames)
if ($Fps -lt 1) { $Fps = 1 }
$intervalMs = [int](1000 / $Fps)
$totalFrames = [int]($Seconds * $Fps)

Write-Host "Recording $totalFrames frame(s) at $Fps fps for $Seconds second(s) into: $OutDir"

$sw = [System.Diagnostics.Stopwatch]::StartNew()
for ($i = 0; $i -lt $totalFrames; $i++) {
    $path = Join-Path $OutDir ("frame_" + $i.ToString('D5') + ".png")

    Save-Screenshot -Path $path -StampTime:$StampTime

    # Maintain cadence
    $elapsedThis = $sw.ElapsedMilliseconds
    $targetNext = ($i + 1) * $intervalMs
    $sleepMs = [int]($targetNext - $elapsedThis)
    if ($sleepMs -gt 0) { Start-Sleep -Milliseconds $sleepMs }
}
$sw.Stop()

Write-Host "Recording complete. Frames saved to: $OutDir"

# Optional rendering via ffmpeg
$wantsVideo = [string]::IsNullOrWhiteSpace($OutVideo) -eq $false
$wantsGif = [string]::IsNullOrWhiteSpace($OutGif) -eq $false

if ($wantsVideo -or $wantsGif) {
    $ffmpeg = $null
    if ($FfmpegExe) {
        if (Test-Path $FfmpegExe) { $ffmpeg = $FfmpegExe } else { Write-Warning "Provided -FfmpegExe not found: $FfmpegExe" }
    }
    if (-not $ffmpeg) {
        $cmd = Get-Command ffmpeg -ErrorAction SilentlyContinue
        if ($cmd) { $ffmpeg = $cmd.Source }
    }

    if (-not $ffmpeg) {
        Write-Warning "ffmpeg not found in PATH and -FfmpegExe not provided. Skipping render. Frames remain in $OutDir"
        exit 0
    }

    Push-Location $OutDir
    try {
        if ($wantsVideo) {
            $absVideo = Resolve-Path -LiteralPath $OutVideo -ErrorAction SilentlyContinue
            if (-not $absVideo) {
                $videoParent = Split-Path -Parent $OutVideo
                if ($videoParent -and -not (Test-Path $videoParent)) { New-Item -ItemType Directory -Force -Path $videoParent | Out-Null }
            }
            Write-Host "Rendering MP4 via ffmpeg -> $OutVideo"
            & $ffmpeg -y -loglevel error -framerate $Fps -i "frame_%05d.png" -pix_fmt yuv420p -vf "pad=ceil(iw/2)*2:ceil(ih/2)*2" "$OutVideo"
            if ($LASTEXITCODE -ne 0) { Write-Warning "ffmpeg MP4 render failed with exit code $LASTEXITCODE" } else { Write-Host "MP4 written: $OutVideo" }
        }

        if ($wantsGif) {
            $palette = "palette.png"
            Write-Host "Generating GIF palette"
            & $ffmpeg -y -loglevel error -i "frame_%05d.png" -vf "palettegen=stats_mode=diff" $palette
            if ($LASTEXITCODE -eq 0) {
                Write-Host "Rendering GIF via palette -> $OutGif"
                & $ffmpeg -y -loglevel error -framerate $Fps -i "frame_%05d.png" -i $palette -lavfi "paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle" "$OutGif"
                if ($LASTEXITCODE -ne 0) { Write-Warning "ffmpeg GIF render failed with exit code $LASTEXITCODE" } else { Write-Host "GIF written: $OutGif" }
            } else {
                Write-Warning "Palette generation failed; skipping GIF render"
            }
            if (Test-Path $palette) { Remove-Item $palette -Force -ErrorAction SilentlyContinue }
        }
    }
    finally {
        Pop-Location
    }

    if (-not $KeepFrames) {
        Write-Host "Cleaning up frames in $OutDir"
        Remove-Item (Join-Path $OutDir "frame_*.png") -Force -ErrorAction SilentlyContinue
    }
}