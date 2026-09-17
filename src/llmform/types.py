"""Public values shared by llmform callers, providers, and the runtime."""

from __future__ import annotations

from datetime import timedelta
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator


class PublicModel(BaseModel):
    """Base class for stable, strict SDK values."""

    model_config = ConfigDict(extra="forbid")


class Principal(PublicModel):
    """The authenticated identity on whose behalf a run executes."""

    id: str = ""
    tenant: str = ""
    role: str
    scopes: list[str] = Field(default_factory=list)
    attrs: dict[str, str] = Field(default_factory=dict)


class ToolCall(PublicModel):
    """A provider-neutral tool invocation requested by an assistant."""

    id: str
    name: str
    arguments: dict[str, JsonValue]


class ToolResult(PublicModel):
    """The result of one tool invocation."""

    tool_call_id: str
    name: str
    content: JsonValue
    is_error: bool = False


class Message(PublicModel):
    """A provider-neutral conversation message.

    Tool calls and results are explicit rather than embedded in ``content`` so
    providers can convert them without parsing or losing correlation IDs.
    """

    role: Literal["system", "user", "assistant", "tool"]
    content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_results: list[ToolResult] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_role_payload(self) -> Message:
        if self.tool_calls and self.role != "assistant":
            raise ValueError("tool_calls are only valid on assistant messages")
        if self.tool_results and self.role != "tool":
            raise ValueError("tool_results are only valid on tool messages")
        if self.role == "tool" and not self.tool_results:
            raise ValueError("tool messages require at least one tool result")
        if self.content is None and not self.tool_calls and not self.tool_results:
            raise ValueError("a message must contain content, tool calls, or tool results")
        return self


class Hook(StrEnum):
    REQUEST = "request"
    MODEL_CALL = "model_call"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    RESPONSE = "response"
    LOOP = "loop"


class Verdict(StrEnum):
    """A verdict that a policy author may select."""

    ALLOW = "ALLOW"
    DENY = "DENY"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"


class RecordedOutcome(StrEnum):
    """An engine-recorded outcome that is not an authorable verdict."""

    TRANSFORM = "TRANSFORM"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class PendingApproval(PublicModel):
    """The policy decision on which a run is suspended."""

    hook: Hook
    policy: str
    rule: str | None = None
    approvers: str
    payload: JsonValue | None = None


class RunState(PublicModel):
    """Serializable state passed into and returned from each runtime step."""

    run_id: str
    closure: str
    agent: str
    principal: Principal
    messages: list[Message] = Field(default_factory=list)
    classes: list[str] = Field(default_factory=list)
    iteration: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0)
    elapsed: timedelta = Field(default=timedelta(0))
    pending: PendingApproval | None = None
    status: Literal["running", "suspended", "completed", "failed", "denied"] = "running"
    failure: str | None = None
    result: JsonValue | None = None
    audit_seq: int = Field(default=0, ge=0)
