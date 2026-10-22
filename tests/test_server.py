from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from llmform.cli import app
from llmform.config.loader import load_project
from llmform.lock import write_lock
from llmform.providers import Completion, Provider, ToolDefinition, Usage
from llmform.runtime import Loop
from llmform.server import Authenticator, LlmformServer
from llmform.types import Message, Principal


class ScriptedProvider(Provider):
    def __init__(self, reply: str = "Hello from agent"):
        self.reply = reply

    def complete(
        self,
        messages: list[Message],
        *,
        model: str,
        tools: list[ToolDefinition],
        output_schema: dict[str, Any] | None = None,
    ) -> Completion:
        return Completion(
            message=Message(role="assistant", content=self.reply),
            usage=Usage(input_tokens=10, output_tokens=5),
        )


def _setup_test_project(tmp_path: Path, *, require_approval: bool = False) -> Path:
    prompts = tmp_path / "prompts"
    prompts.mkdir(parents=True, exist_ok=True)
    (prompts / "assistant.md").write_text("You are a helpful assistant.", encoding="utf-8")

    policy_block = (
        """policies:
  approve:
    on: model_call
    rule: "false"
    otherwise: REQUIRE_APPROVAL
    approval:
      approvers: principal.role == "supervisor"
"""
        if require_approval
        else ""
    )
    agent_policies = "    policies: [policy.approve]\n" if require_approval else ""

    config = f"""version: "0.1"
providers:
  local:
    type: ollama
    endpoint: http://localhost:11434
models:
  default:
    provider: provider.local
    id: llama3
{policy_block}agents:
  support:
    model: model.default
    instructions: prompts/assistant.md
{agent_policies}"""

    (tmp_path / "llmform.yaml").write_text(config, encoding="utf-8")
    doc = load_project(tmp_path)
    write_lock(doc)
    return tmp_path


def _request(
    url: str,
    method: str = "GET",
    data: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any] | str]:
    hdrs = dict(headers or {})
    encoded: bytes | None = None
    if data is not None:
        encoded = json.dumps(data).encode("utf-8")
        hdrs["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=encoded, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            status = resp.status
            content = resp.read().decode("utf-8")
            try:
                return status, json.loads(content)
            except ValueError:
                return status, content
    except urllib.error.HTTPError as err:
        status = err.code
        content = err.read().decode("utf-8")
        try:
            return status, json.loads(content)
        except ValueError:
            return status, content


def test_healthz_and_unauthenticated_rejection(tmp_path: Path) -> None:
    project_dir = _setup_test_project(tmp_path)
    doc = load_project(project_dir)
    loop = Loop(doc, ScriptedProvider(), {})
    authenticator = Authenticator(
        {"valid-token": Principal(id="agent-1", role="developer")},
        insecure_dev=False,
    )
    server = LlmformServer(
        ("127.0.0.1", 0),
        doc,
        loop,
        storage_dir=tmp_path / ".llmform",
        authenticator=authenticator,
    )
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever)
    thread.daemon = True
    thread.start()

    try:
        # /healthz is unauthenticated
        status, body = _request(f"http://127.0.0.1:{port}/healthz")
        assert status == 200
        assert isinstance(body, dict)
        assert body["status"] == "ok"

        # Missing token returns 401
        status, body = _request(
            f"http://127.0.0.1:{port}/v1/agents/support/runs",
            method="POST",
            data={"input": "Hello"},
        )
        assert status == 401
        assert body == {"error": "Unauthorized"}

        # Invalid token returns 401
        status, body = _request(
            f"http://127.0.0.1:{port}/v1/agents/support/runs",
            method="POST",
            data={"input": "Hello"},
            headers={"Authorization": "Bearer bad-token"},
        )
        assert status == 401
    finally:
        server.shutdown()
        server.server_close()


