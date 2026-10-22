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
        lambda transform: lambda payload: minted.append(transform.kind) or payload,
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
        lambda transform: lambda payload: payload,
    )

    result = engine.dispatch("support", Hook.TOOL_CALL, {}, {})

    assert result.verdict == Verdict.DENY
    assert result.policies == ("deny",)


def test_match_on_a_typed_address_applies_to_the_bare_runtime_name(tmp_path) -> None:
    config = VALID.replace('    rule: "true"', '    match: {tool: tool.search}\n    rule: "false"')
    write_config(tmp_path, config)
    engine = PolicyEngine(
        load_project(tmp_path),
        lambda rule, context: rule == "true",
        lambda transform: lambda payload: payload,
    )

    result = engine.dispatch("support", Hook.TOOL_CALL, {}, {"tool": "search"})

    assert result.verdict == Verdict.DENY
    assert result.policies == ("safe",)


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
        lambda _transform: lambda payload: payload,
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
        load_project(tmp_path),
        lambda _rule, _context: True,
        lambda _transform: lambda payload: payload,
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


def test_policy_cost_limit_enforcement(tmp_path) -> None:
    config = VALID.replace(
        '  safe:\n    on: tool_call\n    rule: "true"',
        "  safe:\n"
        "    on: tool_call\n"
        "    cost_limit:\n"
        "      max_cost_usd: 0.50\n"
        "    otherwise: DENY",
    )
    write_config(tmp_path, config)
    engine = PolicyEngine(
        load_project(tmp_path),
        lambda _rule, _context: True,
        lambda _transform: lambda payload: payload,
    )

    # Under budget
    res_under = engine.dispatch("support", Hook.TOOL_CALL, {}, {"cost_usd": 0.25})
    assert res_under.verdict != Verdict.DENY

    # At or over budget
    res_over = engine.dispatch("support", Hook.TOOL_CALL, {}, {"cost_usd": 0.50})
    assert res_over.verdict == Verdict.DENY
    assert res_over.policies == ("safe",)


def test_policy_rate_limit_enforcement(tmp_path, monkeypatch) -> None:
    config = VALID.replace(
        '  safe:\n    on: tool_call\n    rule: "true"',
        "  safe:\n"
        "    on: tool_call\n"
        "    rate_limit:\n"
        "      max_calls: 2\n"
        "      window: 10s\n"
        "    otherwise: DENY",
    )
    write_config(tmp_path, config)
    engine = PolicyEngine(
        load_project(tmp_path),
        lambda _rule, _context: True,
        lambda _transform: lambda payload: payload,
    )

    fake_time = 1000.0
    monkeypatch.setattr("time.monotonic", lambda: fake_time)

    # Call 1: Allowed
    r1 = engine.dispatch("support", Hook.TOOL_CALL, {}, {})
    assert r1.verdict != Verdict.DENY

    # Call 2: Allowed
    r2 = engine.dispatch("support", Hook.TOOL_CALL, {}, {})
    assert r2.verdict != Verdict.DENY

    # Call 3: Exceeded within 10s window -> DENY
    r3 = engine.dispatch("support", Hook.TOOL_CALL, {}, {})
    assert r3.verdict == Verdict.DENY
    assert r3.policies == ("safe",)

    # Advance time past 10s window: Allowed again
    fake_time = 1015.0
    r4 = engine.dispatch("support", Hook.TOOL_CALL, {}, {})
    assert r4.verdict != Verdict.DENY


def test_malformed_limits_validation() -> None:
    import pytest
    from pydantic import ValidationError

    from llmform.config.models import CostLimit, RateLimit

    # Invalid rate limit window
    with pytest.raises(ValidationError, match="malformed rate limit window"):
        RateLimit(max_calls=5, window="not-a-duration")

    # Invalid cost limit without tokens or usd
    with pytest.raises(
        ValidationError, match="requires at least one of max_cost_usd or max_tokens"
    ):
        CostLimit()
