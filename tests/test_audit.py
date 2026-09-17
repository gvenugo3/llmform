from llmform.audit import AuditLog, _safe_payload, verify
from llmform.providers import ProviderError, ProviderErrorKind, Usage, assistant
from llmform.runtime import MemoryStore, cel
from llmform.types import Principal, RunState


def test_audit_chain_detects_tampering(tmp_path) -> None:
    log = AuditLog("run-1", path=tmp_path / "audit.jsonl")
    first = log.append("decision", hook="tool_call", policy="limit", verdict="DENY")
    second = log.append("transform", policy="redact", fields=["$.card"], payload={"card": "secret"})

    assert first.prev_sha256 is None
    assert second.payload is None
    assert verify(log.records)
    assert not verify([second, first])


def test_audit_defaults_to_stderr(capsys) -> None:
    AuditLog("run-2").append("decision", verdict="ALLOW")
    assert '"run_id":"run-2"' in capsys.readouterr().err


def test_recorded_payload_never_contains_plaintext() -> None:
    records = []
    record = AuditLog("run-3", record_payloads=True, emit=records.append).append(
        "decision", payload={"token": "super-secret"}
    )
    assert "super-secret" not in record.model_dump_json()
    assert records == [record]


def test_provider_error_is_typed() -> None:
    error = ProviderError(ProviderErrorKind.TRANSIENT, "retry")
    assert error.kind == ProviderErrorKind.TRANSIENT


def test_memory_store_and_cel() -> None:
    state = RunState(
        run_id="one", closure="sha256:x", agent="agent", principal=Principal(role="dev")
    )
    store = MemoryStore()
    store.save(state)
    assert store.load("one") == state
    assert cel("cost_usd < 1", {"cost_usd": 0})


def test_safe_payload_keeps_non_secret_scalars() -> None:
    assert _safe_payload([None, True, 3]) == [None, True, 3]


def test_provider_completion_helper() -> None:
    assert assistant("ok", [], Usage(input_tokens=1, output_tokens=1)).message.content == "ok"
