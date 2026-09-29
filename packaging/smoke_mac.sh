#!/bin/sh
set -eu

workspace=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
app="$workspace/dist/mac/AgentWorkbench.app/Contents/MacOS/AgentWorkbench"
if [ ! -x "$app" ]; then
  printf 'Missing Mac app: %s\n' "$app" >&2
  exit 1
fi

scratch="$workspace/.local/mac-package-smoke"
mkdir -p "$scratch"
port=8767
CODEX_HOME="$scratch/empty-codex" CLAUDE_CONFIG_DIR="$scratch/empty-claude" \
  HERMES_STATE_DB="$scratch/empty-hermes.db" \
  "$app" --db "$scratch/workbench.db" --port "$port" --no-browser &
server_pid=$!
trap 'kill "$server_pid" 2>/dev/null || true' EXIT INT TERM

ready=0
for _ in $(seq 1 100); do
  if curl --noproxy '*' -fsS "http://127.0.0.1:$port/health/ready" > "$scratch/ready.json" 2>/dev/null; then
    ready=1
    break
  fi
  sleep 0.2
done
if [ "$ready" -ne 1 ]; then
  printf 'Mac app did not become ready\n' >&2
  exit 1
fi
curl --noproxy '*' -fsS "http://127.0.0.1:$port/" > "$scratch/index.html"
curl --noproxy '*' -fsS "http://127.0.0.1:$port/v1/mvp/usage?day=2026-09-28&through=2026-09-28&heatmap_view=day" > "$scratch/usage.json"
curl --noproxy '*' -fsS "http://127.0.0.1:$port/v1/mvp/activity?day=2026-09-28&through=2026-09-28&heatmap_view=day&focus_day=2026-09-28" > "$scratch/activity.json"
python3 - "$scratch" <<'PY'
import json
from pathlib import Path
import sys

root = Path(sys.argv[1])
ready = json.loads((root / "ready.json").read_text())
usage = json.loads((root / "usage.json").read_text())
activity = json.loads((root / "activity.json").read_text())
index = (root / "index.html").read_text()
assert ready["status"] == "ready" and ready["app_version"] == "0.5.3"
assert "/assets/index-" in index
assert usage["status"] == "ready" and len(usage["heatmap"]) == 24
assert len(activity["heatmap"]) == 24 and activity["summary"]["agent_ms"] == 0
print("Mac packaged app smoke test passed")
PY
