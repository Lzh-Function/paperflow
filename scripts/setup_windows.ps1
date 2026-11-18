param(
    [string]$Python = '3.12',
    [switch]$Dev,
    [switch]$NoLaunch,
    [switch]$NoDesktopShortcut
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
$project = Split-Path -Parent $PSScriptRoot
$location = Get-Location
try {
    if ([Environment]::OSVersion.Platform -ne 'Win32NT') { throw 'Windowsで実行してください。' }
    if ([IO.Path]::GetPathRoot($project) -ne 'C:\') { throw 'PaperFlowのフォルダをC:内に置いてください。' }
    if ($project -match '(^|[\\/])\.obsidian([\\/]|$)') { throw '.obsidian内にはインストールできません。' }
    Set-Location -LiteralPath $project
    $env:PYTHONUTF8 = '1'
    $env:UV_PROJECT_ENVIRONMENT = Join-Path $project '.venv-win'
    $env:UV_PYTHON_INSTALL_DIR = Join-Path $project '.tools\python'
    $env:UV_CACHE_DIR = Join-Path $project '.tools\cache'
    $uvVersion = '0.13.8'
    $uv = $null
    $candidates = @(
        (Join-Path $project '.venv-win\Scripts\uv.exe'),
        (Join-Path $project '.tools\uv\uv.exe')
    )
    $command = Get-Command uv.exe -CommandType Application -ErrorAction SilentlyContinue
    if ($command) { $candidates += $command.Source }
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            $version = & $candidate --version
            if ($LASTEXITCODE -eq 0 -and $version -match "^uv $([regex]::Escape($uvVersion))(\s|$)") {
                $uv = $candidate
                break
            }
        }
    }
    if (-not $uv) {
        Write-Host '[1/4] セットアップ用uvを取得しています。'
        $architecture = $env:PROCESSOR_ARCHITECTURE
        if ($env:PROCESSOR_ARCHITEW6432) { $architecture = $env:PROCESSOR_ARCHITEW6432 }
        if ($architecture -eq 'AMD64') {
            $archive = 'uv-x86_64-pc-windows-msvc.zip'
            $expectedHash = '529e78a9cec6cd7e3e014f7ecd8bda0c113969d4ed0f2efeedb85c30943325b8'
        } elseif ($architecture -eq 'ARM64') {
            $archive = 'uv-aarch64-pc-windows-msvc.zip'
            $expectedHash = '59e6d919bed1f72035e622a7c930058f08a8931c4c78ceaf9f56e7fb74dd2c48'
        } else { throw '64bit版Windowsが必要です。' }
        $tools = Join-Path $project '.tools'
        New-Item -ItemType Directory -Force -Path $tools | Out-Null
        $download = Join-Path $tools ('download-' + [guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path $download | Out-Null
        try {
            [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
            $zip = Join-Path $download 'uv.zip'
            Invoke-WebRequest -UseBasicParsing -Uri "https://github.com/astral-sh/uv/releases/download/$uvVersion/$archive" -OutFile $zip
            if ((Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expectedHash) {
                throw 'uvのSHA-256が一致しません。実行せず停止します。'
            }
            Expand-Archive -LiteralPath $zip -DestinationPath (Join-Path $download 'unpacked')
            $binary = @(Get-ChildItem -LiteralPath (Join-Path $download 'unpacked') -Recurse -Filter uv.exe -File)
            if ($binary.Count -ne 1) { throw 'uv.exeを特定できません。' }
            $destination = Join-Path $tools 'uv'
            New-Item -ItemType Directory -Force -Path $destination | Out-Null
            $uv = Join-Path $destination 'uv.exe'
            Copy-Item -LiteralPath $binary[0].FullName -Destination $uv -Force
        } finally { Remove-Item -LiteralPath $download -Recurse -Force }
    }
    Write-Host '[2/4] Windows用Pythonと固定依存ライブラリを準備しています。'
    $syncArgs = @('sync', '--frozen', '--inexact', '--python', $Python)
    if (-not $Dev) { $syncArgs += '--no-dev' }
    & $uv @syncArgs
    if ($LASTEXITCODE -ne 0) { throw '環境構築に失敗しました。接続と上記エラーを確認して再実行してください。' }
    $pythonExe = Join-Path $project '.venv-win\Scripts\python.exe'
    Write-Host '[3/4] 初期設定と画面ライブラリを確認しています。'
    & $pythonExe -B -X utf8 -m paperflow.setup --project $project
    if ($LASTEXITCODE -ne 0) { throw '初期設定に失敗しました。上記エラーを確認してください。' }
    Write-Host '[4/4] 起動アイコンを作成しています。'
    & (Join-Path $PSScriptRoot 'create_gui_shortcut.ps1') -Desktop:(-not $NoDesktopShortcut)
    Write-Host '準備完了。GUIで保存先と使用するCLIを設定してください。'
    if (-not $NoLaunch) {
        $pythonw = Join-Path $project '.venv-win\Scripts\pythonw.exe'
        $launchArgs = '-X utf8 -m paperflow.gui --config "' + (Join-Path $project 'config.production.yaml') + '" --setup'
        Start-Process -FilePath $pythonw -WorkingDirectory $project -ArgumentList $launchArgs
    }
} catch {
    Write-Host ('セットアップを中断しました：' + $_.Exception.Message) -ForegroundColor Red
    Write-Host '既存の設定・論文DBは削除しません。修正後にsetup.cmdを再実行してください。'
    exit 1
} finally { Set-Location -LiteralPath $location.Path }
