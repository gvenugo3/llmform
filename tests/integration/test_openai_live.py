"""Opt-in integration tests against the live OpenAI Responses API.

These tests skip unless ``LLMFORM_OPENAI_INTEGRATION=1``. When enabled they require
``OPENAI_API_KEY`` and fail, rather than skip, if it is missing, so a configured run
can never pass without calling the API. ``LLMFORM_OPENAI_MODEL`` selects the model.
The ``OpenAI integration`` workflow sets all three; see the README.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from llmform.cli import app
from llmform.config.loader import load_project
from llmform.policy.transforms import TOKEN_PREFIX
from llmform.providers.http import OpenAIProvider
from llmform.runtime import Loop
from llmform.sources import Source
from llmform.types import Message, Principal

pytestmark = [
    pytest.mark.openai,
    pytest.mark.skipif(
        os.environ.get("LLMFORM_OPENAI_INTEGRATION") != "1",
        reason="set LLMFORM_OPENAI_INTEGRATION=1 to call the OpenAI API",
    ),
]

MODEL = os.environ.get("LLMFORM_OPENAI_MODEL") or "gpt-4.1-mini"
ACCOUNT = "ACCT-1234"
SSN = "123-45-6789"


@pytest.fixture
def api_key() -> str:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        pytest.fail("LLMFORM_OPENAI_INTEGRATION=1 but OPENAI_API_KEY is not set")
    return key


def _closed_object(**properties: str) -> str:
    return json.dumps(
        {
            "type": "object",
            "properties": {name: {"type": kind} for name, kind in properties.items()},
            "required": sorted(properties),
            "additionalProperties": False,
        }
    )


def test_run_command_completes_a_deterministic_prompt(tmp_path: Path, api_key: str) -> None:
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts/assistant.md").write_text(
        "You are an automated test fixture. Reply with exactly the single word PONG, "
        "in capital letters, and nothing else.",
        encoding="utf-8",
    )
    (tmp_path / "llmform.yaml").write_text(
        """\
version: "0.1"
variables:
  model:
    type: string
providers:
  openai:
    type: openai
    api_key: ${secret.OPENAI_API_KEY}
models:
  default:
    provider: provider.openai
    id: ${var.model}
policies:
  allow_openai:
    on: model_call
    rule: 'model.provider == "provider.openai"'
agents:
  assistant:
    model: model.default
    instructions: prompts/assistant.md
    policies: [policy.allow_openai]
""",
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        ["run", "assistant", "--path", str(tmp_path), "--input", "ping", "--var", f"model={MODEL}"],
    )

    leaked = api_key in result.output
    assert not leaked, "the API key appeared in the command output"
    assert result.exit_code == 0, result.output
    assert "PONG" in result.output.upper()
    records = [
        json.loads(line)
        for line in (tmp_path / "llmform.audit.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    decision = next(record for record in records if record["kind"] == "decision")
    assert decision["hook"] == "model_call"
    assert decision["policy"] == "allow_openai"
    leaked = any(api_key in json.dumps(record) for record in records)
    assert not leaked, "the API key appeared in the audit log"


class RecordingOpenAI(OpenAIProvider):
    """The real provider, keeping each provider-neutral request for inspection."""

    def __init__(self, api_key: str):
        super().__init__(api_key)
        self.requests: list[list[Message]] = []

    def complete(self, messages, **kwargs):
        self.requests.append(list(messages))
        return super().complete(messages, **kwargs)


class CustomerSource(Source):
    def __init__(self) -> None:
        self.received: list[dict[str, Any]] = []

    def execute(self, operation: str, params: dict[str, Any], *, timeout: float) -> Any:
        self.received.append(params)
        return {"name": "Ada Lovelace", "ssn": SSN, "notes": "prefers phone calls"}


def test_tool_round_trip_keeps_tokenized_values_from_the_model(
    tmp_path: Path, api_key: str
) -> None:
    (tmp_path / "schemas").mkdir()
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts/support.md").write_text(
        "You are an automated test fixture. Call the lookup tool exactly once, passing the "
        "account value from the user's JSON message unchanged, character for character. "
        "Then reply with only the customer's name from the tool result.",
        encoding="utf-8",
    )
    (tmp_path / "schemas/input.json").write_text(_closed_object(account="string"), encoding="utf-8")
    (tmp_path / "schemas/lookup.json").write_text(
        _closed_object(account="string"), encoding="utf-8"
    )
    (tmp_path / "schemas/customer.json").write_text(
        _closed_object(name="string", ssn="string", notes="string"), encoding="utf-8"
    )
    (tmp_path / "llmform.yaml").write_text(
        f"""\
version: "0.1"
providers:
  openai: {{type: openai, api_key: unused-the-test-builds-its-own-provider}}
models:
  default: {{provider: provider.openai, id: {MODEL}}}
sources:
  crm:
    type: http
    base_url: https://crm.test
    operations:
      get_customer:
        method: GET
        path: /customers
        params:
          account: {{type: string, required: true}}
        returns: schemas/customer.json
tools:
  lookup:
    source: source.crm
    operation: get_customer
    description: Look up a customer by account number.
    input: schemas/lookup.json
    detokenize: [account]
policies:
  ingress:
    on: request
    transform: [{{kind: tokenize, fields: [account]}}]
  pii:
    on: tool_result
    match: {{source: source.crm, operation: get_customer}}
    transform:
      - {{kind: tokenize, fields: [ssn]}}
      - {{kind: redact, fields: [notes]}}
agents:
  support:
    model: model.default
    instructions: prompts/support.md
    input: schemas/input.json
    tools: [tool.lookup]
    policies: [policy.ingress, policy.pii]
    max_iterations: 4
""",
        encoding="utf-8",
    )
    provider, source = RecordingOpenAI(api_key), CustomerSource()
    loop = Loop(load_project(tmp_path), provider, {"crm": source})

    state = loop.start("support", Principal(role="developer"), json.dumps({"account": ACCOUNT}))
    while state.status == "running":
        state = loop.step(state)

    assert state.status == "completed", state.failure
    assert source.received == [{"account": ACCOUNT}]
    assert "Ada" in str(state.result)
    for request in provider.requests:
        sent = json.dumps([message.model_dump(mode="json") for message in request])
        assert ACCOUNT not in sent
        assert SSN not in sent
        assert "prefers phone calls" not in sent
    assert TOKEN_PREFIX in json.dumps(provider.requests[-1][-1].model_dump(mode="json"))
