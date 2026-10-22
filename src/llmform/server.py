"""HTTP service for llmform agents with authenticated sessions and async approvals."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from llmform.config.loader import ConfigDocument
from llmform.lock import build_lock
from llmform.providers import Completion, Provider, ToolDefinition
from llmform.runtime import Loop, cel
from llmform.types import Message, Principal, PublicModel, RunState


class ProviderRouter(Provider):
    """Routes completion calls to the appropriate provider based on model ID."""

    def __init__(self, providers: dict[str, Provider], model_to_provider: dict[str, str]):
        self.providers = providers
        self.model_to_provider = model_to_provider

    def complete(
        self,
        messages: list[Message],
        *,
        model: str,
        tools: list[ToolDefinition],
        output_schema: dict[str, Any] | None = None,
    ) -> Completion:
        provider_name = self.model_to_provider.get(model)
        if not provider_name or provider_name not in self.providers:
            if len(self.providers) == 1:
                return next(iter(self.providers.values())).complete(
                    messages, model=model, tools=tools, output_schema=output_schema
                )
            raise ValueError(f"no provider found for model {model!r}")
        return self.providers[provider_name].complete(
            messages, model=model, tools=tools, output_schema=output_schema
        )


class Session(PublicModel):
    """A multi-turn conversation session preserving accumulated sensitivity classes."""

    session_id: str
    agent: str
    messages: list[Message] = []
    classes: list[str] = []


class FileSessionStore:
    """Persists sessions to disk while strictly retaining accumulated classes across trimming."""

    def __init__(self, root: Path, max_messages: int = 100):
        self.root = root / "sessions"
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_messages = max_messages

    def get(self, session_id: str) -> Session | None:
        path = self.root / f"{session_id}.json"
        if not path.is_file():
            return None
        try:
            return Session.model_validate_json(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return None

    def save(self, session: Session) -> None:
        if len(session.messages) > self.max_messages:
            session.messages = session.messages[-self.max_messages :]
        path = self.root / f"{session.session_id}.json"
        path.write_text(session.model_dump_json(indent=2), encoding="utf-8")


class FileRunStore:
    """Persists runs, metadata, and event histories across server lifecycles."""

    def __init__(self, root: Path):
        self.root = root / "runs"
        self.root.mkdir(parents=True, exist_ok=True)

    def get(self, run_id: str) -> RunState | None:
        path = self.root / f"{run_id}.json"
        if not path.is_file():
            return None
        try:
            return RunState.model_validate_json(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return None

    def save(
        self,
        state: RunState,
        session_id: str | None = None,
        events: list[dict[str, Any]] | None = None,
    ) -> None:
        path = self.root / f"{state.run_id}.json"
        path.write_text(state.model_dump_json(indent=2), encoding="utf-8")
        if session_id is not None:
            meta_path = self.root / f"{state.run_id}.meta.json"
            meta_path.write_text(json.dumps({"session_id": session_id}, indent=2), encoding="utf-8")
        if events is not None:
            events_path = self.root / f"{state.run_id}.events.json"
            events_path.write_text(json.dumps(events, indent=2), encoding="utf-8")

    def get_session_id(self, run_id: str) -> str | None:
        meta_path = self.root / f"{run_id}.meta.json"
        if not meta_path.is_file():
            return None
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
            return data.get("session_id")
        except (ValueError, OSError):
            return None

    def get_events(self, run_id: str) -> list[dict[str, Any]]:
        events_path = self.root / f"{run_id}.events.json"
        if not events_path.is_file():
            return []
        try:
            return json.loads(events_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return []

    def append_event(self, run_id: str, event: dict[str, Any]) -> None:
        events = self.get_events(run_id)
        events.append(event)
        events_path = self.root / f"{run_id}.events.json"
        events_path.write_text(json.dumps(events, indent=2), encoding="utf-8")


class Authenticator:
    """Resolves authenticated principals from headers or configured token maps."""

    def __init__(
        self,
        token_map: dict[str, Principal] | None = None,
        *,
        insecure_dev: bool = False,
    ):
        self.token_map = dict(token_map or {})
        self.insecure_dev = insecure_dev

    def authenticate(self, headers: dict[str, str]) -> Principal | None:
        auth_header = headers.get("authorization", "").strip()
        token = ""
        if auth_header.lower().startswith("bearer "):
            token = auth_header[7:].strip()
        elif auth_header:
            token = auth_header

        if token and token in self.token_map:
            return self.token_map[token]

        if token and token.startswith("{") and token.endswith("}"):
            try:
                return Principal.model_validate_json(token)
            except ValueError:
                pass

        principal_header = headers.get("x-principal", "").strip()
        if principal_header:
            try:
                return Principal.model_validate_json(principal_header)
            except ValueError:
                pass

        if self.insecure_dev:
            return Principal(id="developer", role="developer")
        return None


def _format_run_response(state: RunState) -> dict[str, Any]:
    api_status = "awaiting_approval" if state.status == "suspended" else state.status
    payload: dict[str, Any] = {
        "run_id": state.run_id,
        "agent": state.agent,
        "status": api_status,
        "output": state.result,
        "failure": state.failure,
        "classes": state.classes,
        "cost_usd": state.cost_usd,
        "iteration": state.iteration,
    }
    if state.pending:
        payload["pending"] = state.pending.model_dump()
    return payload


class LlmformRequestHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the v0.2 llmform service surface."""

    server: LlmformServer

    def _headers(self) -> dict[str, str]:
        return {key.lower(): value for key, value in self.headers.items()}

    def _send_json(self, status: int, data: Any) -> None:
        encoded = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _read_json(self) -> dict[str, Any]:
        length_header = self.headers.get("content-length")
        if not length_header:
            return {}
        try:
            length = int(length_header)
            raw = self.rfile.read(length).decode("utf-8")
            return json.loads(raw) if raw.strip() else {}
        except (ValueError, UnicodeDecodeError):
            return {}

    def _require_auth(self) -> Principal | None:
        principal = self.server.authenticator.authenticate(self._headers())
        if principal is None:
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Bearer realm="llmform"')
            self.send_header("Content-Type", "application/json; charset=utf-8")
            body = json.dumps({"error": "Unauthorized"}).encode("utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return None
        return principal

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path == "/healthz":
            self._send_json(200, {"status": "ok", "version": "0.2.0"})
            return

        principal = self._require_auth()
        if principal is None:
            return

        if path.startswith("/v1/runs/"):
            parts = path.split("/")
            if len(parts) == 4:
                run_id = parts[3]
                state = self.server.run_store.get(run_id)
                if state is None:
                    self._send_json(404, {"error": f"run {run_id!r} not found"})
                    return
                self._send_json(200, _format_run_response(state))
                return
            if len(parts) == 5 and parts[4] == "events":
                run_id = parts[3]
                state = self.server.run_store.get(run_id)
                if state is None:
                    self._send_json(404, {"error": f"run {run_id!r} not found"})
                    return
                events = self.server.run_store.get_events(run_id)
                accept = self.headers.get("accept", "")
                if "text/event-stream" in accept:
                    self._stream_events(events)
                    return
                self._send_json(200, {"run_id": run_id, "events": events})
                return

        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802, C901
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        principal = self._require_auth()
        if principal is None:
            return

        if path.startswith("/v1/agents/") and path.endswith("/runs"):
            agent_name = path[len("/v1/agents/") : -len("/runs")].strip("/")
            self._handle_create_run(agent_name, principal)
            return

        if path.startswith("/v1/runs/") and path.endswith("/approve"):
            parts = path.split("/")
            if len(parts) == 5:
                run_id = parts[3]
                self._handle_approve(run_id, principal)
                return

        if path.startswith("/v1/runs/") and path.endswith("/deny"):
            parts = path.split("/")
            if len(parts) == 5:
                run_id = parts[3]
                self._handle_deny(run_id, principal)
                return

        self._send_json(404, {"error": "not found"})

    def _handle_create_run(self, agent: str, principal: Principal) -> None:
        agent_name = agent.removeprefix("agent.")
        if (
            self.server.document.config is None
            or agent_name not in self.server.document.config.agents
        ):
            self._send_json(404, {"error": f"agent {agent!r} not found"})
            return

        body = self._read_json()
        raw_input = body.get("input", body.get("text", ""))
        text = json.dumps(raw_input) if isinstance(raw_input, (dict, list)) else str(raw_input)
        session_id = body.get("session_id")
        stream = bool(body.get("stream", False)) or "text/event-stream" in self.headers.get(
            "accept", ""
        )

        session: Session | None = None
        if session_id:
            session = self.server.session_store.get(session_id)
            if session is None:
                session = Session(session_id=session_id, agent=agent_name)

        loop = self.server.loop
        state = loop.start(agent_name, principal, text)
        if session is not None:
            merged_classes = list(dict.fromkeys(session.classes + state.classes))
            all_messages = list(session.messages) + list(state.messages)
            state = state.model_copy(update={"classes": merged_classes, "messages": all_messages})

        events: list[dict[str, Any]] = [
            {"event": "run_started", "run_id": state.run_id, "agent": agent_name}
        ]

        if stream:
            self._stream_run_execution(state, session, events)
            return

        while state.status in {"running", "suspended"}:
            if state.pending:
                break
            state = loop.step(state)
            events.append(
                {
                    "event": "step",
                    "status": state.status,
                    "iteration": state.iteration,
                    "cost_usd": state.cost_usd,
                }
            )

        events.append(
            {
                "event": "run_finished",
                "status": "awaiting_approval" if state.status == "suspended" else state.status,
                "output": state.result,
            }
        )
        self.server.run_store.save(state, session_id=session_id, events=events)
        if session is not None and state.status == "completed":
            session.messages = list(state.messages)
            session.classes = list(dict.fromkeys(session.classes + state.classes))
            self.server.session_store.save(session)

        self._send_json(200, _format_run_response(state))

    def _stream_run_execution(
        self,
        state: RunState,
        session: Session | None,
        events: list[dict[str, Any]],
    ) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        def emit(event_type: str, data: dict[str, Any]) -> None:
            chunk = f"event: {event_type}\ndata: {json.dumps(data)}\n\n".encode()
            self.wfile.write(chunk)
            self.wfile.flush()

        emit("run_started", {"run_id": state.run_id, "agent": state.agent})
        loop = self.server.loop
        while state.status in {"running", "suspended"}:
            if state.pending:
                break
            state = loop.step(state)
            step_data = {
                "status": state.status,
                "iteration": state.iteration,
                "cost_usd": state.cost_usd,
            }
            events.append({"event": "step", **step_data})
            emit("step", step_data)

        api_status = "awaiting_approval" if state.status == "suspended" else state.status
        finished_data = {
            "run_id": state.run_id,
            "status": api_status,
            "output": state.result,
            "failure": state.failure,
            "pending": state.pending.model_dump() if state.pending else None,
        }
        events.append({"event": "run_finished", **finished_data})
        emit("run_finished", finished_data)

        session_id = session.session_id if session else None
        self.server.run_store.save(state, session_id=session_id, events=events)
        if session is not None and state.status == "completed":
            session.messages = list(state.messages)
            session.classes = list(dict.fromkeys(session.classes + state.classes))
            self.server.session_store.save(session)
        self.close_connection = True

    def _stream_events(self, events: list[dict[str, Any]]) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        for item in events:
            ev_type = item.get("event", "message")
            chunk = f"event: {ev_type}\ndata: {json.dumps(item)}\n\n".encode()
            self.wfile.write(chunk)
        self.wfile.flush()
        self.close_connection = True

    def _handle_approve(self, run_id: str, approver: Principal) -> None:
        state = self.server.run_store.get(run_id)
        if state is None:
            self._send_json(404, {"error": f"run {run_id!r} not found"})
            return
        if state.status != "suspended" or state.pending is None:
            self._send_json(400, {"error": "run is not awaiting approval"})
            return

        current_closure = build_lock(self.server.document)["closure_sha256"]
        if state.closure != current_closure:
            denied_state = state.model_copy(
                update={
                    "pending": None,
                    "status": "failed",
                    "failure": "configuration closure changed; cannot resume",
                }
            )
            self.server.run_store.save(denied_state)
            self._send_json(
                403,
                {
                    "error": "configuration closure changed; cannot resume",
                    "status": "failed",
                    "run_id": run_id,
                },
            )
            return

        pending = state.pending
        qualifies = cel(
            pending.approvers,
            {"principal": approver.model_dump(), "run": state.model_dump(mode="json")},
        )
        if not qualifies:
            denied_state = self.server.loop.step(state, approved=False, approver=approver)
            self.server.run_store.save(denied_state)
            self._send_json(
                403,
                {
                    "error": f"approval denied; requires {pending.approvers}",
                    "status": "denied",
                    "run_id": run_id,
                },
            )
            return

        loop = self.server.loop
        state = loop.step(state, approved=True, approver=approver)
        while state.status in {"running", "suspended"}:
            if state.pending:
                break
            state = loop.step(state)

        session_id = self.server.run_store.get_session_id(run_id)
        self.server.run_store.save(state, session_id=session_id)
        if session_id and state.status == "completed":
            session = self.server.session_store.get(session_id)
            if session:
                session.messages = list(state.messages)
                session.classes = list(dict.fromkeys(session.classes + state.classes))
                self.server.session_store.save(session)

        self._send_json(200, _format_run_response(state))

    def _handle_deny(self, run_id: str, principal: Principal) -> None:
        state = self.server.run_store.get(run_id)
        if state is None:
            self._send_json(404, {"error": f"run {run_id!r} not found"})
            return
        if state.status != "suspended":
            self._send_json(400, {"error": "run is not awaiting approval"})
            return

        state = self.server.loop.step(state, approved=False, approver=principal)
        session_id = self.server.run_store.get_session_id(run_id)
        self.server.run_store.save(state, session_id=session_id)
        self._send_json(200, _format_run_response(state))


class LlmformServer(ThreadingHTTPServer):
    """Multi-threaded HTTP server powering `llmform serve`."""

    def __init__(
        self,
        server_address: tuple[str, int],
        document: ConfigDocument,
        loop: Loop,
        *,
        storage_dir: Path | None = None,
        authenticator: Authenticator | None = None,
    ):
        super().__init__(server_address, LlmformRequestHandler)
        self.document = document
        self.loop = loop
        storage = storage_dir or (document.root / ".llmform")
        self.session_store = FileSessionStore(storage)
        self.run_store = FileRunStore(storage)
        self.authenticator = authenticator or Authenticator(insecure_dev=True)
