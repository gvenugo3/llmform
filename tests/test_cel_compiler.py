from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from llmform.cli import app
from llmform.config.loader import load_project
from llmform.diagnostics import Position
from llmform.policy.cel.compiler import compile_expression, validate_policy_rules
from llmform.policy.cel.environment import HookEnvironment, build_hook_environment
from llmform.policy.cel.types import BOOL, INT, STRING, list_of, map_of, object_of
from llmform.types import Hook
from tests.test_config import VALID, write_config

runner = CliRunner()


def refund_project(tmp_path, rule: str) -> None:
    write_config(tmp_path, VALID.replace('rule: "true"', f"rule: '{rule}'"))
    (tmp_path / "schemas/input.json").write_text(
        json.dumps(
            {
                "type": "object",
                "properties": {"amount_cents": {"type": "integer"}},
                "required": ["amount_cents"],
                "additionalProperties": False,
            }
        ),
        encoding="utf-8",
    )


def test_valid_rule_compiles_to_bool(tmp_path) -> None:
    refund_project(tmp_path, 'args.amount_cents <= 10000 || principal.role == "supervisor"')
    document = load_project(tmp_path)
    environment, diagnostics = build_hook_environment(
        document, "support", Hook.TOOL_CALL, tool_name="search"
    )
    assert diagnostics == []
    assert environment is not None
    result = compile_expression(
        document.config.policies["safe"].rule,  # type: ignore[union-attr,arg-type]
        environment,
        document.position(("policies", "safe", "rule")),
    )
    assert result.diagnostics == []
    assert result.result_type == BOOL


def test_undefined_field_has_exact_yaml_column_and_suggestion(tmp_path) -> None:
    expression = "args.amount <= 100"
    refund_project(tmp_path, expression)
    document = load_project(tmp_path)
    findings = validate_policy_rules(document)
    finding = next(item for item in findings if item.code == "LLMF411")
    source_line = finding.position.file.read_text(encoding="utf-8").splitlines()[
        finding.position.line - 1
    ]
    assert finding.position.column == source_line.index("amount") + 1
    assert finding.message == "undefined field 'amount'"
    assert "schema: input.json" in (finding.hint or "")
    assert "did you mean 'amount_cents'?" in (finding.hint or "")


def test_validate_cli_renders_exit_criterion(tmp_path) -> None:
    refund_project(tmp_path, "args.amount <= 100")
    result = runner.invoke(app, ["validate", str(tmp_path)])
    assert result.exit_code == 1
    assert "undefined field 'amount'" in result.output
    assert "on tool.search (schema: input.json)" in result.output
    assert "did you mean 'amount_cents'?" in result.output


def test_non_boolean_rule_is_rejected(tmp_path) -> None:
    refund_project(tmp_path, "args.amount_cents")
    findings = validate_policy_rules(load_project(tmp_path))
    assert any(
        item.code == "LLMF412" and "must evaluate to bool" in item.message for item in findings
    )


def test_syntax_error_is_positioned_inside_rule(tmp_path) -> None:
    refund_project(tmp_path, "args.amount_cents <=")
    document = load_project(tmp_path)
    findings = validate_policy_rules(document)
    finding = next(item for item in findings if item.code == "LLMF410")
    assert finding.position.line == document.position(("policies", "safe", "rule")).line
    assert finding.position.column > 10


def test_incompatible_operands_are_rejected(tmp_path) -> None:
    refund_project(tmp_path, 'args.amount_cents < "many"')
    findings = validate_policy_rules(load_project(tmp_path))
    assert any("comparison operands have incompatible types" in item.message for item in findings)


def test_iteration_macros_are_rejected_by_cost_policy(tmp_path) -> None:
    config = VALID.replace('rule: "true"', "rule: 'data.classes.exists(x, x == \"pii\")'").replace(
        "on: tool_call", "on: model_call"
    )
    write_config(tmp_path, config)
    findings = validate_policy_rules(load_project(tmp_path))
    assert any(
        item.code == "LLMF413" and "iteration macro 'exists'" in item.message for item in findings
    )


def compile_test_expression(expression: str):
    environment = HookEnvironment(
        Hook.LOOP,
        {
            "flag": BOOL,
            "count": INT,
            "name": STRING,
            "items": list_of(INT),
            "labels": map_of(STRING, INT),
            "record": object_of(value=INT),
        },
    )
    return compile_expression(expression, environment, Position(Path("policy.yaml")))


@pytest.mark.parametrize(
    "expression",
    [
        "!flag",
        "flag && true",
        "count + 2 == 3",
        "count * 2 > 0",
        'name.startsWith("a")',
        'name.endsWith("z")',
        'name.matches("a.*")',
        "items.size() > 0",
        "size(labels) > 0",
        "items.contains(1)",
        "has(record.value)",
        "items[0] == 1",
        'labels["one"] == 1',
        "(flag ? 1 : 2) == 1",
        "[1, 2][0] == 1",
        '{"one": 1}["one"] == 1',
    ],
)
def test_supported_expression_families(expression: str) -> None:
    result = compile_test_expression(expression)
    assert result.diagnostics == []
    assert result.result_type == BOOL


@pytest.mark.parametrize(
    ("expression", "message"),
    [
        ("missing == 1", "undefined identifier"),
        ("name.missing == 1", "cannot select field"),
        ('count < "one"', "comparison operands"),
        ('count + "one" == 1', "arithmetic operands"),
        ("count && flag", "logical operators"),
        ("!count", "unary_not"),
        ('flag ? 1 : "one"', "conditional branches"),
        ("count ? 1 : 2", "conditional test"),
        ("size(count) > 0", "size() requires"),
        ("count.size() > 0", "size() requires"),
        ("count.contains(1)", "contains() requires"),
        ('items.contains("one")', "contains() argument"),
        ("name.unknown()", "unsupported CEL method"),
        ("int(count) == 1", "unsupported CEL function"),
        ("[] == []", "empty list literals"),
        ('[1, "one"] == [1, "one"]', "list literal elements"),
        ('{"one": 1, "two": "two"} == {}', "map literal entries"),
        ('items["zero"] == 1', "list index must be int"),
        ("labels[1] == 1", "map index has incompatible type"),
        ('record["value"] == 1', "indexing is supported only"),
        ("null == null", "unsupported CEL literal"),
    ],
)
def test_expression_type_errors(expression: str, message: str) -> None:
    result = compile_test_expression(expression)
    assert any(message in item.message for item in result.diagnostics)


def test_large_expression_exceeds_static_cost_limit() -> None:
    result = compile_test_expression(" || ".join(["true"] * 80))
    assert any(item.code == "LLMF413" for item in result.diagnostics)
