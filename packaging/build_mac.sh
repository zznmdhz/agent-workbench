#!/bin/sh
set -eu

workspace=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$workspace"

pnpm --dir web install --frozen-lockfile
pnpm --dir web build
uv sync --frozen --group build
uv run --frozen --group build pyinstaller --noconfirm --onedir --windowed \
  --name AgentWorkbench --osx-bundle-identifier com.zznmdhz.agentworkbench \
  --hidden-import awb.cli \
  --add-data "$workspace/web/dist:web/dist" \
  --distpath "$workspace/dist/mac" \
  --workpath "$workspace/.local/pyinstaller-mac-work" \
  --specpath "$workspace/.local" packaging/desktop_entrypoint.py

printf 'Mac app: %s\n' "$workspace/dist/mac/AgentWorkbench.app"
