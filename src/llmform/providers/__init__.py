"""Provider-neutral model completion interfaces."""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import StrEnum
from typing import Any

from pydantic import Field

from llmform.types import Message, PublicModel, ToolCall


class ProviderErrorKind(StrEnum):
    AUTH = "auth"
    RATE_LIMIT = "rate_limit"
    CONTEXT_LENGTH = "context_length"
    TRANSIENT = "transient"
    UNSUPPORTED = "unsupported"


class ProviderError(RuntimeError):
    def __init__(self, kind: ProviderErrorKind, message: str):
        self.kind = kind
        super().__init__(message)


class Usage(PublicModel):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)


class Completion(PublicModel):
    message: Message
    usage: Usage


class ToolDefinition(PublicModel):
    name: str
    description: str
    input_schema: dict[str, Any]


class Provider(ABC):
    @abstractmethod
    def complete(
        self,
        messages: list[Message],
        *,
        model: str,
        tools: list[ToolDefinition],
        output_schema: dict[str, Any] | None = None,
    ) -> Completion: ...


def assistant(content: str | None, calls: list[dict[str, Any]], usage: Usage) -> Completion:
    return Completion(
        message=Message(
            role="assistant",
            content=content,
            tool_calls=[ToolCall.model_validate(c) for c in calls],
        ),
        usage=usage,
    )
