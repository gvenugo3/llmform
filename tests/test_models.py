from __future__ import annotations

from llmform.config.interpolation import resolve_interpolations
from llmform.config.loader import attach_model_positions, load_project
from llmform.config.models import ProjectConfig
from tests.test_config import VALID, write_config


def test_strict_model_does_not_coerce_quoted_number(tmp_path) -> None:
    config = VALID.replace("    id: llama3.2\n", '    id: llama3.2\n    temperature: "0.3"\n')
    write_config(tmp_path, config)
    document = load_project(tmp_path)
    diagnostic = next(item for item in document.diagnostics if item.code == "LLMF100")
    path = ("models", "default", "temperature")
    assert diagnostic.position == document.position(path)
    assert diagnostic.position.line == document.key_position(path).line
    assert diagnostic.position.column > document.key_position(path).column


def test_variable_default_must_match_declared_type(tmp_path) -> None:
    config = 'variables:\n  retries:\n    type: integer\n    default: "three"\n'
    (tmp_path / "llmform.yaml").write_text(config, encoding="utf-8")
    document = load_project(tmp_path)
    diagnostic = next(item for item in document.diagnostics if item.code == "LLMF100")
    path = ("variables", "retries", "default")
    assert "default must have declared type integer" in diagnostic.message
    assert diagnostic.position == document.position(path)
    assert diagnostic.position.line == 4


def test_nested_typed_fields_carry_key_and_value_locations(tmp_path) -> None:
    write_config(tmp_path)
    document = load_project(tmp_path)
    assert document.config is not None
    agent = document.config.agents["support"]
    location = agent.field_location("model")
    assert location is not None
    assert location.key == document.key_position(("agents", "support", "model"))
    assert location.value == document.position(("agents", "support", "model"))

    operation = document.config.sources["api"].operations["search"]
    returns_location = operation.field_location("returns")
    assert returns_location is not None
    assert returns_location.value == document.position(
        ("sources", "api", "operations", "search", "returns")
    )


def test_interpolated_model_tree_retains_source_locations(tmp_path) -> None:
    config = VALID.replace("endpoint: http://localhost:11434", "endpoint: ${env.OLLAMA_URL}")
    write_config(tmp_path, config)
    document = load_project(tmp_path)
    resolved, diagnostics, _ = resolve_interpolations(
        document, environ={"OLLAMA_URL": "http://localhost:11434"}
    )
    assert diagnostics == []
    reparsed = ProjectConfig.model_validate(resolved)
    attach_model_positions(reparsed, document)
    location = reparsed.providers["local"].field_location("endpoint")
    assert location is not None
    assert location.value == document.position(("providers", "local", "endpoint"))


def test_typed_ast_serialization_contains_no_position_metadata(tmp_path) -> None:
    write_config(tmp_path)
    document = load_project(tmp_path)
    assert document.config is not None
    dumped = document.config.model_dump()
    assert "_field_locations" not in repr(dumped)
    assert dumped["agents"]["support"]["model"] == "model.default"