def test_create_and_inspect_run_with_sessions(tmp_path: Path) -> None:
    project_dir = _setup_test_project(tmp_path)
    doc = load_project(project_dir)
    loop = Loop(doc, ScriptedProvider("Answer 1"), {})
    token_map = {"tok-dev": Principal(id="dev1", role="developer")}
    authenticator = Authenticator(token_map, insecure_dev=False)
    server = LlmformServer(
        ("127.0.0.1", 0),
        doc,
        loop,
        storage_dir=tmp_path / ".llmform",
        authenticator=authenticator,
    )
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever)
    thread.daemon = True
    thread.start()

    try:
        headers = {"Authorization": "Bearer tok-dev"}

        # Start first turn with session
        status, body = _request(
            f"http://127.0.0.1:{port}/v1/agents/support/runs",
            method="POST",
            data={"input": "What is my account?", "session_id": "sess-123"},
            headers=headers,
        )
        assert status == 200
        assert isinstance(body, dict)
        run_id = body["run_id"]
        assert body["status"] == "completed"
        assert body["output"] == "Answer 1"

        # Inspect run
        status, inspect_body = _request(
            f"http://127.0.0.1:{port}/v1/runs/{run_id}",
            headers=headers,
        )
        assert status == 200
        assert isinstance(inspect_body, dict)
        assert inspect_body["run_id"] == run_id
        assert inspect_body["status"] == "completed"

        # Check events
        status, events_body = _request(
            f"http://127.0.0.1:{port}/v1/runs/{run_id}/events",
            headers=headers,
        )
        assert status == 200
        assert isinstance(events_body, dict)
        assert len(events_body["events"]) >= 2

        # Second turn in same session
        loop.provider = ScriptedProvider("Answer 2")
        status, body2 = _request(
            f"http://127.0.0.1:{port}/v1/agents/support/runs",
            method="POST",
            data={"input": "Follow up question", "session_id": "sess-123"},
            headers=headers,
        )
        assert status == 200
        assert isinstance(body2, dict)
        assert body2["output"] == "Answer 2"

        # Verify session has preserved history
        sess = server.session_store.get("sess-123")
        assert sess is not None
        assert len(sess.messages) >= 2
    finally:
        server.shutdown()
        server.server_close()


def test_async_approvals_authorization_and_denial(tmp_path: Path) -> None:
    project_dir = _setup_test_project(tmp_path, require_approval=True)
    doc = load_project(project_dir)
    loop = Loop(doc, ScriptedProvider("Approved execution output"), {})
    token_map = {
        "dev-tok": Principal(id="u-dev", role="developer"),
        "sup-tok": Principal(id="u-sup", role="supervisor"),
    }
    authenticator = Authenticator(token_map, insecure_dev=False)
    server = LlmformServer(
        ("127.0.0.1", 0),
        doc,
        loop,
        storage_dir=tmp_path / ".llmform",
        authenticator=authenticator,
    )
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever)
    thread.daemon = True
    thread.start()

    try:
        # Developer starts run -> suspends on policy.approve
        status, body = _request(
            f"http://127.0.0.1:{port}/v1/agents/support/runs",
            method="POST",
            data={"input": "Execute protected action"},
            headers={"Authorization": "Bearer dev-tok"},
        )
        assert status == 200
        assert isinstance(body, dict)
        run_id = body["run_id"]
        assert body["status"] == "awaiting_approval"
        assert body["pending"]["policy"] == "approve"

        # Developer attempts to self-approve -> rejected with 403
        status, dev_approve = _request(
            f"http://127.0.0.1:{port}/v1/runs/{run_id}/approve",
            method="POST",
            headers={"Authorization": "Bearer dev-tok"},
        )
        assert status == 403
        assert isinstance(dev_approve, dict)
        assert "approval denied" in dev_approve["error"]

        # Run state is now denied
        status, inspect_denied = _request(
            f"http://127.0.0.1:{port}/v1/runs/{run_id}",
            headers={"Authorization": "Bearer dev-tok"},
        )
        assert status == 200
        assert isinstance(inspect_denied, dict)
        assert inspect_denied["status"] == "denied"

        # Start another run to test supervisor approval
        status, body2 = _request(
            f"http://127.0.0.1:{port}/v1/agents/support/runs",
            method="POST",
            data={"input": "Action 2"},
            headers={"Authorization": "Bearer dev-tok"},
        )
        assert status == 200
        assert isinstance(body2, dict)
        run_id2 = body2["run_id"]
        assert body2["status"] == "awaiting_approval"

        # Supervisor approves -> accepted with 200 and completed
        status, sup_approve = _request(
            f"http://127.0.0.1:{port}/v1/runs/{run_id2}/approve",
            method="POST",
            headers={"Authorization": "Bearer sup-tok"},
        )
        assert status == 200
        assert isinstance(sup_approve, dict)
        assert sup_approve["status"] == "completed"
        assert sup_approve["output"] == "Approved execution output"

        # Start a 3rd run to test explicit denial by caller
        status, body3 = _request(
            f"http://127.0.0.1:{port}/v1/agents/support/runs",
            method="POST",
            data={"input": "Action 3"},
            headers={"Authorization": "Bearer dev-tok"},
        )
        run_id3 = body3["run_id"]
        assert body3["status"] == "awaiting_approval"

        status, deny_body = _request(
            f"http://127.0.0.1:{port}/v1/runs/{run_id3}/deny",
            method="POST",
            headers={"Authorization": "Bearer sup-tok"},
        )
        assert status == 200
        assert isinstance(deny_body, dict)
        assert deny_body["status"] == "denied"
    finally:
        server.shutdown()
        server.server_close()


