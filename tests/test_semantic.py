from __future__ import annotations

import json
from pathlib import Path

from llmform.config.loader import load_project
from llmform.config.semantic import validate_semantics
from llmform.diagnostics import Severity
from tests.test_config import VALID, write_config


def write_schema(
    path: Path, properties: dict[str, object], required: list[str] | None = None
) -> None:
    path.write_text(
        json.dumps(
            {
                "type": "object",
                "properties": properties,
                "required": required or [],
                "additionalProperties": False,
            }
        ),
        encoding="utf-8",
    )


def finding(tmp_path: Path, config: str, code: str):
    write_config(tmp_path, config)
    result = next(item for item in validate_semantics(load_project(tmp_path)) if item.code == code)
    assert result.position.line > 0
    assert result.position.column > 0
    return result


def test_tool_input_fields_must_be_accepted_by_operation(tmp_path: Path) -> None:
    config = VALID.replace("query: {type: string, required: true}", "limit: {type: integer}")
    result = finding(tmp_path, config, "LLMF500")
    assert "query" in result.message
    assert "not accepted" in result.message


def test_tool_input_must_include_required_operation_parameter(tmp_path: Path) -> None:
    config = VALID.replace(
        "query: {type: string, required: true}",
        "query: {type: string}\n          tenant: {type: string, required: true}",
    )
    result = finding(tmp_path, config, "LLMF500")
    assert "does not require operation parameter 'tenant'" in result.message


def test_required_operation_parameter_cannot_be_optional_in_tool_input(tmp_path: Path) -> None:
    write_config(tmp_path)
    write_schema(tmp_path / "schemas/input.json", {"query": {"type": "string"}})
    results = validate_semantics(load_project(tmp_path))
    result = next(item for item in results if item.code == "LLMF500")
    assert "does not require operation parameter 'query'" in result.message


def test_tool_output_must_narrow_operation_returns(tmp_path: Path) -> None:
    config = VALID.replace(
        "    input: schemas/input.json\n",
        "    input: schemas/input.json\n    output: schemas/output.json\n",
    )
    write_config(tmp_path, config)
    write_schema(tmp_path / "schemas/output.json", {"invented": {"type": "string"}})
    results = validate_semantics(load_project(tmp_path))
    result = next(item for item in results if item.code == "LLMF501")
    assert "does not narrow" in result.message
    assert result.position.line > 0


def test_match_keys_must_exist_at_hook(tmp_path: Path) -> None:
    config = VALID.replace('    rule: "true"', '    match: {tool: tool.search}\n    rule: "true"')
    config = config.replace("on: tool_call", "on: request")
    result = finding(tmp_path, config, "LLMF502")
    assert "'tool'" in result.message
    assert "request hook" in result.message


def test_referenced_data_class_must_be_reachable(tmp_path: Path) -> None:
    config = VALID.replace("on: tool_call", "on: model_call").replace(
        'rule: "true"', "rule: 'data.classes.contains(\"restriced\")'"
    )
    result = finding(tmp_path, config, "LLMF503")
    assert "'restriced'" in result.message
    assert "agent.support" in result.message


def test_require_approval_requires_approver_predicate(tmp_path: Path) -> None:
    config = VALID.replace('rule: "true"', 'rule: "false"').replace(
        '    rule: "false"', '    rule: "false"\n    otherwise: REQUIRE_APPROVAL'
    )
    result = finding(tmp_path, config, "LLMF504")
    assert "approval.approvers" in result.message
    assert "no implicit approver" in (result.hint or "")


def test_cost_enforcement_requires_declared_model_price(tmp_path: Path) -> None:
    config = VALID.replace(
        "    max_iterations: 8\n" if "    max_iterations: 8\n" in VALID else "", ""
    )
    config = config.replace(
        "    policies: [policy.safe]\n", "    policies: [policy.safe]\n    max_cost_usd: 1.0\n"
    )
    result = finding(tmp_path, config, "LLMF505")
    assert "model.default" in result.message
    assert "declared price" in result.message


def test_cost_rule_requires_declared_model_price(tmp_path: Path) -> None:
    config = VALID.replace("on: tool_call", "on: loop").replace(
        'rule: "true"', 'rule: "cost_usd < 1.0"'
    )
    result = finding(tmp_path, config, "LLMF505")
    assert "cost enforcement" in result.message


def test_detokenize_field_must_exist_in_input_schema(tmp_path: Path) -> None:
    config = VALID.replace(
        "    input: schemas/input.json\n",
        "    input: schemas/input.json\n    detokenize: [secret]\n",
    )
    result = finding(tmp_path, config, "LLMF506")
    assert "'secret'" in result.message


def test_detokenizing_into_write_operation_warns(tmp_path: Path) -> None:
    config = VALID.replace("method: GET", "method: POST").replace(
        "    input: schemas/input.json\n",
        "    input: schemas/input.json\n    detokenize: [query]\n",
    )
    result = finding(tmp_path, config, "LLMF507")
    assert result.severity == Severity.WARNING
    assert "write operation" in result.message
