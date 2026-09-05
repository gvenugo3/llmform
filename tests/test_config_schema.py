from __future__ import annotations

import json
from importlib.resources import files

from pydantic import TypeAdapter

from llmform.config.loader import load_project
from llmform.config.models import (
    Agent,
    Model,
    Policy,
    ProjectConfig,
    Provider,
    Source,
    Tool,
    Variable,
)
from llmform.schemas import CONFIG_SCHEMA_VERSION


def test_all_configuration_schema_artifacts_are_packaged() -> None:
    expected = {
        "agent.json",
        "model.json",
        "policy.json",
        "project.json",
        "provider.json",
        "source.json",
        "tool.json",
        "variable.json",
    }
    root = files("llmform.schemas")
    assert expected <= {item.name for item in root.iterdir()}
    for name in expected:
        schema = json.loads(root.joinpath(name).read_text(encoding="utf-8"))
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert schema["x-llmform-schema-version"] == CONFIG_SCHEMA_VERSION
        assert f"/v{CONFIG_SCHEMA_VERSION}/" in schema["$id"]


def test_committed_schemas_match_strict_models() -> None:
    models = {
        "agent": Agent,
        "model": Model,
        "policy": Policy,
        "project": ProjectConfig,
        "provider": Provider,
        "source": Source,
        "tool": Tool,
        "variable": Variable,
    }
    root = files("llmform.schemas")
    for name, model in models.items():
        committed = json.loads(root.joinpath(f"{name}.json").read_text(encoding="utf-8"))
        generated = TypeAdapter(model).json_schema(mode="validation")
        for metadata in ("$id", "$schema", "x-llmform-schema-version"):
            committed.pop(metadata)
        assert committed == generated, f"regenerate {name}.json"


def test_l0_rejects_misspelled_kind_at_key_position(tmp_path) -> None:
    text = 'version: "0.1"\nmdoels: {}\n'
    (tmp_path / "llmform.yaml").write_text(text, encoding="utf-8")
    document = load_project(tmp_path)
    diagnostic = next(item for item in document.diagnostics if item.code == "LLMF100")
    assert diagnostic.message == "unknown field 'mdoels'"
    assert diagnostic.position == document.key_position(("mdoels",))
    assert diagnostic.position.line == 2
    assert diagnostic.position.column == 1


def test_l0_rejects_missing_required_resource_field_with_position(tmp_path) -> None:
    text = 'version: "0.1"\nproviders:\n  local:\n    endpoint: http://localhost:11434\n'
    (tmp_path / "llmform.yaml").write_text(text, encoding="utf-8")
    document = load_project(tmp_path)
    diagnostic = next(item for item in document.diagnostics if item.code == "LLMF100")
    assert "'type' is a required property" in diagnostic.message
    assert diagnostic.position == document.position(("providers", "local"))
    assert diagnostic.position.line == 4


def test_l0_rejects_wrong_scalar_at_value_position(tmp_path) -> None:
    text = 'version: "0.1"\naudit:\n  record_payloads: "false"\n'
    (tmp_path / "llmform.yaml").write_text(text, encoding="utf-8")
    document = load_project(tmp_path)
    diagnostic = next(item for item in document.diagnostics if item.code == "LLMF100")
    path = ("audit", "record_payloads")
    assert "is not of type 'boolean'" in diagnostic.message
    assert diagnostic.position == document.position(path)
    assert diagnostic.position.line == 3
    assert diagnostic.position.column == 20


def test_l0_rejects_unknown_schema_version(tmp_path) -> None:
    (tmp_path / "llmform.yaml").write_text('version: "0.2"\n', encoding="utf-8")
    document = load_project(tmp_path)
    diagnostic = next(item for item in document.diagnostics if item.code == "LLMF100")
    assert "'0.1' was expected" in diagnostic.message
    assert diagnostic.position == document.position(("version",))
