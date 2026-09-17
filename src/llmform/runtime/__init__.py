"""Serializable agent loop and approval store."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from celpy import Environment
from celpy.celtypes import MapType
from jsonschema import validate

from llmform.audit import AuditLog
from llmform.config.loader import ConfigDocument
from llmform.policy.engine import DispatchResult, PolicyEngine
from llmform.providers import Provider, ToolDefinition
from llmform.sources import Source
from llmform.types import Hook, Message, PendingApproval, Principal, RunState, ToolResult, Verdict


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
        self.engine = PolicyEngine(document, cel, lambda kind, payload: lambda: payload)

    def start(self, agent: str, principal: Principal, text: str) -> RunState:
        from llmform.lock import build_lock

        state = RunState(
            run_id=str(uuid.uuid4()),
            closure=build_lock(self.document)["closure_sha256"],
            agent=agent.removeprefix("agent."),
            principal=principal,
            messages=[Message(role="user", content=text)],
        )
        self.logs[state.run_id] = AuditLog(
            state.run_id, record_payloads=self.config.audit.record_payloads, path=self.audit_path
        )
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
            for transform in policy.transform:
                self._audit(state).append(
                    "transform",
                    hook=hook.value,
                    policy=policy_name,
                    agent=state.agent,
                    fields=transform.fields,
                )

    def step(self, state: RunState, *, approved: bool | None = None) -> RunState:  # noqa: C901
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
        call = self.engine.dispatch_model_call(
            state.agent, state, state.messages, {"id": model.id, "provider": model.provider}
        )
        self._record_dispatch(state, Hook.MODEL_CALL, call, state.messages)
        if call.verdict == Verdict.REQUIRE_APPROVAL and call.policies[-1] == approved_policy:
            call = call.__class__(Verdict.ALLOW, call.payload, call.policies)
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
                input_schema={"type": "object"},
            )
            for name in agent.tools
        ]
        completion = self.provider.complete(state.messages, model=model.id, tools=definitions)
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
            response = self.engine.dispatch(
                state.agent, Hook.RESPONSE, result, {"principal": state.principal.model_dump()}
            )
            self._record_dispatch(state, Hook.RESPONSE, response, result)
            if response.verdict == Verdict.DENY:
                return state.model_copy(
                    update={
                        "status": "denied",
                        "failure": f"policy {response.policies[-1]} denied response",
                    }
                )
            return state.model_copy(update={"status": "completed", "result": result})
        results: list[ToolResult] = []
        for call in completion.message.tool_calls:
            tool = self.config.tools[call.name]
            for attempt in range(tool.retries + 1):
                tool_result = self.engine.dispatch(
                    state.agent,
                    Hook.TOOL_CALL,
                    call.arguments,
                    {"tool": call.name, "args": call.arguments, "attempt": attempt},
                )
                self._record_dispatch(state, Hook.TOOL_CALL, tool_result, call.arguments)
                if tool_result.verdict == Verdict.DENY:
                    return state.model_copy(
                        update={
                            "status": "denied",
                            "failure": f"policy {tool_result.policies[-1]} denied tool {call.name}",
                        }
                    )
                try:
                    value = self.sources[tool.source.removeprefix("source.")].execute(
                        tool.operation, call.arguments, timeout=30
                    )
                except Exception as exc:
                    if attempt == tool.retries:
                        value, error = {"error": str(exc)}, True
                        break
                else:
                    error = False
                    break
            results.append(
                ToolResult(tool_call_id=call.id, name=call.name, content=value, is_error=error)
            )
            state = self.engine.admit_tool_result(state, call.name)
        return state.model_copy(
            update={"messages": [*state.messages, Message(role="tool", tool_results=results)]}
        )
