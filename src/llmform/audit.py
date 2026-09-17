"""Tamper-evident, secret-safe audit records."""

from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from llmform.types import PublicModel


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()


def digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


def _safe_payload(value: Any) -> Any:
    """Preserve payload shape without retaining values that may be secrets."""

    if isinstance(value, dict):
        return {str(key): _safe_payload(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_safe_payload(item) for item in value]
    if value is None or isinstance(value, bool | int | float):
        return value
    # A digest is useful for correlating records, but cannot reveal plaintext.
    return {"sha256": digest(value)}


class AuditRecord(PublicModel):
    seq: int = Field(ge=1)
    prev_sha256: str | None = None
    ts: datetime
    run_id: str
    kind: Literal["decision", "transform", "approval"]
    hook: str | None = None
    policy: str | None = None
    verdict: str | None = None
    agent: str | None = None
    tool: str | None = None
    principal: dict[str, Any] | None = None
    data_classes: list[str] = Field(default_factory=list)
    input_sha256: str | None = None
    rule: str | None = None
    fields: list[str] = Field(default_factory=list)
    answers_seq: int | None = None
    payload: Any | None = None


def verify(records: Iterable[AuditRecord]) -> bool:
    """Return false when records are deleted, reordered, or altered."""

    previous: str | None = None
    expected = 1
    for record in records:
        if record.seq != expected or record.prev_sha256 != previous:
            return False
        previous = digest(record.model_dump(mode="json"))
        expected += 1
    return True


class AuditLog:
    def __init__(
        self,
        run_id: str,
        *,
        record_payloads: bool = False,
        path: Path | None = None,
        emit: Callable[[AuditRecord], None] | None = None,
    ):
        self.run_id, self.record_payloads, self.path = run_id, record_payloads, path
        self.emit = emit
        self.records: list[AuditRecord] = []

    def append(
        self, kind: Literal["decision", "transform", "approval"], **values: Any
    ) -> AuditRecord:
        payload = values.pop("payload", None)
        # Payloads must be explicitly requested; transform values and token maps never are.
        if kind == "transform":
            payload = None
        record = AuditRecord(
            seq=len(self.records) + 1,
            prev_sha256=digest(self.records[-1].model_dump(mode="json")) if self.records else None,
            ts=datetime.now(UTC),
            run_id=self.run_id,
            kind=kind,
            input_sha256=digest(payload)
            if payload is not None
            else values.pop("input_sha256", None),
            payload=_safe_payload(payload)
            if self.record_payloads and kind != "transform"
            else None,
            **values,
        )
        self.records.append(record)
        line = record.model_dump_json() + "\n"
        if self.path:
            with self.path.open("a", encoding="utf-8") as output:
                output.write(line)
        else:
            sys.stderr.write(line)
        if self.emit:
            self.emit(record)
        return record
