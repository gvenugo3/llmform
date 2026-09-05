from __future__ import annotations

from llmform.config.loader import load_project
from llmform.policy.engine import PolicyEngine
from llmform.types import Hook, Verdict
from tests.test_config import VALID, write_config


def test_later_denial_discards_staged_transforms(tmp_path) -> None:
    config = VALID.replace(
        'rule: "true"',
        'rule: "true"\n    transform: [{kind: redact, fields: [query]}]\n'
        '  deny:\n    on: tool_call\n    rule: "true"\n    otherwise: DENY',
    ).replace("policies: [policy.safe]", "policies: [policy.safe, policy.deny]")
    write_config(tmp_path, config)
    minted: list[str] = []
    engine = PolicyEngine(
        load_project(tmp_path),
        lambda rule, context: True,
        lambda kind, payload: lambda: minted.append(kind) or payload,
    )

    result = engine.dispatch("support", Hook.TOOL_CALL, {"query": "x"}, {})

    assert result.verdict == Verdict.DENY
    assert minted == []


def test_agent_policy_order_beats_declaration_order(tmp_path) -> None:
    config = VALID.replace(
        'rule: "true"',
        'rule: "true"\n  deny:\n    on: tool_call\n    rule: "true"\n    otherwise: DENY',
    ).replace("policies: [policy.safe]", "policies: [policy.deny, policy.safe]")
    write_config(tmp_path, config)
    engine = PolicyEngine(
        load_project(tmp_path), lambda rule, context: True, lambda kind, payload: lambda: payload
    )

    result = engine.dispatch("support", Hook.TOOL_CALL, {}, {})

    assert result.verdict == Verdict.DENY
    assert result.policies == ("deny",)
