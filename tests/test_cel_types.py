from __future__ import annotations

import json
from pathlib import Path

import pytest

from llmform.config.loader import load_project
from llmform.policy.cel.environment import build_hook_environment
from llmform.policy.cel.schema import SchemaCompiler, compile_schema_file
from llmform.policy.cel.types import CelKind
from llmform.types import Hook
from tests.test_config import VALID, write_config


@pytest.mark.parametrize(
    ("schema", "kind"),
    [
        ({"type": "string"}, CelKind.STRING),
        ({"type": "number"}, CelKind.DOUBLE),
        ({"type": "integer"}, CelKind.INT),
        ({"type": "boolean"}, CelKind.BOOL),
        ({"type": "array", "items": {"type": "string"}}, CelKind.LIST),
        (
            {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
                "additionalProperties": False,
            },
            CelKind.OBJECT,
        ),
        ({"type": "string", "enum": ["open", "closed"]}, CelKind.STRING),
        (
            {
                "$defs": {"Name": {"type": "string"}},
                "$ref": "#/$defs/Name",
            },
            CelKind.STRING,
        ),
    ],
)
def test_supported_schema_constructs_compile(schema: object, kind: CelKind) -> None:
    result = SchemaCompiler(schema, Path("schema.json")).compile()
    assert result.diagnostics == []
    assert result.type is not None
    assert result.type.kind == kind
    assert result.type.kind != "dyn"


@pytest.mark.parametrize(
    ("schema", "message"),
    [
        ({"oneOf": [{"type": "string"}]}, "oneOf"),
        ({"type": ["string", "null"]}, "type arrays"),
        ({"type": "array", "items": [{"type": "string"}]}, "tuple-form"),
        ({"type": "object", "properties": {}}, "additionalProperties"),
        (
            {"type": "object", "properties": {}, "additionalProperties": True},
            "additionalProperties",
        ),
        ({"$ref": "https://example.test/schema.json"}, "this document's $defs"),
        ({"type": "string", "minLength": -1}, "invalid JSON Schema"),
        ({"type": "string", "pattern": "["}, "invalid JSON Schema"),
        ({"type": "integer", "enum": [1, 2]}, "enum is supported only for string schemas"),
        (
            {
                "$defs": {
                    "Node": {
                        "type": "object",
                        "properties": {"next": {"$ref": "#/$defs/Node"}},
                        "additionalProperties": False,
                    }
                },
                "$ref": "#/$defs/Node",
            },
            "cyclic $ref",
        ),
    ],
)
def test_unsupported_schema_constructs_are_diagnostics(schema: object, message: str) -> None:
    result = SchemaCompiler(schema, Path("schema.json")).compile()
    assert result.type is None or result.diagnostics
    assert any(message in item.message for item in result.diagnostics)


def test_invalid_json_reports_exact_position(tmp_path: Path) -> None:
    path = tmp_path / "broken.json"
    path.write_text('{\n  "type": nope\n}', encoding="utf-8")
    result = compile_schema_file(path)
    assert result.type is None
    assert result.diagnostics[0].position.line == 2
    assert result.diagnostics[0].position.column == 11


def test_hook_environments_use_declared_and_default_schemas(tmp_path: Path) -> None:
    config = VALID.replace(
        "    instructions: prompts/support.md\n",
        "    instructions: prompts/support.md\n"
        "    input: schemas/agent_input.json\n"
        "    output: schemas/agent_output.json\n",
    )
    write_config(tmp_path, config)
    schemas = {
        "agent_input.json": {
            "type": "object",
            "properties": {"ticket_id": {"type": "integer"}},
            "additionalProperties": False,
        },
        "agent_output.json": {
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "additionalProperties": False,
        },
    }
    for name, schema in schemas.items():
        (tmp_path / "schemas" / name).write_text(json.dumps(schema), encoding="utf-8")
    document = load_project(tmp_path)

    request, diagnostics = build_hook_environment(document, "support", Hook.REQUEST)
    assert diagnostics == []
    assert request is not None
    assert request.variables["input"].field("ticket_id").kind == CelKind.INT

    tool_call, diagnostics = build_hook_environment(
        document, "agent.support", Hook.TOOL_CALL, tool_name="tool.search"
    )
    assert diagnostics == []
    assert tool_call is not None
    assert tool_call.variables["args"].field("query").kind == CelKind.STRING

    tool_result, diagnostics = build_hook_environment(
        document, "support", Hook.TOOL_RESULT, tool_name="search"
    )
    assert diagnostics == []
    assert tool_result is not None
    assert tool_result.variables["result"].kind == CelKind.OBJECT

    response, diagnostics = build_hook_environment(document, "support", Hook.RESPONSE)
    assert diagnostics == []
    assert response is not None
    assert response.variables["draft"].field("answer").kind == CelKind.STRING


def test_agent_hook_defaults_are_statically_typed(tmp_path: Path) -> None:
    write_config(tmp_path)
    document = load_project(tmp_path)
    request, request_diagnostics = build_hook_environment(document, "support", Hook.REQUEST)
    response, response_diagnostics = build_hook_environment(document, "support", Hook.RESPONSE)
    assert request_diagnostics == response_diagnostics == []
    assert request is not None and response is not None
    assert request.variables["input"].field("message").kind == CelKind.STRING
    assert response.variables["draft"].field("text").kind == CelKind.STRING


@pytest.mark.parametrize(
    ("hook", "expected"),
    [
        (Hook.MODEL_CALL, {"model", "messages", "data"}),
        (Hook.LOOP, {"iteration", "cost_usd", "elapsed"}),
    ],
)
def test_builtin_hook_contexts(tmp_path: Path, hook: Hook, expected: set[str]) -> None:
    write_config(tmp_path)
    document = load_project(tmp_path)
    environment, diagnostics = build_hook_environment(document, "support", hook)
    assert diagnostics == []
    assert environment is not None
    assert {"principal", "agent", "run", *expected} <= environment.variables.keys()
