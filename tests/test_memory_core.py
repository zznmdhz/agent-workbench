import os
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest

from awb.memory import BudgetExceededError, MemoryStore, MemoryValidationError, StaleRevisionError
from awb.memory.store import machine_binding


def make_store(tmp_path, device="mac"):
    store = MemoryStore(tmp_path / device / "memory.db", device_id=device).initialize()
    store.configure_space("create")
    return store


def add_source(store, *, body="请记住：客户文档不要内部说明。", scope="personal", message="message", local=True, complete=True, role="user"):
    return store.ingest_source({"agent": "codex", "native_id": "session", "message_id": message,
                                "body": body, "role": role, "scope": scope, "complete": complete}, local=local)


def propose(store, source, text="客户文档不要内部说明", actor="owner"):
    return store.propose(text, "preference", source["scope"],
                         [{"source_id": source["id"], "quote": source["body"]}], actor=actor)


def sync_records(sender, recipient):
    data = sender.export_events(after=0, limit=1000)
    return recipient.import_events(data["operations"], data["sources"])


def test_candidate_requires_owner_confirmation(tmp_path):
    store = make_store(tmp_path)
    source = add_source(store)
    candidate = propose(store, source)
    assert not store.recall("客户文档", frozenset({"personal"}), include_sources=False)["items"]
    raw = store.recall("客户文档", frozenset({"personal"}))["items"]
    assert raw and all(item["type"] == "source" for item in raw)
    current = store.change(candidate["id"], "confirm", parents=candidate["heads"])
    assert current["state"] == "active"
    assert store.recall("客户文档", frozenset({"personal"}), include_sources=False)["items"][0]["id"] == candidate["id"]


def test_scope_applies_to_every_read_and_proposal(tmp_path):
    store = make_store(tmp_path)
    source = add_source(store, scope="customer-A")
    item = propose(store, source)
    public = frozenset({"personal"})
    for operation in (lambda: store.get_source(source["id"], scopes=public),
                      lambda: store.inspect(item["id"], scopes=public),
                      lambda: store.profile("customer-A", scopes=public)):
        with pytest.raises(LookupError):
            operation()
    assert not store.sources(scopes=public)
    assert not store.list_memories(scopes=public)["items"]
    assert store.status(scopes=public)["counts"]["sources"] == 0
    with pytest.raises(LookupError):
        store.propose("secret", "fact", "personal", [{"source_id": source["id"], "quote": source["body"]}], actor="assistant_proposal", scopes=public)
    with pytest.raises(MemoryValidationError):
        store.get_source(source["id"], scopes={"customer-A"})


def test_source_scope_and_authority_are_immutable(tmp_path):
    store = make_store(tmp_path)
    first = add_source(store)
    second = add_source(store, scope="project")
    assert first["id"] != second["id"]
    changed = {key: value for key, value in first.items() if key not in {"cursor", "local"}}
    changed["role"] = "assistant"
    with pytest.raises(MemoryValidationError, match="authoritative"):
        store.ingest_source(changed)
    with pytest.raises(MemoryValidationError, match="scopes"):
        store.propose("text", "fact", "project", [{"source_id": first["id"], "quote": first["body"]}], actor="owner")
    edited = dict(first, body="changed text")
    with pytest.raises(MemoryValidationError, match="checksum"):
        store.ingest_source(edited)


def test_sources_need_complete_verbatim_evidence(tmp_path):
    store = make_store(tmp_path)
    source = add_source(store, complete=False)
    with pytest.raises(MemoryValidationError, match="complete"):
        propose(store, source)
    source = add_source(store, message="other")
    with pytest.raises(MemoryValidationError, match="quote"):
        store.propose("invented", "fact", "personal", [{"source_id": source["id"], "quote": "not in source"}], actor="owner")
    with pytest.raises(MemoryValidationError, match="credential"):
        add_source(store, body="api_key=abcdefghijklmnopq1234567")


def test_assistant_cannot_promote_or_override_lineage(tmp_path):
    store = make_store(tmp_path)
    candidate = store.propose("我的猜测", "fact", "personal", [], actor="assistant_proposal", scopes=frozenset({"personal"}))
    assert candidate["state"] == "candidate"
    source = store.inspect(candidate["id"])["sources"][0]
    assert source["role"] == "assistant" and not source["local"]
    with pytest.raises(MemoryValidationError, match="only propose"):
        store.propose("claim", "fact", "personal", [], actor="owner", scopes=frozenset({"personal"}))
    with pytest.raises(MemoryValidationError):
        store.propose("claim", "fact", "personal", [], actor="assistant_proposal", memory_id=candidate["id"], scopes=frozenset({"personal"}))


