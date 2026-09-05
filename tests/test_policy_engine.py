from __future__ import annotations

from llmform.config.loader import load_project
from llmform.policy.engine import PolicyEngine
from llmform.types import Hook, Principal, RunState, Verdict
from tests.test_config import VALID, write_config


def test_later_denial_discards_staged_transforms(tmp_path) -> None:
    config = (
        VALID.replace(
            'rule: "true"',
            'rule: "true"\n    transform: [{kind: redact, fields: [query]}]\n'
            '  deny:\n    on: tool_call\n    rule: "true"\n    otherwise: DENY',
        )
        .replace('rule: "true"\n    otherwise: DENY', 'rule: "false"\n    otherwise: DENY')
        .replace("policies: [policy.safe]", "policies: [policy.safe, policy.deny]")
    )
    write_config(tmp_path, config)
    minted: list[str] = []
    engine = PolicyEngine(
        load_project(tmp_path),
        lambda rule, context: rule == "true",
        lambda kind, payload: lambda: minted.append(kind) or payload,
    )

    result = engine.dispatch("support", Hook.TOOL_CALL, {"query": "x"}, {})

    assert result.verdict == Verdict.DENY
    assert minted == []


def test_agent_policy_order_beats_declaration_order(tmp_path) -> None:
    config = (
        VALID.replace(
            'rule: "true"',
            'rule: "true"\n  deny:\n    on: tool_call\n    rule: "true"\n    otherwise: DENY',
        )
        .replace('rule: "true"\n    otherwise: DENY', 'rule: "false"\n    otherwise: DENY')
        .replace("policies: [policy.safe]", "policies: [policy.deny, policy.safe]")
    )
    write_config(tmp_path, config)
    engine = PolicyEngine(
        load_project(tmp_path),
        lambda rule, context: rule == "true",
        lambda kind, payload: lambda: payload,
    )

    result = engine.dispatch("support", Hook.TOOL_CALL, {}, {})

    assert result.verdict == Verdict.DENY
    assert result.policies == ("deny",)


def test_tool_result_classes_restrict_the_following_model_call_and_survive_resume(tmp_path) -> None:
    config = (
        VALID.replace(
            "returns: schemas/result.json",
            "returns: schemas/result.json\n        classes: [restricted]",
        )
        .replace("on: tool_call", "on: model_call")
        .replace(
            'rule: "true"',
            'rule: \'!data.classes.contains("restricted") || model.provider == "local"\'\n'
            "    otherwise: DENY",
        )
    )
    write_config(tmp_path, config)
    engine = PolicyEngine(
        load_project(tmp_path),
        lambda _rule, context: (
            "restricted" not in context["data"]["classes"]
            or context["model"]["provider"] == "local"
        ),
        lambda _kind, payload: lambda: payload,
    )
    state = RunState(
        run_id="run_1",
        closure="sha256:example",
        agent="agent.support",
        principal=Principal(role="developer"),
    )

    admitted = engine.admit_tool_result(state, "tool.search")
    resumed = RunState.model_validate_json(admitted.model_dump_json())
    denied = engine.dispatch_model_call(
        "support", resumed, payload={}, model={"provider": "remote", "id": "example"}
    )
    local = engine.dispatch_model_call(
        "support", resumed, payload={}, model={"provider": "local", "id": "example"}
    )

    assert state.classes == []
    assert admitted.classes == ["restricted"]
    assert resumed.classes == ["restricted"]
    assert denied.verdict == Verdict.DENY
    assert local.verdict != Verdict.DENY


def test_tool_result_classes_are_monotonic(tmp_path) -> None:
    config = VALID.replace(
        "returns: schemas/result.json", "returns: schemas/result.json\n        classes: [pii]"
    )
    write_config(tmp_path, config)
    engine = PolicyEngine(
        load_project(tmp_path), lambda _rule, _context: True, lambda _kind, payload: lambda: payload
    )
    state = RunState(
        run_id="run_1",
        closure="sha256:example",
        agent="agent.support",
        principal=Principal(role="developer"),
        classes=["restricted"],
    )

    admitted = engine.admit_tool_result(state, "search")

    assert admitted.classes == ["restricted", "pii"]
