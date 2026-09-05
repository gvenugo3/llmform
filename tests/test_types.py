from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from llmform import (
    Hook,
    Message,
    PendingApproval,
    Principal,
    RecordedOutcome,
    RunState,
    ToolCall,
    ToolResult,
    Verdict,
)


def example_state() -> RunState:
    return RunState(
        run_id="run_01K4",
        closure="sha256:0123456789abcdef",
        agent="agent.support",
        principal=Principal(
            id="developer@example.test",
            tenant="local",
            role="developer",
            scopes=["refund:read"],
            attrs={"team": "support"},
        ),
        messages=[
            Message(role="user", content="Look up order 42"),
            Message(
                role="assistant",
                tool_calls=[ToolCall(id="call_1", name="get_order", arguments={"order_id": 42})],
            ),
            Message(
                role="tool",
                tool_results=[
                    ToolResult(
                        tool_call_id="call_1",
                        name="get_order",
                        content={"total": 125.5, "refundable": True},
                    )
                ],
            ),
        ],
        classes=["pii", "restricted"],
        iteration=2,
        cost_usd=0.00125,
        elapsed=timedelta(seconds=1, milliseconds=250),
        pending=PendingApproval(
            hook=Hook.TOOL_CALL,
            policy="policy.refund_limit",
            rule="args.amount_cents <= 10000",
            approvers='principal.role == "supervisor"',
            payload={"tool": "tool.issue_refund", "args": {"amount_cents": 12500}},
        ),
    )


def test_run_state_json_round_trip_matches_golden() -> None:
    state = example_state()
    encoded = state.model_dump_json(indent=2)
    golden = Path(__file__).with_name("golden").joinpath("run_state.json")
    assert encoded + "\n" == golden.read_text(encoding="utf-8")
    assert RunState.model_validate_json(encoded) == state


def test_verdicts_and_recorded_outcomes_are_distinct() -> None:
    assert {item.value for item in Verdict} == {"ALLOW", "DENY", "REQUIRE_APPROVAL"}
    assert {item.value for item in RecordedOutcome} == {"TRANSFORM", "NOT_APPLICABLE"}


def test_message_rejects_provider_invalid_role_payload() -> None:
    with pytest.raises(ValidationError, match="only valid on assistant"):
        Message(
            role="user",
            tool_calls=[ToolCall(id="call_1", name="search", arguments={})],
        )


def test_public_types_reject_unknown_fields() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        Principal(role="developer", admin=True)  # type: ignore[call-arg]