def test_explicit_parents_and_disabled_review_state(tmp_path):
    store = make_store(tmp_path)
    item = propose(store, add_source(store))
    with pytest.raises(TypeError):
        store.change(item["id"], "confirm")
    disabled = store.change(item["id"], "disable", parents=item["heads"])
    enabled = store.change(item["id"], "enable", parents=disabled["heads"])
    assert enabled["state"] == "candidate"
    with pytest.raises(StaleRevisionError):
        store.change(item["id"], "confirm", parents=item["heads"])
    active = store.change(item["id"], "confirm", parents=enabled["heads"])
    disabled = store.change(item["id"], "disable", parents=active["heads"])
    assert not store.recall("客户文档", frozenset({"personal"}))["items"]
    assert store.change(item["id"], "enable", parents=disabled["heads"])["state"] == "active"


def test_deleted_lineage_and_changed_body_stay_suppressed(tmp_path):
    store = make_store(tmp_path)
    source = add_source(store)
    item = propose(store, source)
    deleted = store.change(item["id"], "delete", parents=item["heads"])
    assert deleted["state"] == "deleted"
    assert store.get_source(source["id"])["body"] == source["body"]
    with pytest.raises(LookupError):
        store.get_source(source["id"], scopes=frozenset({"personal"}))
    with pytest.raises(LookupError):
        store.inspect(item["id"], scopes=frozenset({"personal"}))
    assert not store.recall("客户文档", frozenset({"personal"}))["items"]
    changed_source = add_source(store, body="客户文档不要内部说明，新版本。")
    assert not store.source_available(changed_source["id"])
    assert not store.claim_jobs()
    with pytest.raises(MemoryValidationError):
        propose(store, changed_source, actor="processor")
    with pytest.raises(MemoryValidationError, match="permanent"):
        store.change(item["id"], "enable", parents=deleted["heads"])
    new_owner_source = add_source(store, message="explicit-new", local=False)
    assert propose(store, new_owner_source)["state"] == "candidate"


def test_cross_process_leases_fixed_checksum_and_atomic_budget(tmp_path):
    store = make_store(tmp_path)
    source = add_source(store)
    another = MemoryStore(store.path).initialize()
    jobs = store.claim_jobs()
    assert jobs and not another.claim_jobs()
    token = jobs[0]["lease_token"]
    with pytest.raises(MemoryValidationError, match="lease"):
        another.finish_job(source["id"], "wrong")
    with store.transaction() as conn:
        conn.execute("UPDATE memory_jobs SET lease_until=?", (time.time() - 1,))
    retry = another.claim_jobs()[0]
    assert retry["lease_token"] != token and retry["attempts"] == 2
    with pytest.raises(MemoryValidationError, match="lease"):
        store.propose_from_job(source["id"], token, [{"text": "preference", "kind": "fact", "scope": "personal", "evidence": [{"source_id": source["id"], "quote": source["body"]}]}])
    another.finish_job(source["id"], retry["lease_token"])
    def reserve(_):
        try:
            return another.reserve_model_call("query_embedding", "test", 3)
        except BudgetExceededError:
            return None
    with ThreadPoolExecutor(max_workers=8) as executor:
        calls = list(executor.map(reserve, range(10)))
    assert sum(call is not None for call in calls) == 3
    store.finish_model_call(next(call for call in calls if call), error="timeout")
    with pytest.raises(BudgetExceededError):
        store.reserve_model_call("llm", "different model", 3)
    usage = store.model_usage()["items"][0]
    assert usage["calls"] == 3 and usage["unknown_usage"] == 3


def test_pending_parent_and_delete_first_converge(tmp_path):
    first = make_store(tmp_path, "first")
    source = add_source(first)
    item = propose(first, source)
    active = first.change(item["id"], "confirm", parents=item["heads"])
    first.change(item["id"], "delete", parents=active["heads"])
    data = first.export_events(after=0)
    for index, permutation in enumerate((data["operations"], list(reversed(data["operations"])))):
        other = MemoryStore(tmp_path / f"copy{index}.db", device_id=f"copy{index}").initialize()
        other.configure_space("join", first.join_code())
        for operation in permutation:
            other.import_events([operation], data["sources"])
            if operation["action"] == "delete":
                assert other.list_memories()["items"][0]["state"] == "deleted"
                assert not other.recall("客户文档", frozenset({"personal"}))["items"]
        assert other.inspect(item["id"])["memory"]["state"] == "deleted"
        assert all(op["validity"] == "valid" for op in other.inspect(item["id"])["operations"])


