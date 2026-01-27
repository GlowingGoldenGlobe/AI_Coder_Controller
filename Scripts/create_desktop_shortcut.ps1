param(
    [string]$ShortcutName = "AI_Coder_Controller.lnk"
)

$desktop = [Environment]::GetFolderPath('Desktop')
$shortcutPath = Join-Path $desktop $ShortcutName
$root = Split-Path $PSScriptRoot -Parent
$python = Join-Path $root "Scripts\python.exe"

if (-not (Test-Path $python)) {
    Write-Error "Python executable not found at: $python"
    exit 1
}

try {
    if (Test-Path $shortcutPath) { Remove-Item $shortcutPath -Force }
    $wsh = New-Object -ComObject WScript.Shell
    $sc = $wsh.CreateShortcut($shortcutPath)
    $sc.TargetPath = $python
    $sc.Arguments = "-m src.main"
    $sc.WorkingDirectory = $root
    $sc.IconLocation = "$python,0"
    $sc.Description = "Launch AI_Coder_Controller"
    $sc.Save()
    Write-Output "Shortcut created at: $shortcutPath"
} catch {
    Write-Error $_
    exit 1
}
