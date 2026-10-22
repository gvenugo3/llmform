"""Run-scoped redaction and whole-field tokenization."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

REDACTION_MARKER = "[REDACTED]"
TOKEN_PREFIX = "{{llmform:token:"
TOKEN_SUFFIX = "}}"


class TransformError(ValueError):
    """A declared transform cannot be applied to the payload at its hook."""


class Origin(StrEnum):
    REQUEST = "request"
    TOOL_RESULT = "tool_result"


@dataclass(frozen=True)
class TokenRecord:
    plaintext: str
    origin: Origin


class TokenVault(Protocol):
    """Protocol for token vaults."""

    def put(self, run_id: str, plaintext: str, origin: Origin, closure: str = "") -> str: ...
    def resolve(self, run_id: str, token: str, closure: str = "") -> TokenRecord | None: ...
    def purge(self, run_id: str) -> None: ...


class InMemoryTokenVault:
    """A run-keyed vault; callers must purge it when a run becomes terminal."""

    def __init__(self) -> None:
        self._records: dict[str, dict[str, TokenRecord]] = {}
        self._plain_tokens: dict[str, dict[tuple[str, Origin], str]] = {}
        self._closures: dict[str, str] = {}

    def put(self, run_id: str, plaintext: str, origin: Origin, closure: str = "") -> str:
        if closure:
            stored = self._closures.get(run_id)
            if stored and stored != closure:
                raise TransformError(
                    f"Closure mismatch for run {run_id}: access to token vault refused"
                )
            self._closures[run_id] = closure
        key = (plaintext, origin)
        known = self._plain_tokens.setdefault(run_id, {}).get(key)
        if known:
            return known
        token = f"{TOKEN_PREFIX}{secrets.token_urlsafe(18)}{TOKEN_SUFFIX}"
        self._records.setdefault(run_id, {})[token] = TokenRecord(plaintext, origin)
        self._plain_tokens[run_id][key] = token
        return token

    def resolve(self, run_id: str, token: str, closure: str = "") -> TokenRecord | None:
        if closure:
            stored = self._closures.get(run_id)
            if stored and stored != closure:
                raise TransformError(
                    f"Closure mismatch for run {run_id}: access to token vault refused"
                )
        return self._records.get(run_id, {}).get(token)

    def purge(self, run_id: str) -> None:
        self._records.pop(run_id, None)
        self._plain_tokens.pop(run_id, None)
        self._closures.pop(run_id, None)


class PersistentEncryptedTokenVault:
    """A persistent vault encrypting tokens on disk with closure enforcement.

    Uses authenticated encryption (HMAC-SHA256 + keystream) to protect token data.
    """

    def __init__(
        self,
        root: Path,
        *,
        key: bytes | str | None = None,
    ) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        if key is not None:
            self._key = key.encode("utf-8") if isinstance(key, str) else key
        else:
            env_key = os.environ.get("LLMFORM_VAULT_KEY")
            if env_key:
                self._key = env_key.encode("utf-8")
            else:
                key_file = self.root / ".vault.key"
                if key_file.is_file():
                    self._key = key_file.read_bytes()
                else:
                    self._key = secrets.token_bytes(32)
                    key_file.write_bytes(self._key)

    def _encrypt(self, plaintext: bytes) -> bytes:
        nonce = secrets.token_bytes(16)
        enc_k = hashlib.sha256(self._key + b":enc").digest()
        mac_k = hashlib.sha256(self._key + b":mac").digest()
        keystream = bytearray()
        i = 0
        while len(keystream) < len(plaintext):
            keystream.extend(hmac.new(enc_k, nonce + i.to_bytes(4, "big"), hashlib.sha256).digest())
            i += 1
        ct = bytes(b ^ k for b, k in zip(plaintext, keystream, strict=False))
        tag = hmac.new(mac_k, nonce + ct, hashlib.sha256).digest()
        return b"LFV1" + nonce + tag + ct

    def _decrypt(self, blob: bytes) -> bytes:
        if len(blob) < 52 or blob[:4] != b"LFV1":
            raise TransformError("Invalid or corrupted token vault payload")
        nonce = blob[4:20]
        tag = blob[20:52]
        ct = blob[52:]
        mac_k = hashlib.sha256(self._key + b":mac").digest()
        expected_tag = hmac.new(mac_k, nonce + ct, hashlib.sha256).digest()
        if not hmac.compare_digest(tag, expected_tag):
            raise TransformError("Token vault integrity check failed: wrong key or tampered file")
        enc_k = hashlib.sha256(self._key + b":enc").digest()
        keystream = bytearray()
        i = 0
        while len(keystream) < len(ct):
            keystream.extend(hmac.new(enc_k, nonce + i.to_bytes(4, "big"), hashlib.sha256).digest())
            i += 1
        return bytes(b ^ k for b, k in zip(ct, keystream, strict=False))

    def _vault_file(self, run_id: str) -> Path:
        safe_name = re.sub(r"[^a-zA-Z0-9_\-]", "_", run_id)
        return self.root / f"{safe_name}.vault"

    def _load_run(self, run_id: str, closure: str = "") -> dict[str, Any]:
        path = self._vault_file(run_id)
        if not path.is_file():
            return {
                "run_id": run_id,
                "closure": closure,
                "records": {},
                "plain_tokens": {},
            }
        blob = path.read_bytes()
        try:
            raw = self._decrypt(blob)
            data = json.loads(raw.decode("utf-8"))
        except TransformError:
            raise
        except Exception as exc:
            raise TransformError(f"Failed to read token vault: {exc}") from exc

        stored_closure = data.get("closure")
        if closure and stored_closure and stored_closure != closure:
            raise TransformError(
                f"Closure mismatch for run {run_id}: access to token vault refused"
            )
        if closure and not stored_closure:
            data["closure"] = closure
        return data

    def _save_run(self, run_id: str, data: dict[str, Any]) -> None:
        raw = json.dumps(data).encode("utf-8")
        encrypted = self._encrypt(raw)
        path = self._vault_file(run_id)
        path.write_bytes(encrypted)

    def put(self, run_id: str, plaintext: str, origin: Origin, closure: str = "") -> str:
        data = self._load_run(run_id, closure)
        key = f"{plaintext}::{origin.value}"
        known = data["plain_tokens"].get(key)
        if known:
            return known
        token = f"{TOKEN_PREFIX}{secrets.token_urlsafe(18)}{TOKEN_SUFFIX}"
        data["records"][token] = {"plaintext": plaintext, "origin": origin.value}
        data["plain_tokens"][key] = token
        if closure and not data.get("closure"):
            data["closure"] = closure
        self._save_run(run_id, data)
        return token

    def resolve(self, run_id: str, token: str, closure: str = "") -> TokenRecord | None:
        data = self._load_run(run_id, closure)
        rec = data["records"].get(token)
        if rec is None:
            return None
        return TokenRecord(plaintext=rec["plaintext"], origin=Origin(rec["origin"]))

    def purge(self, run_id: str) -> None:
        path = self._vault_file(run_id)
        if path.is_file():
            try:
                path.unlink()
            except OSError:
                pass


def transform(
    payload: dict[str, Any],
    fields: list[str],
    kind: str,
    vault: TokenVault,
    run_id: str,
    origin: Origin,
    closure: str = "",
) -> dict[str, Any]:
    """Apply a declared transform to named top-level fields without mutating input."""

    if not isinstance(payload, dict):
        raise TransformError(f"{kind} requires an object payload, not {type(payload).__name__}")
    result = dict(payload)
    for field in fields:
        if field not in result:
            continue
        if kind == "redact":
            result[field] = REDACTION_MARKER
        elif kind == "tokenize":
            value = str(result[field])
            try:
                resolved = vault.resolve(run_id, value, closure=closure)
            except TypeError:
                resolved = vault.resolve(run_id, value)
            if resolved is None:
                try:
                    result[field] = vault.put(run_id, value, origin, closure=closure)
                except TypeError:
                    result[field] = vault.put(run_id, value, origin)
        else:
            raise ValueError(f"unknown transform kind {kind!r}")
    return result


def detokenize(
    payload: dict[str, Any],
    fields: list[str],
    vault: TokenVault,
    run_id: str,
    *,
    caller: bool = False,
    closure: str = "",
) -> dict[str, Any]:
    """Restore only exact token fields, with caller access limited to request-origin tokens."""

    result = dict(payload)
    for field in fields:
        value = result.get(field)
        if not isinstance(value, str):
            continue
        try:
            record = vault.resolve(run_id, value, closure=closure)
        except TypeError:
            record = vault.resolve(run_id, value)
        if record is not None and (not caller or record.origin == Origin.REQUEST):
            result[field] = record.plaintext
    return result
