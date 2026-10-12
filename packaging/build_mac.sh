#!/bin/sh
set -eu

workspace=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$workspace"
uv run python -c 'import json,subprocess; from datetime import datetime,timezone; from pathlib import Path; from awb import __version__; Path("src/awb/build.json").write_text(json.dumps({"version":__version__,"commit":subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip(),"built_at":datetime.now(timezone.utc).isoformat()}))'

pnpm --dir web install --frozen-lockfile
pnpm --dir web build
uv sync --frozen --group build --extra memory-mcp
uv run --frozen --group build --extra memory-mcp pyinstaller --noconfirm --onedir --windowed \
  --name AgentWorkbench --osx-bundle-identifier com.zznmdhz.agentworkbench \
  --hidden-import awb.cli --collect-submodules awb.memory --collect-all mcp \
  --add-data "$workspace/web/dist:web/dist" \
  --add-data "$workspace/src/awb/build.json:awb" \
  --distpath "$workspace/dist/mac" \
  --workpath "$workspace/.local/pyinstaller-mac-work" \
  --specpath "$workspace/.local" packaging/desktop_entrypoint.py

printf 'Mac app: %s\n' "$workspace/dist/mac/AgentWorkbench.app"

uv run --frozen --group build --extra memory-mcp pyinstaller --noconfirm --onedir --console \
  --name AgentWorkbenchCLI --collect-submodules awb.memory --collect-all mcp \
  --distpath "$workspace/dist/mac-cli" \
  --workpath "$workspace/.local/pyinstaller-mac-cli-work" \
  --specpath "$workspace/.local" packaging/entrypoint.py
mkdir -p "$workspace/dist/mac/AgentWorkbench.app/Contents/Resources/cli"
ditto "$workspace/dist/mac-cli/AgentWorkbenchCLI" "$workspace/dist/mac/AgentWorkbench.app/Contents/Resources/cli"
codesign --force --deep --sign - "$workspace/dist/mac/AgentWorkbench.app"
