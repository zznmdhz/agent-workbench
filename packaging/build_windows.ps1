$ErrorActionPreference = 'Stop'
$workspace = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$portable = [IO.Path]::GetFullPath((Join-Path $workspace '.local\package-build\AgentWorkbench'))
$portableStage = [IO.Path]::GetFullPath((Join-Path $workspace '.local\portable-stage\AgentWorkbench'))
$work = [IO.Path]::GetFullPath((Join-Path $workspace '.local\pyinstaller-work'))
$spec = [IO.Path]::GetFullPath((Join-Path $workspace '.local'))
foreach ($target in @($portable, $portableStage, $work, $spec)) {
    if (-not $target.StartsWith($workspace + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Build target escaped workspace: $target"
    }
}
Set-Location -LiteralPath $workspace
pnpm --dir web install --frozen-lockfile
if ($LASTEXITCODE -ne 0) { throw 'pnpm install failed' }
pnpm --dir web build
if ($LASTEXITCODE -ne 0) { throw 'web build failed' }
uv sync --frozen --group build
if ($LASTEXITCODE -ne 0) { throw 'uv sync failed' }
uv run --frozen --group build pyinstaller --noconfirm --onedir --contents-directory _internal `
    --windowed --name AgentWorkbench --hidden-import awb.cli --add-data "$workspace\web\dist:web/dist" `
    --distpath (Join-Path $workspace '.local\package-build') `
    --workpath $work --specpath $spec packaging/desktop_entrypoint.py
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed' }
uv run --frozen --group build pyinstaller --noconfirm --onedir --contents-directory _internal `
    --console --name AgentWorkbenchCLI --add-data "$workspace\web\dist:web/dist" `
    --distpath (Join-Path $workspace '.local\package-build-cli') `
    --workpath (Join-Path $workspace '.local\pyinstaller-cli-work') `
    --specpath $spec packaging/entrypoint.py
if ($LASTEXITCODE -ne 0) { throw 'CLI build failed' }
Copy-Item -LiteralPath (Join-Path $workspace '.local\package-build-cli\AgentWorkbenchCLI\AgentWorkbenchCLI.exe') -Destination $portable -Force
uv run --frozen --group build pyinstaller --noconfirm --onedir --contents-directory _internal `
    --windowed --name AgentWorkbenchReset --hidden-import awb.cli --add-data "$workspace\web\dist:web/dist" `
    --distpath (Join-Path $workspace '.local\package-build-reset') `
    --workpath (Join-Path $workspace '.local\pyinstaller-reset-work') `
    --specpath $spec packaging/reset_entrypoint.py
if ($LASTEXITCODE -ne 0) { throw 'Reset launcher build failed' }
Copy-Item -LiteralPath (Join-Path $workspace '.local\package-build-reset\AgentWorkbenchReset\AgentWorkbenchReset.exe') -Destination $portable -Force
if (Test-Path -LiteralPath $portableStage) {
    if (-not $portableStage.StartsWith($workspace + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) { throw 'Portable stage escaped workspace' }
    Remove-Item -LiteralPath $portableStage -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $portableStage | Out-Null
Copy-Item -Path (Join-Path $portable '*') -Destination $portableStage -Recurse -Force
New-Item -ItemType File -Path (Join-Path $portableStage 'portable.flag') -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'README-PORTABLE.md') -Destination $portableStage
Copy-Item -LiteralPath (Join-Path $workspace 'LICENSE') -Destination $portableStage
Copy-Item -LiteralPath (Join-Path $workspace 'docs\third_party\CC_SWITCH_LICENSE.txt') -Destination (Join-Path $portableStage 'THIRD-PARTY-CC-SWITCH-LICENSE.txt')
$archive = Join-Path $workspace 'dist\AgentWorkbench-Windows-portable-0.3.0.zip'
New-Item -ItemType Directory -Force -Path (Split-Path $archive) | Out-Null
Compress-Archive -LiteralPath $portableStage -DestinationPath $archive -CompressionLevel Optimal -Force
Get-FileHash -Algorithm SHA256 -LiteralPath $archive | Select-Object Path, Hash
$compiler = Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe'
if (-not (Test-Path -LiteralPath $compiler)) {
    $compiler = 'C:\Program Files (x86)\Inno Setup 6\ISCC.exe'
}
if (-not (Test-Path -LiteralPath $compiler)) { throw 'Inno Setup 6 compiler not found' }
& $compiler (Join-Path $PSScriptRoot 'AgentWorkbench.iss')
if ($LASTEXITCODE -ne 0) { throw 'Installer build failed' }
Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $workspace 'dist\AgentWorkbench-Setup-0.3.0-Windows-x64.exe') | Select-Object Path, Hash
