"""Operational entry point for server setup and local collectors."""

from __future__ import annotations

import json
import os
import platform
import time
import webbrowser
from pathlib import Path
from threading import Timer
from uuid import uuid4

import httpx
import typer
import uvicorn

from .auth import initialize_owner
from .backup import create_backup, restore_backup
from .collector import Outbox, run_cycle
from .db import Database

app = typer.Typer(help="Agent Workbench service and read-only collectors")


def _config(path: Path) -> dict:
    if not path.exists():
        raise typer.BadParameter(f"Collector config not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _write_config(path: Path, config: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o600)


@app.command("init-owner")
def init_owner(db: Path = typer.Option(Path("data/agent-workbench.db"), help="Server database path")):
    password = typer.prompt("New owner password (12+ characters)", hide_input=True, confirmation_prompt=True)
    store = Database(db)
    store.initialize()
    initialize_owner(store, password)
    typer.echo("Owner account created.")


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8765,
          db: Path = typer.Option(Path("data/agent-workbench.db"), help="Server database path")):
    os.environ["AWB_DB_PATH"] = str(db)
    from .api import app as service_app

    uvicorn.run(service_app, host=host, port=port, workers=1)


@app.command("open")
def open_workbench(db: Path = typer.Option(Path("data/agent-workbench.db")),
                   port: int = typer.Option(8765, min=1, max=65535),
                   browser: bool = typer.Option(True, "--browser/--no-browser")):
    store = Database(db)
    store.initialize()
    with store.read() as conn:
        initialized = conn.execute("SELECT 1 FROM owner WHERE id=1").fetchone() is not None
    if not initialized:
        password = typer.prompt("Create owner password (12+ characters)", hide_input=True, confirmation_prompt=True)
        initialize_owner(store, password)
    if browser:
        Timer(2.0, lambda: webbrowser.open(f"http://127.0.0.1:{port}/")).start()
    serve(host="127.0.0.1", port=port, db=db)


@app.command("pair")
def pair(server: str, code: str, name: str = platform.node(),
         config: Path = Path(".local/collector.json")):
    with httpx.Client(base_url=server, timeout=20) as client:
        response = client.post("/v1/devices/pair", json={"code": code, "name": name,
                                                          "os": platform.system(), "environment": platform.platform()})
        response.raise_for_status()
        result = response.json()
    value = {"server": server.rstrip("/"), "collector_id": result["collector_id"],
             "token": result["token"], "sources": []}
    _write_config(config, value)
    typer.echo(f"Paired device {result['collector_id']}. Token saved only in {config}.")


@app.command("add-source")
def add_source(agent: str, root: Path, profile: str = "default",
               policy: str = "stats_only", config: Path = Path(".local/collector.json")):
    if agent not in {"codex", "hermes"} or policy not in {"full_content", "stats_only", "excluded"}:
        raise typer.BadParameter("Agent must be codex/hermes and policy full_content/stats_only/excluded")
    value = _config(config)
    path = root.expanduser().resolve()
    if not path.exists():
        raise typer.BadParameter("Source path does not exist")
    source = {"id": str(uuid4()), "agent": agent, "profile": profile, "root": str(path),
              "content_policy": policy, "environment_id": None}
    value["sources"].append(source)
    _write_config(config, value)
    typer.echo("Source added locally. Register this source in the owner web UI before scanning:")
    typer.echo(json.dumps({"id": source["id"], "device_id": value["collector_id"],
                           "agent": agent, "profile": profile, "content_policy": policy}, ensure_ascii=False))


@app.command("map-root")
def map_root(root_id: str, native_root: Path, map_version: int = 1,
             config: Path = Path(".local/collector.json")):
    value = _config(config)
    path = native_root.expanduser().resolve()
    if not path.is_dir():
        raise typer.BadParameter("Root directory does not exist")
    maps = [m for m in value.get("root_maps", []) if m["root_id"] != root_id]
    maps.append({"root_id": root_id, "native_root": str(path), "map_version": map_version})
    value["root_maps"] = maps
    _write_config(config, value)
    typer.echo("Local root registered. Add the matching root/environment map in owner settings.")


@app.command("collect-once")
def collect_once(config: Path = Path(".local/collector.json"),
                 outbox: Path = Path(".local/outbox.db")):
    result = run_cycle(_config(config), Outbox(outbox))
    typer.echo(json.dumps(result, ensure_ascii=False))


@app.command("collect")
def collect(interval: int = typer.Option(15, min=5), config: Path = Path(".local/collector.json"),
            outbox: Path = Path(".local/outbox.db")):
    queue = Outbox(outbox)
    while True:
        try:
            result = run_cycle(_config(config), queue)
            typer.echo(json.dumps(result, ensure_ascii=False))
        except (httpx.HTTPError, OSError, ValueError) as exc:
            typer.echo(f"Collector cycle failed: {type(exc).__name__}", err=True)
        time.sleep(interval)


@app.command("backup")
def backup(db: Path = Path("data/agent-workbench.db"), output: Path = Path("backups/agent-workbench.zip")):
    result = create_backup(db, output)
    typer.echo(json.dumps(result, ensure_ascii=False))


@app.command("restore-to")
def restore_to(archive: Path, target_dir: Path):
    path = restore_backup(archive, target_dir)
    typer.echo(f"Verified restore at {path}; use this directory only after checking security changes since backup.")


if __name__ == "__main__":
    app()
