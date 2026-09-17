"""Small non-streaming OpenAI Responses and Ollama provider adapters."""

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
        raise ProviderError(
            kinds.get(exc.code, ProviderErrorKind.TRANSIENT), f"provider HTTP {exc.code}"
        ) from exc
    except URLError as exc:
        raise ProviderError(ProviderErrorKind.TRANSIENT, str(exc)) from exc


def _messages(messages: list[Message]) -> list[dict[str, Any]]:
    return [message.model_dump(exclude_none=True) for message in messages]


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
        body: dict[str, Any] = {"model": model, "input": _messages(messages)}
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.input_schema,
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
