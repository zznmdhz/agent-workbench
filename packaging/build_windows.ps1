$ErrorActionPreference = 'Stop'
$workspace = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$portable = [IO.Path]::GetFullPath((Join-Path $workspace '.local\package-build\AgentWorkbench'))
$work = [IO.Path]::GetFullPath((Join-Path $workspace '.local\pyinstaller-work'))
$spec = [IO.Path]::GetFullPath((Join-Path $workspace '.local'))
foreach ($target in @($portable, $work, $spec)) {
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
    --name AgentWorkbench --add-data "$workspace\web\dist:web/dist" `
    --distpath (Join-Path $workspace '.local\package-build') `
    --workpath $work --specpath $spec packaging/entrypoint.py
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed' }
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'Start-AgentWorkbench.cmd') -Destination $portable
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'Pair-This-PC.cmd') -Destination $portable
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'Collect-This-PC.cmd') -Destination $portable
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'README-PORTABLE.md') -Destination $portable
Copy-Item -LiteralPath (Join-Path $workspace 'LICENSE') -Destination $portable
$archive = Join-Path $workspace 'dist\AgentWorkbench-Windows-preview.zip'
New-Item -ItemType Directory -Force -Path (Split-Path $archive) | Out-Null
Compress-Archive -LiteralPath $portable -DestinationPath $archive -CompressionLevel Optimal -Force
Get-FileHash -Algorithm SHA256 -LiteralPath $archive | Select-Object Path, Hash