def test_delete_first_blocks_all_old_evidence_and_late_message_snapshots(tmp_path):
    first = make_store(tmp_path, "first")
    original = add_source(first, body="原始要求：保留证据", message="one")
    current = add_source(first, body="修订要求：仍然保留证据", message="two")
    item = propose(first, original)
    edited = first.change(item["id"], "edit", {"text": "修订要求", "kind": "constraint", "scope": "personal", "evidence": [{"source_id": current["id"], "quote": current["body"]}]}, parents=item["heads"])
    first.change(item["id"], "delete", parents=edited["heads"])
    records = first.export_events(after=0)
    second = make_store(tmp_path, "second")
    delete = records["operations"][-1]
    second.import_events([delete], [])
    # The delete carries all historic evidence identities, even before snapshots.
    assert original["id"] in delete["payload"]["blocked_sources"]
    second.import_events([], records["sources"])
    assert not second.recall("保留证据", frozenset({"personal"}))["items"]
    updated = add_source(second, body="原始要求：保留证据，迟到更新。", message="one")
    assert not second.source_available(updated["id"])
    assert not second.recall("迟到更新", frozenset({"personal"}))["items"]
    second.import_events(records["operations"][:-1], [])
    assert second.inspect(item["id"])["memory"]["state"] == "deleted"


def test_concurrent_heads_require_explicit_resolution(tmp_path):
    first = make_store(tmp_path, "first")
    second = MemoryStore(tmp_path / "second.db", device_id="second").initialize()
    second.configure_space("join", first.join_code())
    source = add_source(first)
    initial = propose(first, source)
    current = first.change(initial["id"], "confirm", parents=initial["heads"])
    sync_records(first, second)
    payload = {key: current[key] for key in ("text", "kind", "scope", "evidence")}
    edit = first.change(current["id"], "edit", payload | {"text": "Mac决定"}, parents=current["heads"])
    second.change(current["id"], "edit", payload | {"text": "Windows决定"}, parents=current["heads"])
    sync_records(first, second)
    sync_records(second, first)
    assert first.inspect(current["id"])["memory"] == second.inspect(current["id"])["memory"]
    conflict = first.inspect(current["id"])["memory"]
    assert conflict["state"] == "conflict"
    assert not first.recall("决定", frozenset({"personal"}), include_sources=False)["items"]
    with pytest.raises(StaleRevisionError):
        first.change(current["id"], "resolve", payload | {"state": "active"}, parents=edit["heads"])
    resolved = first.change(current["id"], "resolve", payload | {"text": "owner解决", "state": "active"}, parents=conflict["heads"])
    sync_records(first, second)
    assert second.inspect(current["id"])["memory"] == resolved


def test_rewrites_cycles_and_cross_lineage_are_rejected(tmp_path):
    first = make_store(tmp_path, "first")
    source = add_source(first)
    item = propose(first, source)
    data = first.export_events(after=0)
    second = make_store(tmp_path, "second")
    second.import_events(data["operations"], data["sources"])
    op = deepcopy(data["operations"][0])
    op["payload"]["text"] = "rewritten"
    with pytest.raises(MemoryValidationError, match="rewritten"):
        second.import_events([op], [])
    op = deepcopy(data["operations"][0])
    op.update(id="self-cycle", seq=2, action="confirm", actor="owner", payload={}, parents=["self-cycle"])
    with pytest.raises(MemoryValidationError):
        second.import_events([op], [])
    op.update(id="future-child", parents=["future-parent"])
    second.import_events([op], [])
    op.update(id="future-parent", seq=3, parents=["future-child"])
    with pytest.raises(MemoryValidationError, match="cycle"):
        second.import_events([op], [])
    op.update(id="foreign", seq=3, memory_id="different-memory", parents=item["heads"])
    with pytest.raises(MemoryValidationError, match="another lineage"):
        second.import_events([op], [])


def test_restore_identity_credentials_and_future_schema(tmp_path):
    store = make_store(tmp_path)
    item = propose(store, add_source(store))
    credential = store.join_code()
    old = store.device_id
    store.reset_replica_identity("restored")
    assert store.space_id and store.device_id != old
    assert store.status()["credentials_missing"]
    assert not store.claim_jobs()
    assert store.inspect(item["id"])["memory"]["id"] == item["id"]
    store.configure_space("join", credential)
    assert store.status()["has_join_key"]
    with pytest.raises(MemoryValidationError, match="empty"):
        store.configure_space("create")
    with pytest.raises(MemoryValidationError, match="identity"):
        MemoryStore(store.path, device_id="another-machine").initialize()
    with store.connect() as conn:
        conn.execute("PRAGMA user_version=99")
    with pytest.raises(MemoryValidationError, match="newer"):
        store.initialize()


