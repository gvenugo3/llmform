"""Small non-streaming OpenAI Responses, Anthropic Messages, and Ollama provider adapters."""

from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from llmform.providers import (
    Completion,
    Provider,
    ProviderError,
    ProviderErrorKind,
    ToolDefinition,
    Usage,
    assistant,
)
from llmform.types import Message


def _post(url: str, body: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
    try:
        with urlopen(
            Request(url, data=json.dumps(body).encode(), headers=headers, method="POST"), timeout=60
        ) as response:  # noqa: S310
            return json.load(response)
    except HTTPError as exc:
        kinds = {
            401: ProviderErrorKind.AUTH,
            403: ProviderErrorKind.AUTH,
            429: ProviderErrorKind.RATE_LIMIT,
            400: ProviderErrorKind.CONTEXT_LENGTH,
        }
        kind = kinds.get(exc.code, ProviderErrorKind.TRANSIENT)
        message = f"provider HTTP {exc.code}"
        # Auth errors can echo a partial credential, so only other errors carry the reason.
        if kind != ProviderErrorKind.AUTH and (reason := _error_reason(exc)):
            message += f": {reason}"
        raise ProviderError(kind, message) from exc
    except URLError as exc:
        raise ProviderError(ProviderErrorKind.TRANSIENT, str(exc)) from exc


def _error_reason(exc: HTTPError) -> str | None:
    """Return the provider's short error message from a JSON error body, if any."""

    try:
        error = json.loads(exc.read()).get("error")
    except (OSError, ValueError, AttributeError):
        return None
    message = error.get("message") if isinstance(error, dict) else error
    return str(message)[:300] if message else None


def _messages(messages: list[Message]) -> list[dict[str, Any]]:
    return [message.model_dump(exclude_none=True) for message in messages]


def _responses_input(messages: list[Message]) -> list[dict[str, Any]]:
    """Convert provider-neutral messages into OpenAI Responses API input items.

    Text turns become role messages; tool calls and results become the separate
    ``function_call`` and ``function_call_output`` items the API correlates by call ID.
    """

    items: list[dict[str, Any]] = []
    for message in messages:
        for result in message.tool_results:
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": result.tool_call_id,
                    "output": json.dumps(result.content),
                }
            )
        if message.content:
            items.append({"role": message.role, "content": message.content})
        for call in message.tool_calls:
            items.append(
                {
                    "type": "function_call",
                    "call_id": call.id,
                    "name": call.name,
                    "arguments": json.dumps(call.arguments),
                }
            )
    return items


class OpenAIProvider(Provider):
    def __init__(self, api_key: str, endpoint: str = "https://api.openai.com/v1/responses"):
        self.api_key, self.endpoint = api_key, endpoint

    def complete(
        self,
        messages: list[Message],
        *,
        model: str,
        tools: list[ToolDefinition],
        output_schema: dict[str, Any] | None = None,
    ) -> Completion:
        body: dict[str, Any] = {"model": model, "input": _responses_input(messages)}
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.input_schema,
                    # Strict mode requires every property in `required`, but llmform
                    # tool schemas may declare optional fields.
                    "strict": False,
                }
                for t in tools
            ]
        if output_schema:
            body["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": "response",
                    "schema": output_schema,
                    "strict": True,
                }
            }
        value = _post(
            self.endpoint,
            body,
            {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
        )
        calls = [
            {
                "id": item["call_id"],
                "name": item["name"],
                "arguments": json.loads(item["arguments"]),
            }
            for item in value.get("output", [])
            if item.get("type") == "function_call"
        ]
        content = next(
            (
                part.get("text")
                for item in value.get("output", [])
                for part in item.get("content", [])
                if part.get("type") == "output_text"
            ),
            None,
        )
        usage = value.get("usage", {})
        return assistant(
            content,
            calls,
            Usage(
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
            ),
        )


def _anthropic_input(messages: list[Message]) -> tuple[str | None, list[dict[str, Any]]]:
    """Convert provider-neutral messages into Anthropic Messages API format.

    Anthropic requires the system prompt to be passed as a top-level ``system``
    parameter rather than as a message role.  Tool calls from the assistant
    become ``tool_use`` content blocks; tool results are ``tool_result`` blocks
    carried by a ``user`` turn.
    """

    system: str | None = None
    turns: list[dict[str, Any]] = []

    for message in messages:
        if message.role == "system":
            system = message.content
            continue

        if message.role == "tool":
            # Tool results always go into a user turn.
            content: list[dict[str, Any]] = [
                {
                    "type": "tool_result",
                    "tool_use_id": result.tool_call_id,
                    "content": json.dumps(result.content),
                    **({"is_error": True} if result.is_error else {}),
                }
                for result in message.tool_results
            ]
            turns.append({"role": "user", "content": content})
            continue

        if message.role == "assistant":
            blocks: list[dict[str, Any]] = []
            if message.content:
                blocks.append({"type": "text", "text": message.content})
            for call in message.tool_calls:
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call.id,
                        "name": call.name,
                        "input": call.arguments,
                    }
                )
            turns.append({"role": "assistant", "content": blocks})
            continue

        # user role
        turns.append({"role": "user", "content": message.content or ""})

    return system, turns


