"""Wire contracts for version 1 ingestion."""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from .codec import event_id, sha256, utc_ms

FactKind = Literal[
    "session.observed", "message.observed", "run.observed", "usage.observed",
    "file.observed", "gap.observed",
]


def no_floats(value: Any) -> None:
    if isinstance(value, float):
        raise ValueError("Floating-point values are not allowed in canonical facts")
    if isinstance(value, dict):
        for key, nested in value.items():
            if not isinstance(key, str):
                raise ValueError("JSON object keys must be strings")
            no_floats(nested)
    elif isinstance(value, list):
        for nested in value:
            no_floats(nested)


class Quality(BaseModel):
    certainty: Literal["recorded", "derived", "estimated", "unknown"]
    rule_id: str | None = None
    reason: str | None = None


class Execution(BaseModel):
    physical_device_id: UUID | None = None
    environment_id: UUID | None = None
    basis: Literal["source_metadata", "live_observed", "user_confirmed", "unknown"]


class EventContent(BaseModel):
    source_instance_id: UUID
    native_session_id: str = Field(min_length=1, max_length=512)
    fact_kind: FactKind
    native_fact_id: str = Field(min_length=1, max_length=1024)
    revision_key: str = Field(min_length=1, max_length=256)
    source_order: int | None = Field(default=None, ge=0)
    occurred_at: str | None = None
    supersedes_event_id: str | None = None
    quality: Quality
    execution: Execution
    payload: dict[str, Any]

    @model_validator(mode="after")
    def validate_fact(self) -> "EventContent":
        if self.occurred_at is not None and utc_ms(self.occurred_at) is None:
            raise ValueError("occurred_at must be a timestamp with a timezone")
        no_floats(self.payload)
        p = self.payload
        match self.fact_kind:
            case "message.observed":
                for required in ("native_message_id", "role", "input_origin", "content_state", "finalized"):
                    if required not in p:
                        raise ValueError(f"message payload missing {required}")
                content_state = p["content_state"]
                has_body = isinstance(p.get("body"), str)
                has_ref = isinstance(p.get("body_ref"), dict)
                if content_state in {"full", "redacted"} and has_body == has_ref:
                    raise ValueError("full/redacted message requires exactly one body or body_ref")
                if content_state in {"stats_only", "source_only", "missing"}:
                    if has_body or has_ref or not p.get("omission_reason"):
                        raise ValueError("omitted message requires a reason and no body")
            case "run.observed":
                for required in ("native_turn_id", "status"):
                    if required not in p:
                        raise ValueError(f"run payload missing {required}")
            case "usage.observed":
                for required in ("usage_key", "quantity_semantics", "coverage_scope"):
                    if required not in p:
                        raise ValueError(f"usage payload missing {required}")
        return self


class Envelope(BaseModel):
    schema_version: Literal[1]
    event_id: str = Field(pattern="^[a-f0-9]{64}$")
    body_hash: str = Field(pattern="^[a-f0-9]{64}$")
    content: EventContent

    @model_validator(mode="after")
    def check_hashes(self) -> "Envelope":
        content = self.content.model_dump(mode="json")
        if self.event_id != event_id(content):
            raise ValueError("event_id does not match the fact identity")
        if self.body_hash != sha256(content):
            raise ValueError("body_hash does not match canonical content")
        return self


class Observation(BaseModel):
    observed_at: str
    adapter_version: str
    source_locator: dict[str, Any]


class BatchEntry(BaseModel):
    seq: int = Field(ge=1)
    observation: Observation
    event: Envelope


class Batch(BaseModel):
    batch_id: UUID
    collector_id: UUID
    outbox_epoch: UUID
    entries: list[BatchEntry] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def unique_seq(self) -> "Batch":
        values = [entry.seq for entry in self.entries]
        if len(values) != len(set(values)):
            raise ValueError("duplicate outbox sequence in batch")
        return self
