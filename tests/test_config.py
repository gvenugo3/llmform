from __future__ import annotations

import json
from pathlib import Path

from llmform.config.interpolation import resolve_interpolations
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


def test_multiple_files_reject_duplicate_resource(tmp_path: Path) -> None:
    write_config(tmp_path, VALID)
    (tmp_path / "extra.llmform.yaml").write_text(
        "providers:\n  local:\n    type: ollama\n", encoding="utf-8"
    )
    document = load_project(tmp_path)
    assert any(item.code == "LLMF003" for item in document.diagnostics)