def test_closure_mismatch_denied_on_resume(tmp_path: Path) -> None:
    project_dir = _setup_test_project(tmp_path, require_approval=True)
    doc = load_project(project_dir)
    loop = Loop(doc, ScriptedProvider("Out"), {})
    token_map = {"sup-tok": Principal(id="u-sup", role="supervisor")}
    authenticator = Authenticator(token_map)
    server = LlmformServer(
        ("127.0.0.1", 0),
        doc,
        loop,
        storage_dir=tmp_path / ".llmform",
        authenticator=authenticator,
    )
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever)
    thread.daemon = True
    thread.start()

    try:
        # Start run and suspend
        status, body = _request(
            f"http://127.0.0.1:{port}/v1/agents/support/runs",
            method="POST",
            data={"input": "Action"},
            headers={"Authorization": "Bearer sup-tok"},
        )
        assert status == 200
        run_id = body["run_id"]
        assert body["status"] == "awaiting_approval"

        # Modify configuration to invalidate closure hash
        yaml_path = project_dir / "llmform.yaml"
        new_content = yaml_path.read_text(encoding="utf-8").replace("llama3", "llama3-modified")
        yaml_path.write_text(new_content, encoding="utf-8")
        # Reload doc on server to reflect project change
        server.document = load_project(project_dir)

        # Attempt to resume with approval -> must be refused with 403
        status, approve_body = _request(
            f"http://127.0.0.1:{port}/v1/runs/{run_id}/approve",
            method="POST",
            headers={"Authorization": "Bearer sup-tok"},
        )
        assert status == 403
        assert isinstance(approve_body, dict)
        assert "closure changed" in approve_body["error"]
    finally:
        server.shutdown()
        server.server_close()


def test_restart_and_resume_across_server_instances(tmp_path: Path) -> None:
    project_dir = _setup_test_project(tmp_path, require_approval=True)
    storage_dir = tmp_path / ".llmform"
    token_map = {
        "dev-tok": Principal(id="u-dev", role="developer"),
        "sup-tok": Principal(id="u-sup", role="supervisor"),
    }

    # 1. Start Server 1
    doc1 = load_project(project_dir)
    loop1 = Loop(doc1, ScriptedProvider("Final output after restart"), {})
    server1 = LlmformServer(
        ("127.0.0.1", 0),
        doc1,
        loop1,
        storage_dir=storage_dir,
        authenticator=Authenticator(token_map),
    )
    port1 = server1.server_port
    thread1 = threading.Thread(target=server1.serve_forever)
    thread1.daemon = True
    thread1.start()

    run_id: str = ""
    try:
        status, body = _request(
            f"http://127.0.0.1:{port1}/v1/agents/support/runs",
            method="POST",
            data={"input": "Action to resume across restart", "session_id": "sess-restart"},
            headers={"Authorization": "Bearer dev-tok"},
        )
        assert status == 200
        assert isinstance(body, dict)
        run_id = body["run_id"]
        assert body["status"] == "awaiting_approval"
    finally:
        server1.shutdown()
        server1.server_close()

    # 2. Simulate server restart: spin up Server 2 using same storage directory
    doc2 = load_project(project_dir)
    loop2 = Loop(doc2, ScriptedProvider("Final output after restart"), {})
    server2 = LlmformServer(
        ("127.0.0.1", 0),
        doc2,
        loop2,
        storage_dir=storage_dir,
        authenticator=Authenticator(token_map),
    )
    port2 = server2.server_port
    thread2 = threading.Thread(target=server2.serve_forever)
    thread2.daemon = True
    thread2.start()

    try:
        # Verify run is recovered
        status, inspect_body = _request(
            f"http://127.0.0.1:{port2}/v1/runs/{run_id}",
            headers={"Authorization": "Bearer dev-tok"},
        )
        assert status == 200
        assert isinstance(inspect_body, dict)
        assert inspect_body["status"] == "awaiting_approval"

        # Supervisor approves on Server 2
        status, approve_body = _request(
            f"http://127.0.0.1:{port2}/v1/runs/{run_id}/approve",
            method="POST",
            headers={"Authorization": "Bearer sup-tok"},
        )
        assert status == 200
        assert isinstance(approve_body, dict)
        assert approve_body["status"] == "completed"
        assert approve_body["output"] == "Final output after restart"
    finally:
        server2.shutdown()
        server2.server_close()


