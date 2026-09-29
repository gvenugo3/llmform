from email.message import Message as Message_headers
from io import BytesIO
from urllib.error import HTTPError

import pytest

from llmform.providers import ProviderError, ProviderErrorKind, ToolDefinition
from llmform.providers.http import OllamaProvider, OpenAIProvider
from llmform.types import Message, ToolCall, ToolResult


def test_openai_recorded_tool_and_structured_output(monkeypatch) -> None:
    recorded = {
        "output": [
            {
                "type": "function_call",
                "call_id": "call-1",
                "name": "lookup",
                "arguments": '{"id": 7}',
            },
            {"type": "message", "content": [{"type": "output_text", "text": '{"ok":true}'}]},
        ],
        "usage": {"input_tokens": 10, "output_tokens": 2},
    }
    requests = []

    def post(*args):
        requests.append(args[1])
        return recorded

    monkeypatch.setattr("llmform.providers.http._post", post)
    result = OpenAIProvider("key").complete(
        [Message(role="user", content="hello")],
        model="gpt-test",
        tools=[
            ToolDefinition(name="lookup", description="Lookup", input_schema={"type": "object"})
        ],
        output_schema={"type": "object"},
    )
    assert result.message.tool_calls[0].arguments == {"id": 7}
    assert result.message.content == '{"ok":true}'
    assert result.usage.input_tokens == 10
    OpenAIProvider("key").complete(
        [
            Message(
                role="tool",
                tool_results=[ToolResult(tool_call_id="call-1", name="lookup", content={})],
            )
        ],
        model="gpt-test",
        tools=[],
    )
    assert requests[0]["tools"][0]["name"] == "lookup"


def test_ollama_recorded_tool_response(monkeypatch) -> None:
    monkeypatch.setattr(
        "llmform.providers.http._post",
        lambda *_: {
            "message": {
                "content": "done",
                "tool_calls": [{"function": {"name": "lookup", "arguments": {}}}],
            },
            "prompt_eval_count": 3,
            "eval_count": 4,
        },
    )
    result = OllamaProvider().complete([], model="local", tools=[])
    assert result.message.content == "done"
    assert result.message.tool_calls[0].name == "lookup"
    assert result.usage.output_tokens == 4


_TEXT_REPLY = {"output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}]}


def test_openai_request_uses_responses_input_items(monkeypatch) -> None:
    requests = []
    monkeypatch.setattr(
        "llmform.providers.http._post", lambda *args: requests.append(args[1]) or _TEXT_REPLY
    )
    OpenAIProvider("key").complete(
        [
            Message(role="system", content="Be brief."),
            Message(role="user", content="Look up 7."),
            Message(
                role="assistant", tool_calls=[ToolCall(id="c1", name="lookup", arguments={"id": 7})]
            ),
            Message(
                role="tool",
                tool_results=[ToolResult(tool_call_id="c1", name="lookup", content={"ok": True})],
            ),
        ],
        model="gpt-test",
        tools=[
            ToolDefinition(name="lookup", description="Lookup", input_schema={"type": "object"})
        ],
    )

    assert requests[0]["input"] == [
        {"role": "system", "content": "Be brief."},
        {"role": "user", "content": "Look up 7."},
        {"type": "function_call", "call_id": "c1", "name": "lookup", "arguments": '{"id": 7}'},
        {"type": "function_call_output", "call_id": "c1", "output": '{"ok": true}'},
    ]
    assert requests[0]["tools"][0]["strict"] is False


def _http_error(code: int, body: bytes) -> HTTPError:
    return HTTPError("https://api.test", code, "error", Message_headers(), BytesIO(body))


def test_provider_error_carries_the_reason_but_not_for_auth(monkeypatch) -> None:
    body = b'{"error": {"message": "model gpt-missing does not exist"}}'
    monkeypatch.setattr("llmform.providers.http.urlopen", _raise(_http_error(404, body)))
    with pytest.raises(ProviderError, match="provider HTTP 404: model gpt-missing does not exist"):
        OpenAIProvider("key").complete([], model="gpt-missing", tools=[])

    auth = b'{"error": {"message": "Incorrect API key provided: sk-abc***wxyz"}}'
    monkeypatch.setattr("llmform.providers.http.urlopen", _raise(_http_error(401, auth)))
    with pytest.raises(ProviderError) as caught:
        OpenAIProvider("key").complete([], model="gpt-test", tools=[])
    assert str(caught.value) == "provider HTTP 401"
    assert caught.value.kind == ProviderErrorKind.AUTH


def _raise(error: Exception):
    def urlopen(*_args, **_kwargs):
        raise error

    return urlopen
