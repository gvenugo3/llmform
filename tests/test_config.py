from __future__ import annotations

import json
from pathlib import Path

from llmform.config.interpolation import SecretScrubber, resolve_interpolations
from llmform.config.loader import load_project
from llmform.config.validate import validate_references

VALID = """\
version: "0.1"
providers:
  local:
    type: ollama
    endpoint: http://localhost:11434
models:
  default:
    provider: provider.local
    id: llama3.2
sources:
  api:
    type: http
    base_url: https://example.test
    operations:
      search:
        method: GET
        path: /search
        params:
          query: {type: string, required: true}
        returns: schemas/result.json
tools:
  search:
    source: source.api
    operation: search
    description: Search
    input: schemas/input.json
policies:
  safe:
    on: tool_call
    rule: "true"
agents:
  support:
    model: model.default
    instructions: prompts/support.md
    tools: [tool.search]
    policies: [policy.safe]
"""


def write_config(tmp_path: Path, text: str = VALID) -> Path:
    schemas = tmp_path / "schemas"
    schemas.mkdir(exist_ok=True)
    closed_object = {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
        "additionalProperties": False,
    }
    (schemas / "input.json").write_text(json.dumps(closed_object), encoding="utf-8")
    (schemas / "result.json").write_text(json.dumps(closed_object), encoding="utf-8")
    path = tmp_path / "llmform.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_valid_project_has_no_findings(tmp_path: Path) -> None:
    write_config(tmp_path)
    document = load_project(tmp_path)
    assert document.config is not None
    assert document.diagnostics == []
    assert validate_references(document) == []


def test_unknown_reference_has_exact_position(tmp_path: Path) -> None:
    write_config(tmp_path, VALID.replace("model.default", "model.missing"))
    document = load_project(tmp_path)
    findings = validate_references(document)
    error = next(item for item in findings if item.code == "LLMF300")
    assert error.position.line == document.position(("agents", "support", "model")).line
    assert "model.missing" in error.message


def test_duplicate_yaml_key_is_rejected(tmp_path: Path) -> None:
    write_config(tmp_path, "version: '0.1'\nversion: '0.2'\n")
    document = load_project(tmp_path)
    assert any(item.code == "LLMF002" and item.position.line == 2 for item in document.diagnostics)


def test_secret_is_resolved_and_scrubbed(tmp_path: Path) -> None:
    write_config(tmp_path, VALID.replace("http://localhost:11434", "${secret.API_TOKEN}"))
    document = load_project(tmp_path)
    resolved, diagnostics, scrubber = resolve_interpolations(
        document, environ={"API_TOKEN": "super-secret"}
    )
    assert diagnostics == []
    assert resolved["providers"]["local"]["endpoint"] == "super-secret"
    assert scrubber.scrub("failed: super-secret") == "failed: [REDACTED]"


def test_secret_provider_is_injected_and_structured_payloads_are_scrubbed(tmp_path: Path) -> None:
    class TestSecretProvider:
        def get(self, name: str) -> str | None:
            return {"API_TOKEN": "super-secret"}.get(name)

    write_config(tmp_path, VALID.replace("http://localhost:11434", "${secret.API_TOKEN}"))
    document = load_project(tmp_path)
    resolved, diagnostics, scrubber = resolve_interpolations(
        document,
        environ={},
        secret_provider=TestSecretProvider(),
    )

    assert diagnostics == []
    assert resolved["providers"]["local"]["endpoint"] == "super-secret"
    audit_payload = {
        "message": "request failed for super-secret",
        "nested": ["super-secret", {"value": "safe"}],
    }
    assert scrubber.scrub_value(audit_payload) == {
        "message": "request failed for [REDACTED]",
        "nested": ["[REDACTED]", {"value": "safe"}],
    }


def test_unresolved_interpolation_names_form_and_exact_position(tmp_path: Path) -> None:
    write_config(tmp_path, VALID.replace("http://localhost:11434", "${env.OLLAMA_URL}"))
    document = load_project(tmp_path)
    _, diagnostics, _ = resolve_interpolations(document, environ={})

    diagnostic = next(item for item in diagnostics if item.code == "LLMF200")
    assert "${env.OLLAMA_URL}" in diagnostic.message
    assert diagnostic.position == document.position(("providers", "local", "endpoint"))


def test_partial_interpolation_is_rejected_at_exact_position(tmp_path: Path) -> None:
    write_config(
        tmp_path,
        VALID.replace("http://localhost:11434", "https://${env.OLLAMA_HOST}"),
    )
    document = load_project(tmp_path)
    _, diagnostics, _ = resolve_interpolations(document, environ={"OLLAMA_HOST": "localhost"})

    diagnostic = next(item for item in diagnostics if item.code == "LLMF201")
    assert diagnostic.position == document.position(("providers", "local", "endpoint"))


def test_required_variable_needs_default_or_cli_value(tmp_path: Path) -> None:
    config = """\
variables:
  retries:
    type: integer
    required: true
"""
    (tmp_path / "llmform.yaml").write_text(config, encoding="utf-8")
    document = load_project(tmp_path)

    _, missing, _ = resolve_interpolations(document)
    resolved, supplied, _ = resolve_interpolations(document, {"retries": "3"})

    diagnostic = next(item for item in missing if item.code == "LLMF200")
    assert diagnostic.position == document.position(("variables", "retries"))
    assert "--var retries=VALUE" in diagnostic.message
    assert supplied == []
    assert resolved["variables"]["retries"]["required"] is True


def test_cli_variable_is_converted_to_its_declared_type(tmp_path: Path) -> None:
    config = """\
variables:
  retries:
    type: integer
models:
  default:
    provider: provider.local
    id: example
    max_tokens: ${var.retries}
"""
    (tmp_path / "llmform.yaml").write_text(config, encoding="utf-8")
    document = load_project(tmp_path)

    resolved, diagnostics, _ = resolve_interpolations(document, {"retries": "3"})

    assert diagnostics == []
    assert resolved["models"]["default"]["max_tokens"] == 3
    assert type(resolved["models"]["default"]["max_tokens"]) is int


def test_scrubber_ignores_empty_secrets() -> None:
    scrubber = SecretScrubber()
    scrubber.register("")
    assert scrubber.scrub("visible") == "visible"


def test_multiple_files_reject_duplicate_resource(tmp_path: Path) -> None:
    write_config(tmp_path, VALID)
    (tmp_path / "extra.llmform.yaml").write_text(
        "providers:\n  local:\n    type: ollama\n", encoding="utf-8"
    )
    document = load_project(tmp_path)
    assert any(item.code == "LLMF003" for item in document.diagnostics)
