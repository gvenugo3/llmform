"""Runtime hook dispatch with atomic transform staging."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from llmform.config.loader import ConfigDocument
from llmform.types import Hook, RecordedOutcome, RunState, Verdict

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
            applied.append(name)
            if policy.rule is None or self.evaluate(policy.rule, context):
                staged.extend(self.stage(transform.kind, payload) for transform in policy.transform)
                continue
            verdict = Verdict(policy.otherwise)
            if verdict != Verdict.ALLOW:
                return DispatchResult(verdict, payload, tuple(applied))
        if not applied and hook == Hook.TOOL_CALL and agent.default_tool_posture == "deny":
            return DispatchResult(Verdict.DENY, payload, ())
        for commit in staged:
            payload = commit()
        outcome = RecordedOutcome.TRANSFORM if staged else RecordedOutcome.NOT_APPLICABLE
        return DispatchResult(outcome, payload, tuple(applied))

    def admit_tool_result(self, state: RunState, tool_name: str) -> RunState:
        """Record the declared classes when a tool result enters a run.

        Classification belongs to the source operation, rather than the result
        fields or any transforms applied to them.  Consequently this only ever
        adds classes; later redaction cannot lower the run's data ceiling.
        """

        tool = self.config.tools[tool_name.removeprefix("tool.")]
        source = self.config.sources[tool.source.removeprefix("source.")]
        operation = source.operations[tool.operation]
        classes = list(dict.fromkeys([*state.classes, *operation.classes]))
        return state.model_copy(update={"classes": classes})

    def dispatch_model_call(
        self,
        agent_name: str,
        state: RunState,
        payload: Any,
        model: Mapping[str, Any],
        context: Mapping[str, Any] | None = None,
    ) -> DispatchResult:
        """Dispatch a model call with its data classification derived from state."""

        model_context = dict(context or {})
        model_context["model"] = model
        model_context["messages"] = [message.model_dump(mode="json") for message in state.messages]
        model_context["data"] = {"classes": list(state.classes)}
        return self.dispatch(agent_name, Hook.MODEL_CALL, payload, model_context)

    @staticmethod
    def _matches(match: object, context: Mapping[str, Any]) -> bool:
        if match is None:
            return True
        for key, value in match.model_dump().items():
            if value is not None and context.get(key) != value:
                return False
        return True
