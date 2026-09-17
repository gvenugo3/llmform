from llmform.providers import ToolDefinition
from llmform.providers.http import OllamaProvider, OpenAIProvider
from llmform.types import Message, ToolResult


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
