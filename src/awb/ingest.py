"""At-least-once ingestion with immutable facts and contiguous durable receipts."""

from __future__ import annotations

import json
from uuid import uuid4

from fastapi import HTTPException

from .auth import now_iso
from .codec import ack_prefix, sha256
from .db import Database
from .models import Batch
from .projector import project

SETTLED = {"accepted", "duplicate", "ignored_tombstoned", "quarantined_resolved"}


def server_epoch(db: Database) -> str:
    with db.tx() as conn:
        row = conn.execute("SELECT value FROM app_meta WHERE key='server_epoch'").fetchone()
        if row:
            return row[0]
        value = str(uuid4())
        conn.execute("INSERT INTO app_meta(key,value) VALUES('server_epoch',?)", (value,))
        return value


def _acks(conn, device_id: str, epoch: str) -> tuple[int, int]:
    row = conn.execute("SELECT durable_ack,projected_ack FROM streams WHERE collector_id=? AND epoch=?", (device_id, epoch)).fetchone()
    old = row["durable_ack"]
    receipts = {r["seq"]: r["status"] for r in conn.execute("SELECT seq,status FROM deliveries WHERE collector_id=? AND epoch=? AND seq>? ORDER BY seq LIMIT 1000", (device_id, epoch, old))}
    ack = ack_prefix(old, receipts)
    # Projection runs in the same transaction as receipt persistence in v0.1.
    conn.execute("UPDATE streams SET durable_ack=?,projected_ack=? WHERE collector_id=? AND epoch=?", (ack, ack, device_id, epoch))
    return ack, ack


def receive_batch(db: Database, device_id: str, batch: Batch) -> dict:
    if str(batch.collector_id) != device_id:
        raise HTTPException(403, "Collector identity mismatch")
    epoch = str(batch.outbox_epoch)
    batch_json = batch.model_dump(mode="json")
    digest = sha256(batch_json)
    received = now_iso()
    current_server_epoch = server_epoch(db)
    receipts = []
    with db.tx() as conn:
        old_batch = conn.execute("SELECT body_hash FROM ingest_batches WHERE batch_id=?", (str(batch.batch_id),)).fetchone()
        if old_batch and old_batch[0] != digest:
            raise HTTPException(409, "Batch ID reused with different content")
        conn.execute("INSERT OR IGNORE INTO ingest_batches(batch_id,collector_id,epoch,body_hash,received_at) VALUES(?,?,?,?,?)", (str(batch.batch_id), device_id, epoch, digest, received))
        conn.execute("INSERT OR IGNORE INTO streams(collector_id,epoch,server_epoch) VALUES(?,?,?)", (device_id, epoch, current_server_epoch))
        for entry in batch.entries:
            ev = entry.event.model_dump(mode="json")
            c = ev["content"]
            seq = entry.seq
            old_delivery = conn.execute("SELECT event_id,body_hash,status,receipt_id,reason FROM deliveries WHERE collector_id=? AND epoch=? AND seq=?", (device_id, epoch, seq)).fetchone()
            if old_delivery:
                if old_delivery["event_id"] != ev["event_id"] or old_delivery["body_hash"] != ev["body_hash"]:
                    raise HTTPException(409, f"Outbox sequence {seq} reused with different content")
                receipts.append({"seq": seq, "status": old_delivery["status"], "receipt_id": old_delivery["receipt_id"], "reason": old_delivery["reason"]})
                continue
            source = conn.execute("SELECT device_id,capability_json FROM sources WHERE id=?", (c["source_instance_id"],)).fetchone()
            if source is None or source["device_id"] != device_id:
                raise HTTPException(403, "Source is not registered to this collector")
            if c["execution"]["physical_device_id"] not in {None, device_id}:
                raise HTTPException(403, "Execution device does not match collector")
            policy = json.loads(source["capability_json"]).get("content_policy", "stats_only")
            if policy == "excluded":
                raise HTTPException(403, "Source excluded by owner policy")
            if policy == "stats_only" and c["fact_kind"] == "message.observed":
                if c["payload"].get("body") is not None or c["payload"].get("body_ref") is not None:
                    raise HTTPException(403, "Source policy forbids message body")
            receipt_id = str(uuid4())
            status = "accepted"
            reason = None
            raw = None
            tombstone = conn.execute("SELECT 1 FROM tombstones WHERE source_id=? AND native_session_id=?", (c["source_instance_id"], c["native_session_id"])).fetchone()
            if tombstone:
                status = "ignored_tombstoned"
            else:
                prior = conn.execute("SELECT body_hash FROM events WHERE event_id=?", (ev["event_id"],)).fetchone()
                if prior:
                    if prior[0] == ev["body_hash"]:
                        status = "duplicate"
                    else:
                        status = "quarantined_pending"
                        reason = "event_id_conflict"
                        raw = json.dumps(ev, ensure_ascii=False)
                else:
                    conn.execute("SAVEPOINT item_projection")
                    try:
                        conn.execute(
                            """INSERT INTO events(event_id,body_hash,source_id,native_session_id,fact_kind,
                              native_fact_id,revision_key,source_order,occurred_at,content_json,received_at)
                              VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                            (ev["event_id"], ev["body_hash"], c["source_instance_id"], c["native_session_id"],
                             c["fact_kind"], c["native_fact_id"], c["revision_key"], c.get("source_order"),
                             c.get("occurred_at"), json.dumps(c, ensure_ascii=False), received),
                        )
                        project(conn, ev)
                        conn.execute("RELEASE SAVEPOINT item_projection")
                    except (KeyError, TypeError, ValueError) as exc:
                        conn.execute("ROLLBACK TO SAVEPOINT item_projection")
                        conn.execute("RELEASE SAVEPOINT item_projection")
                        status = "quarantined_pending"
                        reason = f"projection_error:{type(exc).__name__}"
                        raw = json.dumps(ev, ensure_ascii=False)
            conn.execute(
                """INSERT INTO deliveries(collector_id,epoch,seq,event_id,body_hash,status,receipt_id,
                   reason,raw_json,received_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (device_id, epoch, seq, ev["event_id"], ev["body_hash"], status, receipt_id, reason, raw, received),
            )
            receipts.append({"seq": seq, "status": status, "receipt_id": receipt_id, "reason": reason})
        durable, projected = _acks(conn, device_id, epoch)
    return {"receipts": receipts, "durable_ack_seq": durable, "projected_ack_seq": projected, "server_epoch": current_server_epoch}


def resolve_quarantine(db: Database, device_id: str, receipt_id: str, body_hash: str) -> dict:
    with db.tx() as conn:
        row = conn.execute("SELECT epoch,seq,status,body_hash FROM deliveries WHERE collector_id=? AND receipt_id=?", (device_id, receipt_id)).fetchone()
        if row is None or row["body_hash"] != body_hash:
            raise HTTPException(404, "Quarantine receipt not found")
        if row["status"] not in {"quarantined_pending", "quarantined_resolved"}:
            raise HTTPException(409, "Receipt is not quarantined")
        conn.execute("UPDATE deliveries SET status='quarantined_resolved' WHERE collector_id=? AND receipt_id=?", (device_id, receipt_id))
        durable, projected = _acks(conn, device_id, row["epoch"])
        return {"status": "quarantined_resolved", "durable_ack_seq": durable, "projected_ack_seq": projected}
