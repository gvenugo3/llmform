"""Runtime hook dispatch with atomic transform staging."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from llmform.config.loader import ConfigDocument
from llmform.types import Hook, RecordedOutcome, Verdict

RuleEvaluator = Callable[[str, Mapping[str, Any]], bool]
TransformStager = Callable[[str, Any], Callable[[], Any]]


@dataclass(frozen=True)
class DispatchResult:
    verdict: Verdict | RecordedOutcome
    payload: Any
    policies: tuple[str, ...]


class PolicyEngine:
    """Apply an agent's policies in declared order at one runtime hook."""

    def __init__(self, document: ConfigDocument, evaluate: RuleEvaluator, stage: TransformStager):
        if document.config is None:
            raise ValueError("policy engine requires a validated project")
        self.config = document.config
        self.evaluate = evaluate
        self.stage = stage

    def dispatch(
        self,
        agent_name: str,
        hook: Hook,
        payload: Any,
        context: Mapping[str, Any],
    ) -> DispatchResult:
        agent = self.config.agents[agent_name.removeprefix("agent.")]
        staged: list[Callable[[], Any]] = []
        applied: list[str] = []
        for reference in agent.policies:
            name = reference.removeprefix("policy.")
            policy = self.config.policies[name]
            if policy.on != hook.value or not self._matches(policy.match, context):
                continue
            if policy.rule is not None and not self.evaluate(policy.rule, context):
                continue
            applied.append(name)
            staged.extend(self.stage(transform.kind, payload) for transform in policy.transform)
            verdict = Verdict(policy.otherwise)
            if verdict != Verdict.ALLOW:
                return DispatchResult(verdict, payload, tuple(applied))
        if not applied and hook == Hook.TOOL_CALL and agent.default_tool_posture == "deny":
            return DispatchResult(Verdict.DENY, payload, ())
        for commit in staged:
            payload = commit()
        outcome = RecordedOutcome.TRANSFORM if staged else RecordedOutcome.NOT_APPLICABLE
        return DispatchResult(outcome, payload, tuple(applied))

    @staticmethod
    def _matches(match: object, context: Mapping[str, Any]) -> bool:
        if match is None:
            return True
        for key, value in match.model_dump().items():
            if value is not None and context.get(key) != value:
                return False
        return True
