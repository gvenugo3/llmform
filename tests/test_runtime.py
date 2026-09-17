from typer.testing import CliRunner

from llmform.cli import app
from llmform.config.loader import load_project
from llmform.config.models import Policy, Price, Tool
from llmform.providers import Completion, Provider, ToolDefinition, Usage
from llmform.runtime import Loop
from llmform.types import Message, Principal, ToolCall


class FakeProvider(Provider):
    def complete(self, messages, *, model: str, tools: list[ToolDefinition], output_schema=None):
        return Completion(
            message=Message(role="assistant", content="runtime result"),
            usage=Usage(input_tokens=10, output_tokens=2),
        )


class RefundProvider(Provider):
    def complete(self, messages, *, model: str, tools: list[ToolDefinition], output_schema=None):
        return Completion(
            message=Message(
                role="assistant",
                tool_calls=[ToolCall(id="call-1", name="refund", arguments={"amount": 125})],
            ),
            usage=Usage(input_tokens=1, output_tokens=1),
        )


def project(tmp_path, *, approval: bool = False):
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "assistant.md").write_text("instructions")
    policy = (
        """policies:
  approve:
    on: model_call
    rule: "false"
    otherwise: REQUIRE_APPROVAL
    approval:
      approvers: principal.role == "supervisor"
"""
        if approval
        else ""
    )
    attachments = "    policies: [policy.approve]\n" if approval else ""
    base = """version: "0.1"
providers:
  local: {type: ollama}
models:
  default: {provider: provider.local, id: test}
agents:
  support:
    model: model.default
    instructions: prompts/assistant.md
"""
    (tmp_path / "llmform.yaml").write_text(
        """version: "0.1"
"""
        + policy
        + base.removeprefix('version: "0.1"\n')
        + attachments
    )
    document = load_project(tmp_path)
    assert document.config is not None
    return document


def test_loop_step_is_serializable_and_completes(tmp_path) -> None:
    document = project(tmp_path)
    loop = Loop(document, FakeProvider(), {})
    state = loop.start("support", Principal(role="developer"), "help")
    restored = state.model_validate_json(state.model_dump_json())
    result = Loop(document, FakeProvider(), {}).step(restored)
    assert result.status == "completed"
    assert result.result == "runtime result"


def test_loop_refuses_mutated_closure(tmp_path) -> None:
    loop = Loop(project(tmp_path), FakeProvider(), {})
    state = loop.start("support", Principal(role="developer"), "help")
    (tmp_path / "prompts" / "assistant.md").write_text("changed instructions")
    assert loop.step(state).failure == "configuration closure changed; cannot resume"


def test_approval_refuses_an_unauthorized_principal(tmp_path, capsys) -> None:
    loop = Loop(project(tmp_path, approval=True), FakeProvider(), {})
    suspended = loop.step(loop.start("support", Principal(role="developer"), "help"))
    denied = loop.step(suspended, approved=True)
    assert suspended.status == "suspended"
    assert denied.status == "denied"
    assert 'principal.role == "supervisor"' in denied.failure
    assert '"kind":"approval"' in capsys.readouterr().err


def test_serialized_suspension_resumes_in_a_fresh_loop(tmp_path) -> None:
    document = project(tmp_path, approval=True)
    first = Loop(document, FakeProvider(), {})
    suspended = first.step(first.start("support", Principal(role="supervisor"), "help"))
    restored = suspended.model_validate_json(suspended.model_dump_json())
    resumed = Loop(document, FakeProvider(), {}).step(restored, approved=True)
    assert resumed.status == "completed"
    assert resumed.result == "runtime result"


def test_cost_ceiling_blocks_the_next_model_call(tmp_path) -> None:
    document = project(tmp_path)
    assert document.config is not None
    document.config.models["default"] = document.config.models["default"].model_copy(
        update={"max_tokens": 10, "price": Price(input_per_mtok=0, output_per_mtok=1)}
    )
    document.config.agents["support"] = document.config.agents["support"].model_copy(
        update={"max_cost_usd": 0.000001}
    )
    loop = Loop(document, FakeProvider(), {})
    assert loop.step(loop.start("support", Principal(role="developer"), "help")).failure == (
        "max_cost_usd would be exceeded"
    )


def test_refund_limit_is_denied_before_source_execution(tmp_path) -> None:
    document = project(tmp_path)
    assert document.config is not None
    (tmp_path / "input.json").write_text('{"type":"object"}')
    document.config.tools["refund"] = Tool(
        source="source.payments", operation="refund", description="Refund", input="input.json"
    )
    document.config.policies["limit"] = Policy(
        on="tool_call", rule="args.amount <= 100", otherwise="DENY"
    )
    document.config.agents["support"] = document.config.agents["support"].model_copy(
        update={"tools": ["tool.refund"], "policies": ["policy.limit"]}
    )
    denied = Loop(document, RefundProvider(), {}).step(
        Loop(document, RefundProvider(), {}).start("support", Principal(role="developer"), "help")
    )
    assert denied.status == "denied"
    assert denied.failure == "policy limit denied tool refund"


def test_run_command_reports_runtime_refund_denial(tmp_path, monkeypatch) -> None:
    document = project(tmp_path)
    assert document.config is not None
    (tmp_path / "input.json").write_text('{"type":"object"}')
    config = document.config
    config.tools["refund"] = Tool(
        source="source.payments", operation="refund", description="Refund", input="input.json"
    )
    config.policies["limit"] = Policy(on="tool_call", rule="args.amount <= 100", otherwise="DENY")
    config.agents["support"] = config.agents["support"].model_copy(
        update={"tools": ["tool.refund"], "policies": ["policy.limit"]}
    )
    # Serialize the test fixture as config because the CLI owns project loading.
    (tmp_path / "llmform.yaml").write_text(
        """version: "0.1"
providers:
  local: {type: ollama}
models:
  default: {provider: provider.local, id: test}
tools:
  refund: {source: source.payments, operation: refund, description: Refund, input: input.json}
policies:
  limit: {on: tool_call, rule: "args.amount <= 100", otherwise: DENY}
agents:
  support:
    model: model.default
    instructions: prompts/assistant.md
    tools: [tool.refund]
    policies: [policy.limit]
"""
    )
    monkeypatch.setattr("llmform.cli.OllamaProvider", lambda *_: RefundProvider())
    result = CliRunner().invoke(app, ["run", "support", "--path", str(tmp_path), "--input", "help"])
    assert result.exit_code == 1
    assert "policy limit denied tool refund" in result.output
