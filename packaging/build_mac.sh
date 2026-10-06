#!/bin/sh
set -eu

workspace=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$workspace"
uv run python -c 'import json,subprocess; from datetime import datetime,timezone; from pathlib import Path; from awb import __version__; Path("src/awb/build.json").write_text(json.dumps({"version":__version__,"commit":subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip(),"built_at":datetime.now(timezone.utc).isoformat()}))'

pnpm --dir web install --frozen-lockfile
pnpm --dir web build
uv sync --frozen --group build
uv run --frozen --group build pyinstaller --noconfirm --onedir --windowed \
  --name AgentWorkbench --osx-bundle-identifier com.zznmdhz.agentworkbench \
  --hidden-import awb.cli \
  --add-data "$workspace/web/dist:web/dist" \
  --add-data "$workspace/src/awb/build.json:awb" \
  --distpath "$workspace/dist/mac" \
  --workpath "$workspace/.local/pyinstaller-mac-work" \
  --specpath "$workspace/.local" packaging/desktop_entrypoint.py

printf 'Mac app: %s\n' "$workspace/dist/mac/AgentWorkbench.app"
