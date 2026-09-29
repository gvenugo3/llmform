"""Serializable agent loop and approval store."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from celpy import Environment
from celpy.celtypes import MapType
from jsonschema import validate

from llmform.audit import AuditLog
from llmform.config.loader import ConfigDocument
from llmform.config.models import Agent, Transform
from llmform.policy.engine import DispatchResult, PolicyEngine, TransformStager
from llmform.policy.transforms import InMemoryTokenVault, Origin, TransformError, detokenize
from llmform.policy.transforms import transform as apply_transform
from llmform.providers import Provider, ToolDefinition
from llmform.sources import Source
from llmform.types import (
    Hook,
    Message,
    PendingApproval,
    Principal,
    RecordedOutcome,
    RunState,
    ToolResult,
    Verdict,
)

TERMINAL = {"completed", "failed", "denied"}


class MemoryStore:
    def __init__(self):
        self.values: dict[str, RunState] = {}

    def save(self, state: RunState) -> None:
        self.values[state.run_id] = state

    def load(self, run_id: str) -> RunState:
        return self.values[run_id]


def cel(rule: str, context: dict[str, Any]) -> bool:
    def value(item: Any) -> Any:
        if isinstance(item, dict):
            return MapType({key: value(child) for key, child in item.items()})
        if isinstance(item, list):
            return [value(child) for child in item]
        return item

    return bool(Environment().program(Environment().compile(rule)).evaluate(value(context)))


class Loop:
    def __init__(
        self,
        document: ConfigDocument,
        provider: Provider,
        sources: dict[str, Source],
        *,
        audit_path: Path | None = None,
        vault: InMemoryTokenVault | None = None,
    ):
        if document.config is None:
            raise ValueError("loop requires validated project")
        self.document, self.config, self.provider, self.sources = (
            document,
            document.config,
            provider,
            sources,
        )
        self.audit_path = audit_path
        self.logs: dict[str, AuditLog] = {}
        self.vault = vault or InMemoryTokenVault()
        self.engine = PolicyEngine(document, cel, _unbound_stage)

    def start(self, agent: str, principal: Principal, text: str) -> RunState:
        from llmform.lock import build_lock

        state = RunState(
            run_id=str(uuid.uuid4()),
            closure=build_lock(self.document)["closure_sha256"],
            agent=agent.removeprefix("agent."),
            principal=principal,
        )
        self.logs[state.run_id] = AuditLog(
            state.run_id, record_payloads=self.config.audit.record_payloads, path=self.audit_path
        )
        payload = _request_payload(self.config.agents[state.agent], text)
        try:
            request = self.engine.dispatch(
                state.agent,
                Hook.REQUEST,
                payload,
                {
                    "principal": principal.model_dump(),
                    "input": payload,
                    "agent": {"name": state.agent},
                    "run": {"id": state.run_id, "iteration": 0, "cost_usd": 0.0},
                },
                stage=self._stager(state.run_id, Origin.REQUEST),
            )
        except TransformError as exc:
            failed = {"status": "failed", "failure": f"transform failed: {exc}"}
            return self._finish(state.model_copy(update=failed))
        self._record_dispatch(state, Hook.REQUEST, request, payload)
        if failure := _unsupported_block(request, "request"):
            return self._finish(state.model_copy(update={"status": "denied", "failure": failure}))
        admitted = request.payload
        content = admitted["message"] if set(admitted) == {"message"} else json.dumps(admitted)
        return state.model_copy(update={"messages": [Message(role="user", content=content)]})

    def _stager(self, run_id: str, origin: Origin) -> TransformStager:
        """Bind declared transforms to this run's vault and the payload's origin."""

        def stage(declared: Transform) -> Callable[[Any], Any]:
            def commit(payload: Any) -> Any:
                return apply_transform(
                    payload, declared.fields, declared.kind, self.vault, run_id, origin
                )

            return commit

        return stage

    def _message_stager(self, run_id: str) -> TransformStager:
        """Apply model_call transforms to each message bound for the provider."""

        def stage(declared: Transform) -> Callable[[Any], Any]:
            def commit(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
                return [
                    apply_transform(
                        message,
                        declared.fields,
                        declared.kind,
                        self.vault,
                        run_id,
                        Origin.REQUEST if message.get("role") == "user" else Origin.TOOL_RESULT,
                    )
                    for message in messages
                ]

            return commit

        return stage

    def _read_json(self, reference: str) -> Any:
        return json.loads((self.document.root / reference).read_text(encoding="utf-8"))

    def _finish(self, state: RunState) -> RunState:
        # The v0.1 vault is in memory; a terminal run can never resolve its tokens again.
        if state.status in TERMINAL:
            self.vault.purge(state.run_id)
        return state

    def _audit(self, state: RunState) -> AuditLog:
        return self.logs.setdefault(
            state.run_id,
            AuditLog(
                state.run_id,
                record_payloads=self.config.audit.record_payloads,
                path=self.audit_path,
            ),
        )

    def _record_dispatch(
        self, state: RunState, hook: Hook, result: DispatchResult, payload: Any
    ) -> None:
        for policy_name in result.policies:
            policy = self.config.policies[policy_name]
            self._audit(state).append(
                "decision",
                hook=hook.value,
                policy=policy_name,
                verdict=result.verdict.value,
                agent=state.agent,
                principal=state.principal.model_dump(mode="json"),
                data_classes=state.classes,
                rule=policy.rule,
                payload=payload,
            )
            if result.verdict != RecordedOutcome.TRANSFORM:
                continue
            for transform in policy.transform:
                self._audit(state).append(
                    "transform",
                    hook=hook.value,
                    policy=policy_name,
                    agent=state.agent,
                    fields=transform.fields,
                )

    def step(self, state: RunState, *, approved: bool | None = None) -> RunState:
        try:
            result = self._step(state, approved=approved)
        except TransformError as exc:
            failed = {"status": "failed", "failure": f"transform failed: {exc}"}
            result = state.model_copy(update=failed)
        return self._finish(result)

    def _step(self, state: RunState, *, approved: bool | None = None) -> RunState:  # noqa: C901
        if state.status not in {"running", "suspended"}:
            return state
        from llmform.lock import build_lock

        if build_lock(self.document)["closure_sha256"] != state.closure:
            return state.model_copy(
                update={
                    "status": "failed",
                    "failure": "configuration closure changed; cannot resume",
                }
            )
        approved_policy: str | None = None
        if state.pending:
            pending = state.pending
            allowed = approved is True and cel(
                pending.approvers,
                {"principal": state.principal.model_dump(), "run": state.model_dump(mode="json")},
            )
            log = self._audit(state)
            log.append(
                "approval",
                policy=pending.policy,
                hook=pending.hook.value,
                verdict="ALLOW" if allowed else "DENY",
                principal=state.principal.model_dump(mode="json"),
                rule=pending.approvers,
                answers_seq=state.audit_seq,
            )
            if not allowed:
                return state.model_copy(
                    update={
                        "pending": None,
                        "status": "denied",
                        "failure": f"approval denied; requires {pending.approvers}",
                    }
                )
            state = state.model_copy(update={"pending": None, "status": "running"})
            approved_policy = pending.policy
        agent = self.config.agents[state.agent]
        if state.iteration >= agent.max_iterations:
            return state.model_copy(
                update={"status": "failed", "failure": "max_iterations exceeded"}
            )
        if agent.max_cost_usd is not None and state.cost_usd >= agent.max_cost_usd:
            return state.model_copy(update={"status": "failed", "failure": "max_cost_usd exceeded"})
        loop_result = self.engine.dispatch(
            state.agent,
            Hook.LOOP,
            {},
            {
                "iteration": state.iteration,
                "cost_usd": state.cost_usd,
                "principal": state.principal.model_dump(),
            },
            stage=self._stager(state.run_id, Origin.TOOL_RESULT),
        )
        self._record_dispatch(state, Hook.LOOP, loop_result, {})
        if loop_result.verdict == Verdict.DENY:
            return state.model_copy(update={"status": "denied", "failure": "loop policy denied"})
        model_name = agent.model.removeprefix("model.")
        model = self.config.models[model_name]
        if agent.max_cost_usd is not None and model.price and model.max_tokens:
            reserved_cost = model.max_tokens * model.price.output_per_mtok / 1_000_000
            if state.cost_usd + reserved_cost > agent.max_cost_usd:
                return state.model_copy(
                    update={"status": "failed", "failure": "max_cost_usd would be exceeded"}
                )
        outbound = [message.model_dump(mode="json") for message in state.messages]
        call = self.engine.dispatch_model_call(
            state.agent,
            state,
            outbound,
            {"id": model.id, "provider": model.provider},
            stage=self._message_stager(state.run_id),
            approved=approved_policy,
        )
        self._record_dispatch(state, Hook.MODEL_CALL, call, outbound)
        if call.verdict == Verdict.REQUIRE_APPROVAL:
            policy = call.policies[-1]
            definition = self.config.policies[policy]
            return state.model_copy(
                update={
                    "pending": PendingApproval(
                        hook=Hook.MODEL_CALL,
                        policy=policy,
                        rule=definition.rule,
                        approvers=definition.approval.approvers,
                    ),
                    "status": "suspended",
                    "audit_seq": 1,
                }
            )
        if call.verdict == Verdict.DENY:
            return state.model_copy(
                update={
                    "status": "denied",
                    "failure": f"policy {call.policies[-1]} denied model call",
                }
            )
        definitions = [
            ToolDefinition(
                name=name.removeprefix("tool."),
                description=self.config.tools[name.removeprefix("tool.")].description,
                input_schema=self._read_json(self.config.tools[name.removeprefix("tool.")].input),
            )
            for name in agent.tools
        ]
        # Instructions come from the locked prompt file, outside the transformed transcript.
        instructions = (self.document.root / agent.instructions).read_text(encoding="utf-8")
        provider_messages = [
            Message(role="system", content=instructions),
            *(Message.model_validate(message) for message in call.payload),
        ]
        completion = self.provider.complete(provider_messages, model=model.id, tools=definitions)
        price = model.price
        cost = (
            0.0
            if price is None
            else (
                completion.usage.input_tokens * price.input_per_mtok
                + completion.usage.output_tokens * price.output_per_mtok
            )
            / 1_000_000
        )
        state = state.model_copy(
            update={
                "messages": [*state.messages, completion.message],
                "iteration": state.iteration + 1,
                "cost_usd": state.cost_usd + cost,
            }
        )
        if not completion.message.tool_calls:
            result: Any = completion.message.content or ""
            if agent.output:
                try:
                    result = json.loads(result)
                    validate(result, json.loads((self.document.root / agent.output).read_text()))
                except Exception as exc:
                    return state.model_copy(
                        update={"status": "failed", "failure": f"output contract failed: {exc}"}
                    )
            draft = result if isinstance(result, dict) else {"text": result}
            response = self.engine.dispatch(
                state.agent,
                Hook.RESPONSE,
                draft,
                {"principal": state.principal.model_dump(), "draft": draft},
                stage=self._stager(state.run_id, Origin.TOOL_RESULT),
            )
            self._record_dispatch(state, Hook.RESPONSE, response, draft)
            if failure := _unsupported_block(response, "response"):
                return state.model_copy(update={"status": "denied", "failure": failure})
            released = detokenize(
                response.payload, list(response.payload), self.vault, state.run_id, caller=True
            )
            result = released if isinstance(result, dict) else released["text"]
            return state.model_copy(update={"status": "completed", "result": result})
        results: list[ToolResult] = []
        for call in completion.message.tool_calls:
            tool = self.config.tools[call.name]
            context = {
                "tool": call.name,
                "source": tool.source.removeprefix("source."),
                "operation": tool.operation,
                "principal": state.principal.model_dump(),
            }
            for attempt in range(tool.retries + 1):
                admitted = self.engine.dispatch(
                    state.agent,
                    Hook.TOOL_CALL,
                    call.arguments,
                    {**context, "args": call.arguments, "attempt": attempt},
                    stage=self._stager(state.run_id, Origin.TOOL_RESULT),
                )
                self._record_dispatch(state, Hook.TOOL_CALL, admitted, call.arguments)
                if admitted.verdict == Verdict.DENY:
                    return state.model_copy(
                        update={
                            "status": "denied",
                            "failure": f"policy {admitted.policies[-1]} denied tool {call.name}",
                        }
                    )
                if failure := _unsupported_block(admitted, "tool_call"):
                    return state.model_copy(update={"status": "denied", "failure": failure})
                # Only the tool's declared detokenize fields see plaintext (SPEC 3.5.2).
                arguments = detokenize(admitted.payload, tool.detokenize, self.vault, state.run_id)
                try:
                    value = self.sources[tool.source.removeprefix("source.")].execute(
                        tool.operation, arguments, timeout=30
                    )
                except Exception as exc:
                    if attempt == tool.retries:
                        value, error = {"error": str(exc)}, True
                        break
                else:
                    error = False
                    break
            if not error:
                checked = self.engine.dispatch(
                    state.agent,
                    Hook.TOOL_RESULT,
                    value,
                    {**context, "result": value},
                    stage=self._stager(state.run_id, Origin.TOOL_RESULT),
                )
                self._record_dispatch(state, Hook.TOOL_RESULT, checked, value)
                if failure := _unsupported_block(checked, "tool_result"):
                    return state.model_copy(update={"status": "denied", "failure": failure})
                value = checked.payload
            results.append(
                ToolResult(tool_call_id=call.id, name=call.name, content=value, is_error=error)
            )
            state = self.engine.admit_tool_result(state, call.name)
        return state.model_copy(
            update={"messages": [*state.messages, Message(role="tool", tool_results=results)]}
        )


def _unbound_stage(declared: Transform) -> Callable[[Any], Any]:
    raise TransformError(f"{declared.kind} dispatched without a run-scoped vault")


def _request_payload(agent: Agent, text: str) -> dict[str, Any]:
    """Shape caller input for the request hook: a declared object, or the default message."""

    if agent.input:
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            value = None
        if isinstance(value, dict):
            return value
    return {"message": text}


def _unsupported_block(result: DispatchResult, hook: str) -> str | None:
    """Return the failure for a verdict that stops this hook, or None to proceed.

    REQUIRE_APPROVAL discards the staged transforms, so proceeding would release the
    untransformed payload; v0.1 suspends only at model_call, so other hooks fail closed.
    """

    if result.verdict == Verdict.DENY:
        return f"policy {result.policies[-1]} denied {hook}"
    if result.verdict == Verdict.REQUIRE_APPROVAL:
        return (
            f"policy {result.policies[-1]} requires approval at {hook}, "
            "which v0.1 supports only at model_call"
        )
    return None
