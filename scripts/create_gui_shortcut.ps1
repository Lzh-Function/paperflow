param([switch]$Desktop)
$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $PSScriptRoot
$python = Join-Path $project '.venv-win\Scripts\pythonw.exe'
$config = Join-Path $project 'config.production.yaml'
$icon = Join-Path $project 'src\paperflow\resources\paperflow.ico'
if (-not (Test-Path $python)) { throw "Python not found: $python" }
if (-not (Test-Path $config)) { throw "Config not found: $config" }
$locations = @($project)
if ($Desktop) { $locations += [Environment]::GetFolderPath('Desktop') }
$shell = New-Object -ComObject WScript.Shell
foreach ($location in $locations) {
    $path = Join-Path $location 'PaperFlow.lnk'
    if (Test-Path $path) {
        $existing = $shell.CreateShortcut($path)
        if ($existing.TargetPath -ne $python) { throw "Existing shortcut has another target: $path" }
    }
    $shortcut = $shell.CreateShortcut($path)
    $shortcut.TargetPath = $python
    $shortcut.Arguments = '-X utf8 -m paperflow.gui --config "' + $config + '"'
    $shortcut.WorkingDirectory = $project
    $shortcut.IconLocation = $icon + ',0'
    $shortcut.Description = 'PaperFlow - Paperpile to Obsidian'
    $shortcut.Save()
    Write-Output $path
}
