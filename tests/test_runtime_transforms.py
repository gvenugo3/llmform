"""End-to-end transform and token-vault behaviour through the runtime loop (issue #14)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from llmform.cli import app
from llmform.config.loader import load_project
from llmform.policy.transforms import (
    TOKEN_PREFIX,
    InMemoryTokenVault,
    Origin,
    PersistentEncryptedTokenVault,
    TransformError,
)
from llmform.providers import Completion, Provider, ToolDefinition, Usage
from llmform.runtime import Loop
from llmform.sources import Source
from llmform.types import Message, Principal, RunState, ToolCall

ACCOUNT = "ACCT-1234"
SSN = "123-45-6789"
NOTES = "prefers phone calls"

CONFIG = """\
version: "0.1"
providers:
  local: {type: ollama}
models:
  default: {provider: provider.local, id: test}
sources:
  crm:
    type: http
    base_url: https://crm.test
    operations:
      get_customer:
        method: GET
        path: /customers
        params:
          account: {type: string, required: true}
        returns: schemas/customer.json
tools:
  lookup:
    source: source.crm
    operation: get_customer
    description: Look up a customer
    input: schemas/lookup.json
    detokenize: [account]
policies:
  ingress:
    on: request
    transform: [{kind: tokenize, fields: [account]}]
  pii:
    on: tool_result
    match: {source: source.crm, operation: get_customer}
    transform:
      - {kind: tokenize, fields: [ssn]}
      - {kind: redact, fields: [notes]}
agents:
  support:
    model: model.default
    instructions: prompts/support.md
    input: schemas/input.json
    tools: [tool.lookup]
    policies: [policy.ingress, policy.pii]