def test_secret_config_not_in_database_or_public_settings(tmp_path):
    store = make_store(tmp_path)
    store.update_settings({"llm": {"api_key": "super-private-model-key", "enabled": True}})
    assert "super-private" not in str(store.settings())
    assert "shared_key" not in str(store.settings())
    assert store.settings()["llm"]["has_key"]
    assert "super-private-model-key" not in store.path.read_bytes().decode("utf-8", errors="ignore")
    with pytest.raises(MemoryValidationError):
        store.update_settings({"owner": True})
    assert sqlite3.connect(store.path).execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_cloned_machine_is_read_only_until_explicit_restore(tmp_path):
    store = make_store(tmp_path)
    source = add_source(store)
    item = propose(store, source)
    values = store.private_settings()
    values["writer_binding"] = "other-machine"
    store._write_config(values)
    assert not store.status()["writer_available"]
    assert store.inspect(item["id"])["memory"]["text"] == item["text"]
    assert not store.claim_jobs()
    for operation in (lambda: add_source(store, message="new"),
                      lambda: store.change(item["id"], "confirm", parents=item["heads"]),
                      lambda: store.reserve_model_call("llm", "test", 50),
                      lambda: store.import_events([], []), lambda: store.shared_key()):
        with pytest.raises(MemoryValidationError, match="read-only"):
            operation()
    store.reset_replica_identity("fresh-writer")
    assert store.status()["writer_available"] and not store.status()["has_join_key"]


def test_machine_binding_is_stable_in_fresh_process(tmp_path):
    store = make_store(tmp_path)
    code = "from awb.memory.store import machine_binding; print(machine_binding())"
    for _ in range(2):
        child = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                               check=True, timeout=10,
                               env={"PATH": os.environ.get("PATH", ""),
                                    "PYTHONPATH": os.pathsep.join(sys.path)})
        assert child.stdout.strip() == machine_binding()
    item = propose(store, add_source(store))
    code = "from awb.memory import MemoryStore; s=MemoryStore(__import__('sys').argv[1]).initialize(); print(s.propose('新提议','fact','personal',[],actor='assistant_proposal')['state'])"
    child = subprocess.run([sys.executable, "-c", code, str(store.path)], capture_output=True,
                           text=True, check=True, timeout=10,
                           env={"PATH": os.environ.get("PATH", ""),
                                "PYTHONPATH": os.pathsep.join(sys.path)})
    assert child.stdout.strip() == "candidate" and store.inspect(item["id"])


def test_atomic_owner_correction_rolls_back_source_on_stale_or_invalid_state(tmp_path):
    store = make_store(tmp_path)
    item = propose(store, add_source(store))
    current = store.change(item["id"], "confirm", parents=item["heads"])
    before = len(store.sources())
    with pytest.raises(StaleRevisionError):
        store.change_owner_text(item["id"], "edit", "孤儿新要求", "constraint", parents=item["heads"])
    with pytest.raises(MemoryValidationError):
        store.change_owner_text(item["id"], "resolve", "孤儿新要求", "constraint", parents=current["heads"], state="active")
    assert len(store.sources()) == before
    assert not store.recall("孤儿新要求", frozenset({"personal"}))["items"]
    updated = store.change_owner_text(item["id"], "edit", "修改后的长期要求", "constraint", parents=current["heads"])
    assert updated["state"] == "active" and updated["text"] == "修改后的长期要求"


def test_budget_deferral_does_not_exhaust_job_attempts(tmp_path):
    store = make_store(tmp_path)
    source = add_source(store)
    for _ in range(4):
        job = store.claim_jobs()[0]
        assert job["attempts"] == 1
        store.defer_job(source["id"], job["lease_token"], next_run=time.time() + 3600)
        assert not store.claim_jobs()
        with store.transaction() as conn:
            conn.execute("UPDATE memory_jobs SET next_run=0")
    assert not store.model_usage()["items"]


def test_owner_retry_preserves_model_budget_and_skips_deleted_sources(tmp_path):
    store = make_store(tmp_path)
    available = add_source(store, message="available")
    suppressed = add_source(store, message="suppressed")
    for job in store.claim_jobs():
        store.finish_job(job["id"], job["lease_token"], error="temporary failure")
    item = propose(store, suppressed)
    store.change(item["id"], "delete", parents=item["heads"])
    store.reserve_model_call("llm", "test", 1)
    assert store.retry_failed_jobs() == {"retried": 1, "remaining": 1}
    jobs = store.claim_jobs()
    assert len(jobs) == 1 and jobs[0]["id"] == available["id"] and jobs[0]["attempts"] == 1
    with pytest.raises(BudgetExceededError):
        store.reserve_model_call("query_embedding", "test", 1)
