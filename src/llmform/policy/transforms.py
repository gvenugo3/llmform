"""Run-scoped redaction and whole-field tokenization."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

REDACTION_MARKER = "[REDACTED]"
TOKEN_PREFIX = "{{llmform:token:"
TOKEN_SUFFIX = "}}"


class Origin(StrEnum):
    REQUEST = "request"
    TOOL_RESULT = "tool_result"


@dataclass(frozen=True)
class TokenRecord:
    plaintext: str
    origin: Origin


class InMemoryTokenVault:
    """A run-keyed vault; callers must purge it when a run becomes terminal."""

    def __init__(self) -> None:
        self._records: dict[str, dict[str, TokenRecord]] = {}
        self._plain_tokens: dict[str, dict[tuple[str, Origin], str]] = {}

    def put(self, run_id: str, plaintext: str, origin: Origin) -> str:
        key = (plaintext, origin)
        known = self._plain_tokens.setdefault(run_id, {}).get(key)
        if known:
            return known
        token = f"{TOKEN_PREFIX}{secrets.token_urlsafe(18)}{TOKEN_SUFFIX}"
        self._records.setdefault(run_id, {})[token] = TokenRecord(plaintext, origin)
        self._plain_tokens[run_id][key] = token
        return token

    def resolve(self, run_id: str, token: str) -> TokenRecord | None:
        return self._records.get(run_id, {}).get(token)

    def purge(self, run_id: str) -> None:
        self._records.pop(run_id, None)
        self._plain_tokens.pop(run_id, None)


def transform(
    payload: dict[str, Any],
    fields: list[str],
    kind: str,
    vault: InMemoryTokenVault,
    run_id: str,
    origin: Origin,
) -> dict[str, Any]:
    """Apply a declared transform to named top-level fields without mutating input."""

    result = dict(payload)
    for field in fields:
        if field not in result:
            continue
        if kind == "redact":
            result[field] = REDACTION_MARKER
        elif kind == "tokenize":
            result[field] = vault.put(run_id, str(result[field]), origin)
        else:
            raise ValueError(f"unknown transform kind {kind!r}")
    return result


def detokenize(
    payload: dict[str, Any],
    fields: list[str],
    vault: InMemoryTokenVault,
    run_id: str,
    *,
    caller: bool = False,
) -> dict[str, Any]:
    """Restore only exact token fields, with caller access limited to request-origin tokens."""

    result = dict(payload)
    for field in fields:
        value = result.get(field)
        if not isinstance(value, str):
            continue
        record = vault.resolve(run_id, value)
        if record is not None and (not caller or record.origin == Origin.REQUEST):
            result[field] = record.plaintext
    return result