def test_streaming_sse_execution(tmp_path: Path) -> None:
    project_dir = _setup_test_project(tmp_path)
    doc = load_project(project_dir)
    loop = Loop(doc, ScriptedProvider("Streamed response"), {})
    authenticator = Authenticator({"tok": Principal(id="dev", role="developer")})
    server = LlmformServer(
        ("127.0.0.1", 0),
        doc,
        loop,
        storage_dir=tmp_path / ".llmform",
        authenticator=authenticator,
    )
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever)
    thread.daemon = True
    thread.start()

    try:
        # Request with stream=True
        status, raw = _request(
            f"http://127.0.0.1:{port}/v1/agents/support/runs",
            method="POST",
            data={"input": "Hello streaming", "stream": True},
            headers={"Authorization": "Bearer tok"},
        )
        assert status == 200
        assert isinstance(raw, str)
        assert "event: run_started" in raw
        assert "event: run_finished" in raw
    finally:
        server.shutdown()
        server.server_close()


def test_cli_serve_help() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["serve", "--help"])
    assert result.exit_code == 0
    assert "--host" in result.output
    assert "--port" in result.output
    assert "--auth-token" in result.output


def test_session_trimming_preserves_accumulated_classes(tmp_path: Path) -> None:
    from llmform.server import FileSessionStore, Session

    store = FileSessionStore(tmp_path / "sessions", max_messages=2)
    session = Session(
        session_id="sess-trim",
        agent="support",
        messages=[
            Message(role="user", content="msg 1"),
            Message(role="assistant", content="msg 2"),
            Message(role="user", content="msg 3"),
        ],
        classes=["pii", "secret"],
    )
    store.save(session)
    loaded = store.get("sess-trim")
    assert loaded is not None
    # Trimming keeps last 2 messages
    assert len(loaded.messages) == 2
    assert loaded.messages[0].content == "msg 2"
    assert loaded.messages[1].content == "msg 3"
    # Preserves accumulated sensitivity classes
    assert loaded.classes == ["pii", "secret"]


def test_authenticator_formats_and_insecure_dev() -> None:
    auth = Authenticator(insecure_dev=True)
    # Insecure dev returns developer principal
    p = auth.authenticate({})
    assert p is not None
    assert p.role == "developer"

    auth_secure = Authenticator({"my-tok": Principal(id="u1", role="admin")})
    assert auth_secure.authenticate({}) is None
    # Bearer token
    assert auth_secure.authenticate({"authorization": "Bearer my-tok"}).id == "u1"
    # JSON in bearer token
    json_p = auth_secure.authenticate(
        {"authorization": 'Bearer {"id": "json-u", "role": "operator"}'}
    )
    assert json_p is not None
    assert json_p.id == "json-u"
    # X-Principal header
    xp = auth_secure.authenticate({"x-principal": '{"id": "xp-u", "role": "auditor"}'})
    assert xp is not None
    assert xp.role == "auditor"


def test_api_error_branches(tmp_path: Path) -> None:
    project_dir = _setup_test_project(tmp_path)
    doc = load_project(project_dir)
    loop = Loop(doc, ScriptedProvider(), {})
    authenticator = Authenticator(insecure_dev=True)
    server = LlmformServer(
        ("127.0.0.1", 0),
        doc,
        loop,
        storage_dir=tmp_path / ".llmform",
        authenticator=authenticator,
    )
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever)
    thread.daemon = True
    thread.start()

    try:
        # Unknown agent returns 404
        status, body = _request(
            f"http://127.0.0.1:{port}/v1/agents/unknown_agent/runs",
            method="POST",
            data={"input": "test"},
        )
        assert status == 404

        # Unknown run returns 404
        status, body = _request(f"http://127.0.0.1:{port}/v1/runs/non-existent-run")
        assert status == 404

        # Unknown endpoint returns 404
        status, body = _request(f"http://127.0.0.1:{port}/unknown")
        assert status == 404

        # Approve on non-existent run returns 404
        status, body = _request(
            f"http://127.0.0.1:{port}/v1/runs/missing/approve",
            method="POST",
        )
        assert status == 404

        # Deny on non-existent run returns 404
        status, body = _request(
            f"http://127.0.0.1:{port}/v1/runs/missing/deny",
            method="POST",
        )
        assert status == 404

        # Start a completed run
        status, run_body = _request(
            f"http://127.0.0.1:{port}/v1/agents/support/runs",
            method="POST",
            data={"input": "test"},
        )
        run_id = run_body["run_id"]

        # Approve on a run that is completed (not suspended) returns 400
        status, body = _request(
            f"http://127.0.0.1:{port}/v1/runs/{run_id}/approve",
            method="POST",
        )
        assert status == 400

        # Deny on a run that is completed (not suspended) returns 400
        status, body = _request(
            f"http://127.0.0.1:{port}/v1/runs/{run_id}/deny",
            method="POST",
        )
        assert status == 400
    finally:
        server.shutdown()
        server.server_close()
