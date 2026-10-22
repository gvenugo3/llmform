from email.message import Message as Message_headers
from io import BytesIO
from urllib.error import HTTPError

import pytest

from llmform.providers import ProviderError, ProviderErrorKind, ToolDefinition
from llmform.providers.http import (
    AnthropicProvider,
    OllamaProvider,
    OpenAIProvider,
    _anthropic_input,
)
from llmform.types import Message, ToolCall, ToolResult

# ---------------------------------------------------------------------------
# Anthropic tests
# ---------------------------------------------------------------------------

_ANTHROPIC_TEXT_REPLY = {
    "content": [{"type": "text", "text": "Hello from Claude!"}],
    "usage": {"input_tokens": 5, "output_tokens": 3},
}


def test_anthropic_text_response(monkeypatch) -> None:
    monkeypatch.setattr("llmform.providers.http._post", lambda *_: _ANTHROPIC_TEXT_REPLY)
    result = AnthropicProvider("key").complete(
        [Message(role="user", content="Hi")],
        model="claude-opus-4-5",
        tools=[],
    )
    assert result.message.content == "Hello from Claude!"
    assert result.message.tool_calls == []
    assert result.usage.input_tokens == 5
    assert result.usage.output_tokens == 3


def test_anthropic_tool_use(monkeypatch) -> None:
    recorded = {
        "content": [
            {
                "type": "tool_use",
                "id": "toolu_01",
                "name": "lookup",
                "input": {"id": 42},
            }
        ],
        "usage": {"input_tokens": 8, "output_tokens": 6},
    }
    requests = []
    monkeypatch.setattr(
        "llmform.providers.http._post",
        lambda url, body, headers: requests.append(body) or recorded,
    )
    result = AnthropicProvider("key").complete(
        [Message(role="user", content="Look it up")],
        model="claude-opus-4-5",
        tools=[
            ToolDefinition(
                name="lookup",
                description="Lookup",
                input_schema={"type": "object"},
            )
        ],
    )
    assert result.message.tool_calls[0].id == "toolu_01"
    assert result.message.tool_calls[0].name == "lookup"
    assert result.message.tool_calls[0].arguments == {"id": 42}
    assert result.message.content is None
    # Verify tool was sent in correct Anthropic format
    assert requests[0]["tools"][0]["input_schema"] == {"type": "object"}


def test_anthropic_system_message_hoisted(monkeypatch) -> None:
    requests = []
    monkeypatch.setattr(
        "llmform.providers.http._post",
        lambda url, body, headers: requests.append(body) or _ANTHROPIC_TEXT_REPLY,
    )
    AnthropicProvider("key").complete(
        [
            Message(role="system", content="You are concise."),
            Message(role="user", content="Hello"),
        ],
        model="claude-opus-4-5",
        tools=[],
    )
    assert requests[0]["system"] == "You are concise."
    # System message must NOT appear in the messages array
    for turn in requests[0]["messages"]:
        assert turn.get("role") != "system"


def test_anthropic_tool_result_turn(monkeypatch) -> None:
    requests = []
    monkeypatch.setattr(
        "llmform.providers.http._post",
        lambda url, body, headers: requests.append(body) or _ANTHROPIC_TEXT_REPLY,
    )
    AnthropicProvider("key").complete(
        [
            Message(role="user", content="Find item 7."),
            Message(
                role="assistant",
                tool_calls=[ToolCall(id="toolu_02", name="lookup", arguments={"id": 7})],
            ),
            Message(
                role="tool",
                tool_results=[
                    ToolResult(tool_call_id="toolu_02", name="lookup", content={"found": True})
                ],
            ),
        ],
        model="claude-opus-4-5",
        tools=[],
    )
    turns = requests[0]["messages"]
    # Assistant turn: tool_use block
    assistant_turn = next(t for t in turns if t["role"] == "assistant")
    assert any(b["type"] == "tool_use" for b in assistant_turn["content"])
    # Tool result: user turn with tool_result block
    user_turns = [t for t in turns if t["role"] == "user"]
    tool_result_turn = next(t for t in user_turns if isinstance(t["content"], list))
    assert tool_result_turn["content"][0]["type"] == "tool_result"
    assert tool_result_turn["content"][0]["tool_use_id"] == "toolu_02"


def test_anthropic_tool_result_is_error(monkeypatch) -> None:
    requests = []
    monkeypatch.setattr(
        "llmform.providers.http._post",
        lambda url, body, headers: requests.append(body) or _ANTHROPIC_TEXT_REPLY,
    )
    AnthropicProvider("key").complete(
        [
            Message(
                role="tool",
                tool_results=[
                    ToolResult(
                        tool_call_id="toolu_03",
                        name="lookup",
                        content="not found",
                        is_error=True,
                    )
                ],
            )
        ],
        model="claude-opus-4-5",
        tools=[],
    )
    block = requests[0]["messages"][0]["content"][0]
    assert block["is_error"] is True


def test_anthropic_structured_output(monkeypatch) -> None:
    structured_reply = {
        "content": [
            {
                "type": "tool_use",
                "id": "toolu_04",
                "name": "structured_output",
                "input": {"answer": 42},
            }
        ],
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    requests = []
    monkeypatch.setattr(
        "llmform.providers.http._post",
        lambda url, body, headers: requests.append(body) or structured_reply,
    )
    result = AnthropicProvider("key").complete(
        [Message(role="user", content="What is the answer?")],
        model="claude-opus-4-5",
        tools=[],
        output_schema={"type": "object", "properties": {"answer": {"type": "integer"}}},
    )
    # Structured output is returned as JSON text
    assert '"answer"' in (result.message.content or "")
    assert requests[0]["tool_choice"] == {"type": "tool", "name": "structured_output"}


def test_anthropic_empty_messages_gets_placeholder_turn(monkeypatch) -> None:
    """Anthropic requires at least one user turn; empty history must not raise."""
    requests = []
    monkeypatch.setattr(
        "llmform.providers.http._post",
        lambda url, body, headers: requests.append(body) or _ANTHROPIC_TEXT_REPLY,
    )
    AnthropicProvider("key").complete([], model="claude-opus-4-5", tools=[])
    assert requests[0]["messages"] == [{"role": "user", "content": ""}]


def test_anthropic_input_helper_mixed() -> None:
    """_anthropic_input correctly partitions system, user, assistant, and tool turns."""
    messages = [
        Message(role="system", content="Be helpful."),
        Message(role="user", content="Tell me."),
        Message(
            role="assistant",
            content="Let me check.",
            tool_calls=[ToolCall(id="c1", name="fn", arguments={"x": 1})],
        ),
        Message(
            role="tool",
            tool_results=[ToolResult(tool_call_id="c1", name="fn", content={"y": 2})],
        ),
    ]
    system, turns = _anthropic_input(messages)
    assert system == "Be helpful."
    assert turns[0] == {"role": "user", "content": "Tell me."}
    assistant_blocks = turns[1]["content"]
    assert any(b["type"] == "text" for b in assistant_blocks)
    assert any(b["type"] == "tool_use" for b in assistant_blocks)
    assert turns[2]["role"] == "user"
    assert turns[2]["content"][0]["type"] == "tool_result"


# ---------------------------------------------------------------------------
# OpenAI tests (unchanged)
# ---------------------------------------------------------------------------


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
