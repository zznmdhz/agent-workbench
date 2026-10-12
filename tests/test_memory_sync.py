import gzip
from copy import deepcopy

from test_memory_core import add_source, propose

from awb.memory import MemoryStore, MemorySync
from awb.memory.sync import packet_bytes


def pair(tmp_path):
    first = MemoryStore(tmp_path / "mac" / "memory.db", device_id="mac").initialize()
    second = MemoryStore(tmp_path / "win" / "memory.db", device_id="win").initialize()
    first.configure_space("create")
    second.configure_space("join", first.join_code())
    root = tmp_path / "NAS"
    return first, second, MemorySync(first, root), MemorySync(second, root), root


def test_two_device_offline_candidate_confirm_edit_delete(tmp_path):
    first, second, sync_first, sync_second, root = pair(tmp_path)
    source = add_source(first)
    item = propose(first, source)
    assert sync_first.run_once()["exported"] == 1
    result = sync_second.run_once()
    assert result["imported"] == 1 and not result["errors"]
    assert not second.claim_jobs()  # Remote sources never re-trigger local extraction.
    assert second.inspect(item["id"])["memory"]["state"] == "candidate"
    current = first.change(item["id"], "confirm", parents=item["heads"])
    sync_first.run_once()
    sync_second.run_once()
    assert second.recall("客户文档", frozenset({"personal"}), include_sources=False)["items"]
    # Query is entirely local even if NAS becomes unavailable.
    root.rename(root.with_name("NAS-offline"))
    assert second.profile("personal", scopes=frozenset({"personal"}))["items"]
    root.with_name("NAS-offline").rename(root)
    first.change(item["id"], "delete", parents=current["heads"])
    sync_first.run_once()
    sync_second.run_once()
    assert not second.recall("客户文档", frozenset({"personal"}))["items"]
    assert sync_second.run_once()["imported"] == 0
    assert not list(root.rglob("*.db")) and not list(root.rglob("*.private.json"))


def test_raw_source_only_sync_and_missing_evidence_independence(tmp_path):
    first, second, sync_first, sync_second, _ = pair(tmp_path)
    source = add_source(first, body="普通历史对话可用作原始证据。")
    assert sync_first.run_once()["exported"] == 1
    assert sync_second.run_once()["sources"] == 1
    assert second.get_source(source["id"], scopes=frozenset({"personal"}))["body"] == source["body"]
    assert not second.claim_jobs()
    assert not second.list_memories()["items"]


def test_wrong_key_and_tampered_gzip_do_not_mutate(tmp_path):
    first, second, sync_first, sync_second, root = pair(tmp_path)
    propose(first, add_source(first))
    sync_first.run_once()
    packet = next(root.rglob("*.json.gz"))
    data = packet.read_bytes()
    packet.write_bytes(data[:-4] + b"bad!")
    result = sync_second.run_once()
    assert result["errors"] and not second.sources()
    packet.write_bytes(data)
    config = second.private_settings()
    config["shared_key"] = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
    second._write_config(config)
    result = sync_second.run_once()
    assert result["errors"] and not second.sources()


def test_authenticated_sequence_rewrite_rejected(tmp_path):
    first, second, sync_first, sync_second, root = pair(tmp_path)
    source = add_source(first)
    propose(first, source)
    sync_first.run_once()
    sync_second.run_once()
    records = first.export_events(after=0)
    operations = deepcopy(records["operations"])
    operations[0]["payload"]["text"] = "malicious rewrite"
    payload = {"version": 1, "space_id": first.space_id, "device_id": "mac", "packet_seq": 99,
               "operations": operations, "sources": records["sources"]}
    sha, data = packet_bytes(payload, first.shared_key())
    destination = root / "AgentWorkbench-memory-exchange" / first.space_id / "devices/mac/packets"
    (destination / f"{99:012d}-{sha}.json.gz").write_bytes(data)
    assert sync_second.run_once()["errors"]
    assert second.list_memories()["items"][0]["text"] != "malicious rewrite"


def test_oversize_gzip_and_symlink_are_rejected(tmp_path):
    first, second, _, sync_second, root = pair(tmp_path)
    directory = root / "AgentWorkbench-memory-exchange" / first.space_id / "devices/mac/packets"
    directory.mkdir(parents=True)
    name = f"{1:012d}-{'0'*64}.json.gz"
    file = directory / name
    file.write_bytes(gzip.compress(b"x" * (9 * 1024 * 1024)))
    assert sync_second.run_once()["errors"]
    file.unlink()
    target = tmp_path / "outside.json.gz"
    target.write_bytes(b"unused")
    file.symlink_to(target)
    assert sync_second.run_once()["errors"]
    assert not second.sources()


def test_database_cannot_live_in_exchange_root(tmp_path):
    store = MemoryStore(tmp_path / "nas" / "memory.db").initialize()
    store.configure_space("create")
    assert MemorySync(store, tmp_path / "nas").run_once()["errors"]


def test_import_pending_confirmation_before_proposal(tmp_path):
    first, second, _, sync_second, root = pair(tmp_path)
    source = add_source(first)
    item = propose(first, source)
    active = first.change(item["id"], "confirm", parents=item["heads"])
    records = first.export_events(after=0)
    directory = root / "AgentWorkbench-memory-exchange" / first.space_id / "devices/mac/packets"
    directory.mkdir(parents=True)
    for number, operations in ((1, records["operations"][1:]), (2, records["operations"][:1])):
        payload = {"version": 1, "space_id": first.space_id, "device_id": "mac", "packet_seq": number, "operations": operations, "sources": records["sources"]}
        sha, content = packet_bytes(payload, first.shared_key())
        path = directory / f"{number:012d}-{sha}.json.gz"
        path.write_bytes(content)
        assert not sync_second.run_once()["errors"]
        if number == 1:
            assert second.list_memories()["items"][0]["state"] == "pending"
    assert second.inspect(item["id"])["memory"] == active


def test_export_recovers_durable_packet_before_cursor_commit(tmp_path, monkeypatch):
    first, second, sync_first, sync_second, root = pair(tmp_path)
    first_source = add_source(first, body="第一条消息", message="first")
    mark = first.mark_exported
    def fail_after_rename(*args, **kwargs):
        raise OSError("simulated process failure after rename")
    monkeypatch.setattr(first, "mark_exported", fail_after_rename)
    assert sync_first.run_once()["errors"]
    durable = next(root.rglob("*.json.gz"))
    content = durable.read_bytes()
    # New data makes a naïvely regenerated seq=1 packet have a different digest.
    second_source = add_source(first, body="恢复之前又新增的消息", message="second")
    monkeypatch.setattr(first, "mark_exported", mark)
    result = sync_first.run_once()
    assert not result["errors"] and result["exported"] == 2
    assert durable.read_bytes() == content and len(list(root.rglob("*.json.gz"))) == 2
    imported = sync_second.run_once()
    assert not imported["errors"]
    for source in (first_source, second_source):
        assert second.get_source(source["id"], scopes=frozenset({"personal"}))["body"] == source["body"]