"""


def _object(**properties: str) -> str:
    return json.dumps(
        {
            "type": "object",
            "properties": {name: {"type": kind} for name, kind in properties.items()},
            "required": sorted(properties),
            "additionalProperties": False,
        }
    )


def write_project(root: Path, config: str = CONFIG) -> Path:
    (root / "schemas").mkdir(exist_ok=True)
    (root / "prompts").mkdir(exist_ok=True)
    (root / "prompts/support.md").write_text("Help the customer.", encoding="utf-8")
    (root / "schemas/input.json").write_text(
        _object(account="string", question="string"), encoding="utf-8"
    )
    (root / "schemas/lookup.json").write_text(_object(account="string"), encoding="utf-8")
    (root / "schemas/customer.json").write_text(
        _object(name="string", ssn="string", notes="string"), encoding="utf-8"
    )
    (root / "llmform.yaml").write_text(config, encoding="utf-8")
    return root


def _user(messages: list[Message]) -> Message:
    return next(message for message in messages if message.role == "user")


class ScriptedProvider(Provider):
    """Looks up the account the caller supplied, then answers with a chosen field."""

    def __init__(self, answer: str = "ssn"):
        self.answer = answer
        self.requests: list[str] = []

    def complete(self, messages, *, model: str, tools: list[ToolDefinition], output_schema=None):
        self.requests.append(json.dumps([message.model_dump(mode="json") for message in messages]))
        usage = Usage(input_tokens=1, output_tokens=1)
        results = [result for message in messages for result in message.tool_results]
        if not results:
            account = json.loads(_user(messages).content)["account"]
            call = ToolCall(id="call-1", name="lookup", arguments={"account": account})
            return Completion(message=Message(role="assistant", tool_calls=[call]), usage=usage)
        request = json.loads(_user(messages).content)
        answer = request["account"] if self.answer == "account" else results[0].content["ssn"]
        return Completion(message=Message(role="assistant", content=answer), usage=usage)


class CrmSource(Source):
    def __init__(self) -> None:
        self.received: list[dict[str, Any]] = []

    def execute(self, operation: str, params: dict[str, Any], *, timeout: float) -> Any:
        self.received.append(params)
        return {"name": "Ada", "ssn": SSN, "notes": NOTES}


class RecordingVault(InMemoryTokenVault):
    def __init__(self) -> None:
        super().__init__()
        self.minted: list[tuple[str, Origin]] = []

    def put(self, run_id: str, plaintext: str, origin: Origin) -> str:
        self.minted.append((plaintext, origin))
        return super().put(run_id, plaintext, origin)


def run(root: Path, provider: Provider, source: Source, vault=None) -> tuple[Loop, RunState]:
    loop = Loop(load_project(root), provider, {"crm": source}, vault=vault)
    request = json.dumps({"account": ACCOUNT, "question": "What is on file?"})
    state = loop.start("support", Principal(role="developer"), request)
    while state.status == "running":
        state = loop.step(state)
    return loop, state


def test_fixture_project_validates_cleanly(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["validate", str(write_project(tmp_path))])
    assert result.exit_code == 0, result.output


def test_tokenized_values_never_reach_the_provider(tmp_path: Path) -> None:
    provider, source = ScriptedProvider(), CrmSource()
    _, state = run(write_project(tmp_path), provider, source)

    assert state.status == "completed", state.failure
    assert len(provider.requests) == 2
    for request in provider.requests:
        assert ACCOUNT not in request
        assert SSN not in request
        assert NOTES not in request
        assert TOKEN_PREFIX in request
    assert "[REDACTED]" in provider.requests[1]


def test_detokenize_field_gives_plaintext_only_to_the_tool(tmp_path: Path) -> None:
    source = CrmSource()
    run(write_project(tmp_path), ScriptedProvider(), source)
    assert source.received == [{"account": ACCOUNT}]


def test_token_in_a_non_detokenize_field_reaches_the_tool_unresolved(tmp_path: Path) -> None:
    source = CrmSource()
    config = CONFIG.replace("    detokenize: [account]\n", "")
    run(write_project(tmp_path, config), ScriptedProvider(), source)
    assert source.received[0]["account"].startswith(TOKEN_PREFIX)


def test_tool_result_token_is_not_restored_to_the_caller(tmp_path: Path) -> None:
    _, state = run(write_project(tmp_path), ScriptedProvider(answer="ssn"), CrmSource())
    assert state.status == "completed", state.failure
    assert state.result.startswith(TOKEN_PREFIX)
    assert SSN not in state.model_dump_json()


def test_request_token_is_restored_to_the_caller(tmp_path: Path) -> None:
    _, state = run(write_project(tmp_path), ScriptedProvider(answer="account"), CrmSource())
    assert state.status == "completed", state.failure
    assert state.result == ACCOUNT


def test_vault_is_purged_when_the_run_ends(tmp_path: Path) -> None:
    vault = RecordingVault()
    loop, state = run(write_project(tmp_path), ScriptedProvider(), CrmSource(), vault)
    assert state.status == "completed", state.failure
    assert vault.minted
    assert loop.vault.resolve(state.run_id, state.result) is None


def test_denied_request_mints_no_tokens(tmp_path: Path) -> None:
    config = CONFIG.replace(
        "  pii:\n",
        '  gate:\n    on: request\n    rule: "false"\n    otherwise: DENY\n  pii:\n',
    ).replace("[policy.ingress, policy.pii]", "[policy.ingress, policy.gate, policy.pii]")
    vault, provider = RecordingVault(), ScriptedProvider()
    _, state = run(write_project(tmp_path, config), provider, CrmSource(), vault)
    assert state.status == "denied"
    assert state.failure == "policy gate denied request"
    assert vault.minted == []
    assert provider.requests == []


def test_model_call_transform_applies_to_the_provider_request(tmp_path: Path) -> None:
    config = CONFIG.replace(
        "  pii:\n",
        "  outbound:\n    on: model_call\n"
        "    transform: [{kind: tokenize, fields: [content]}]\n  pii:\n",
    ).replace("[policy.ingress, policy.pii]", "[policy.outbound]")
    provider = FinalAnswer()
    loop = Loop(load_project(write_project(tmp_path, config)), provider, {})
    state = loop.step(loop.start("support", Principal(role="developer"), "hi"))
    assert state.status == "completed", state.failure
    sent = _user(provider.messages)
    assert sent.content.startswith(TOKEN_PREFIX)
    # The caller supplied "hi", so a model that echoes the token gives "hi" back.
    assert state.result == "hi"


def test_approved_model_call_replays_the_hook_transforms(tmp_path: Path) -> None:
    config = CONFIG.replace(
        "  pii:\n",
        '  approve:\n    on: model_call\n    rule: "false"\n    otherwise: REQUIRE_APPROVAL\n'
        "    approval: {approvers: 'principal.role == \"supervisor\"'}\n"
        "  outbound:\n    on: model_call\n"
        "    transform: [{kind: tokenize, fields: [content]}]\n  pii:\n",
    ).replace("[policy.ingress, policy.pii]", "[policy.approve, policy.outbound]")
    provider = FinalAnswer()
    loop = Loop(load_project(write_project(tmp_path, config)), provider, {})
    suspended = loop.step(loop.start("support", Principal(role="supervisor"), "hi"))
    assert suspended.status == "suspended"
    resumed = loop.step(suspended, approved=True)
    assert resumed.status == "completed", resumed.failure
    assert _user(provider.messages).content.startswith(TOKEN_PREFIX)


def test_approval_at_tool_call_fails_closed(tmp_path: Path) -> None:
    config = CONFIG.replace(
        "  pii:\n",
        '  review:\n    on: tool_call\n    rule: "false"\n    otherwise: REQUIRE_APPROVAL\n'
        "    approval: {approvers: 'principal.role == \"supervisor\"'}\n  pii:\n",
    ).replace("[policy.ingress, policy.pii]", "[policy.ingress, policy.review, policy.pii]")
    source = CrmSource()
    _, state = run(write_project(tmp_path, config), ScriptedProvider(), source)
    assert state.status == "denied"
    assert "requires approval at tool_call" in (state.failure or "")
    assert source.received == []


def test_transform_on_a_non_object_payload_fails_the_run(tmp_path: Path) -> None:
    source = CrmSource()
    source.execute = lambda operation, params, *, timeout: ["not", "an", "object"]
    _, state = run(write_project(tmp_path), ScriptedProvider(), source)
    assert state.status == "failed"
    assert state.failure == "transform failed: tokenize requires an object payload, not list"


class FinalAnswer(Provider):
    """Echoes the first user message, as a model that repeats a token would."""

    def __init__(self) -> None:
        self.messages: list[Message] = []

    def complete(self, messages, *, model: str, tools: list[ToolDefinition], output_schema=None):
        self.messages = list(messages)
        reply = Message(role="assistant", content=_user(messages).content)
        return Completion(message=reply, usage=Usage(input_tokens=1, output_tokens=1))


def test_persistent_encrypted_token_vault_roundtrip_and_non_leakage(tmp_path: Path) -> None:
    vault = PersistentEncryptedTokenVault(tmp_path / "vault", key="my-secret-vault-key-1234567890!")
    secret_value = "SUPER_SECRET_SSN_987-65-4321"
    run_id = "run-test-123"
    closure = "sha256:closure_hash_abc"

    token = vault.put(run_id, secret_value, Origin.REQUEST, closure=closure)
    assert token.startswith(TOKEN_PREFIX)

    # Resolve token
    rec = vault.resolve(run_id, token, closure=closure)
    assert rec is not None
    assert rec.plaintext == secret_value
    assert rec.origin == Origin.REQUEST

    # Check persistence and non-leakage
    vault_file = tmp_path / "vault" / f"{run_id}.vault"
    assert vault_file.is_file()
    encrypted_bytes = vault_file.read_bytes()

    # Plaintext must NOT appear in the encrypted payload
    assert secret_value.encode("utf-8") not in encrypted_bytes
    assert b"SUPER_SECRET" not in encrypted_bytes

    # Magic header LFV1 must be present
    assert encrypted_bytes[:4] == b"LFV1"

    # Purge removes file
    vault.purge(run_id)
    assert not vault_file.is_file()


def test_persistent_encrypted_token_vault_closure_mismatch_refused(tmp_path: Path) -> None:
    vault = PersistentEncryptedTokenVault(tmp_path / "vault", key="vault-key")
    run_id = "run-closure-test"
    token = vault.put(run_id, "data", Origin.REQUEST, closure="sha256:correct_hash")

    # Accessing with mismatched closure must be refused
    import pytest

    with pytest.raises(TransformError, match="Closure mismatch"):
        vault.resolve(run_id, token, closure="sha256:wrong_hash")


def test_persistent_encrypted_token_vault_tampered_refused(tmp_path: Path) -> None:
    vault = PersistentEncryptedTokenVault(tmp_path / "vault", key="vault-key")
    run_id = "run-tamper-test"
    token = vault.put(run_id, "data", Origin.REQUEST)

    vault_file = tmp_path / "vault" / f"{run_id}.vault"
    tampered = bytearray(vault_file.read_bytes())
    # Tamper with a ciphertext byte
    tampered[-1] ^= 0xFF
    vault_file.write_bytes(tampered)

    import pytest

    with pytest.raises(TransformError, match="integrity check failed"):
        vault.resolve(run_id, token)


def test_suspended_run_resumes_across_process_restart_with_persistent_vault(tmp_path: Path) -> None:
    config = CONFIG.replace(
        "  pii:\n",
        '  approve:\n    on: model_call\n    rule: "false"\n    otherwise: REQUIRE_APPROVAL\n'
        "    approval: {approvers: 'principal.role == \"supervisor\"'}\n  pii:\n",
    ).replace("[policy.ingress, policy.pii]", "[policy.ingress, policy.approve, policy.pii]")

    project_dir = write_project(tmp_path, config)
    doc1 = load_project(project_dir)

    request = json.dumps({"account": ACCOUNT, "question": "What is on file?"})

    # First loop runs and suspends on model_call
    loop1 = Loop(doc1, ScriptedProvider(), {"crm": CrmSource()})
    suspended = loop1.step(loop1.start("support", Principal(role="supervisor"), request))
    assert suspended.status == "suspended"

    # Save state to json as if serializing to store/disk
    saved_state_json = suspended.model_dump_json()

    # Second loop in fresh instance (simulating process restart)
    doc2 = load_project(project_dir)
    loop2 = Loop(doc2, ScriptedProvider(), {"crm": CrmSource()})
    restored_state = RunState.model_validate_json(saved_state_json)

    # Resume the suspended run
    resumed = loop2.step(restored_state, approved=True)
    while resumed.status in {"running", "suspended"}:
        if resumed.status == "suspended":
            resumed = loop2.step(resumed, approved=True)
        else:
            resumed = loop2.step(resumed)
    assert resumed.status == "completed", resumed.failure