class AnthropicProvider(Provider):
    """Adapter for the Anthropic Messages API (https://api.anthropic.com/v1/messages)."""

    _ANTHROPIC_VERSION = "2023-06-01"

    def __init__(
        self,
        api_key: str,
        endpoint: str = "https://api.anthropic.com/v1/messages",
    ):
        self.api_key, self.endpoint = api_key, endpoint

    def complete(
        self,
        messages: list[Message],
        *,
        model: str,
        tools: list[ToolDefinition],
        output_schema: dict[str, Any] | None = None,
    ) -> Completion:
        system, turns = _anthropic_input(messages)

        # Anthropic requires at least one user turn.
        if not turns:
            turns = [{"role": "user", "content": ""}]

        body: dict[str, Any] = {
            "model": model,
            "max_tokens": 4096,
            "messages": turns,
        }
        if system:
            body["system"] = system
        if tools:
            body["tools"] = [
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.input_schema,
                }
                for t in tools
            ]
        headers: dict[str, str] = {
            "x-api-key": self.api_key,
            "anthropic-version": self._ANTHROPIC_VERSION,
            "Content-Type": "application/json",
        }
        if output_schema:
            # Anthropic structured output is in GA; pass the schema via tool_choice.
            body["tools"] = (body.get("tools") or []) + [
                {
                    "name": "structured_output",
                    "description": "Emit the structured response.",
                    "input_schema": output_schema,
                }
            ]
            body["tool_choice"] = {"type": "tool", "name": "structured_output"}

        value = _post(self.endpoint, body, headers)

        # Extract text and tool_use blocks from the response content list.
        calls: list[dict[str, Any]] = []
        text_content: str | None = None
        for block in value.get("content", []):
            if block.get("type") == "tool_use":
                if output_schema and block.get("name") == "structured_output":
                    # Structured output is returned as the tool input JSON.
                    text_content = json.dumps(block.get("input", {}))
                else:
                    calls.append(
                        {
                            "id": block["id"],
                            "name": block["name"],
                            "arguments": block.get("input", {}),
                        }
                    )
            elif block.get("type") == "text" and text_content is None:
                text_content = block.get("text")

        usage = value.get("usage", {})
        return assistant(
            text_content,
            calls,
            Usage(
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
            ),
        )


class OllamaProvider(Provider):
    def __init__(self, endpoint: str = "http://localhost:11434/api/chat"):
        self.endpoint = (
            endpoint.rstrip("/")
            if endpoint.endswith("/chat")
            else endpoint.rstrip("/") + "/api/chat"
        )

    def complete(
        self,
        messages: list[Message],
        *,
        model: str,
        tools: list[ToolDefinition],
        output_schema: dict[str, Any] | None = None,
    ) -> Completion:
        body: dict[str, Any] = {"model": model, "messages": _messages(messages), "stream": False}
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.input_schema,
                    },
                }
                for t in tools
            ]
        if output_schema:
            body["format"] = output_schema
        value = _post(self.endpoint, body, {"Content-Type": "application/json"})
        message = value.get("message", {})
        calls = [
            {
                "id": call.get("id", f"call-{index}"),
                "name": call["function"]["name"],
                "arguments": call["function"].get("arguments", {}),
            }
            for index, call in enumerate(message.get("tool_calls", []))
        ]
        return assistant(
            message.get("content"),
            calls,
            Usage(
                input_tokens=value.get("prompt_eval_count", 0),
                output_tokens=value.get("eval_count", 0),
            ),
        )
