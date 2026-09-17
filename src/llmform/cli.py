from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from llmform import __version__
from llmform.config.discovery import discover_files, find_project_root
from llmform.config.format import format_file
from llmform.config.interpolation import SecretScrubber, resolve_interpolations
from llmform.config.loader import attach_model_positions, load_project
from llmform.config.models import ProjectConfig
from llmform.config.online import validate_online
from llmform.config.schema import validate_config_schema
from llmform.config.semantic import validate_semantics
from llmform.config.validate import validate_references
from llmform.diagnostics import Diagnostic, Position, Severity, exit_code, render_all, render_json
from llmform.lock import LOCK_NAME, diff_lock, write_lock
from llmform.policy.cel.compiler import validate_policy_rules
from llmform.policy.cel.environment import validate_schema_profiles
from llmform.providers.http import OllamaProvider, OpenAIProvider
from llmform.runtime import Loop
from llmform.sources.http import HttpOperationSource
from llmform.sources.mcp import McpSource
from llmform.types import Principal

app = typer.Typer(no_args_is_help=True, pretty_exceptions_enable=False)

_INIT_CONFIG = """version: "0.1"
providers:
  local:
    type: ollama
    endpoint: http://localhost:11434
models:
  default:
    provider: provider.local
    id: llama3.2
policies:
  review_request:
    on: request
    rule: "true"
    otherwise: REQUIRE_APPROVAL
    approval:
      approvers: principal.role == "developer"
agents:
  assistant:
    model: model.default
    instructions: prompts/assistant.md
    policies: [policy.review_request]
"""


def _scrub_diagnostics(diagnostics: list[Diagnostic], scrubber: SecretScrubber) -> list[Diagnostic]:
    """Redact secrets in diagnostic fields before rendering them in any format."""

    return [
        Diagnostic(
            item.code,
            item.severity,
            scrubber.scrub(item.message),
            item.position,
            scrubber.scrub(item.hint) if item.hint else None,
            scrubber.scrub(item.suggestion) if item.suggestion else None,
            scrubber.scrub(item.context) if item.context else None,
        )
        for item in diagnostics
    ]


def _parse_variables(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise typer.BadParameter("variables must use NAME=VALUE")
        name, resolved = value.split("=", 1)
        result[name] = resolved
    return result


@app.command()
def version() -> None:
    """Print the llmform version."""
    typer.echo(__version__)


@app.command()
def init(
    path: Annotated[Path | None, typer.Argument(help="Directory for the new project")] = None,
) -> None:
    """Create a minimal project with a checked-in closure lockfile."""

    root = (path or Path.cwd()).resolve()
    config = root / "llmform.yaml"
    if config.exists():
        raise typer.BadParameter(f"{config} already exists")
    root.mkdir(parents=True, exist_ok=True)
    (root / "prompts").mkdir(exist_ok=True)
    config.write_text(_INIT_CONFIG, encoding="utf-8")
    (root / "prompts" / "assistant.md").write_text(
        "You are a helpful assistant.\n", encoding="utf-8"
    )
    document = load_project(root)
    if document.config is None or document.diagnostics:
        raise typer.Exit(1)
    write_lock(document)
    typer.echo(f"Initialized {root}")


@app.command()
def fmt(
    path: Annotated[Path | None, typer.Argument(help="Project file or directory")] = None,
) -> None:
    """Canonicalize project YAML while preserving comments."""

    root = find_project_root(path or Path.cwd())
    if root is None:
        raise typer.BadParameter("no llmform configuration found")
    changed = sum(format_file(file) for file in discover_files(root))
    typer.echo(f"Formatted {changed} file(s)")


@app.command()
def lock(
    path: Annotated[Path | None, typer.Argument(help="Project file or directory")] = None,
    variable: Annotated[list[str] | None, typer.Option("--var", help="Set NAME=VALUE")] = None,
) -> None:
    """Write the canonical offline closure lockfile."""

    document = load_project(path or Path.cwd())
    diagnostics = list(document.diagnostics)
    scrubber = None
    if document.files and not any(item.severity == Severity.ERROR for item in diagnostics):
        resolved, interpolation_diagnostics, scrubber = resolve_interpolations(
            document, _parse_variables(variable or [])
        )
        diagnostics.extend(interpolation_diagnostics)
        if not interpolation_diagnostics:
            try:
                diagnostics.extend(
                    validate_config_schema(
                        resolved,
                        document.positions,
                        document.key_positions,
                        document.position(()),
                    )
                )
                if not any(item.severity == Severity.ERROR for item in diagnostics):
                    document.config = ProjectConfig.model_validate(resolved)
                    attach_model_positions(document.config, document)
                    diagnostics.extend(validate_references(document))
                    diagnostics.extend(validate_schema_profiles(document))
                    diagnostics.extend(validate_policy_rules(document))
                    diagnostics.extend(validate_semantics(document))
            except Exception as exc:
                typer.echo(scrubber.scrub(str(exc)), err=True)
                raise typer.Exit(1) from None
    if not any(item.severity == Severity.ERROR for item in diagnostics):
        try:
            lockfile = write_lock(document)
        except OSError as exc:
            diagnostics.append(
                Diagnostic(
                    "LLMF102",
                    Severity.ERROR,
                    f"cannot write {LOCK_NAME}: {exc}",
                    Position(document.root),
                )
            )
        else:
            typer.echo(f"Wrote {lockfile}")
    if diagnostics:
        diagnostics = _scrub_diagnostics(diagnostics, scrubber) if scrubber else diagnostics
        typer.echo(render_all(diagnostics, document.root))
    if code := exit_code(diagnostics):
        raise typer.Exit(code)


@app.command()
def run(
    agent: Annotated[str, typer.Argument(help="Agent name")],
    path: Annotated[Path | None, typer.Option("--path", help="Project file or directory")] = None,
    input_text: Annotated[str | None, typer.Option("--input", help="User input")] = None,
    input_file: Annotated[
        Path | None, typer.Option("--input-file", help="File containing input")
    ] = None,
    principal: Annotated[str | None, typer.Option("--principal", help="JSON principal")] = None,
    trust_sources: Annotated[bool, typer.Option("--trust-sources")] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Execute an agent locally with runtime policy enforcement."""
    document = load_project(path or Path.cwd())
    if document.config is None or document.diagnostics:
        typer.echo(render_all(document.diagnostics, document.root), err=True)
        raise typer.Exit(1)
    agent_name = agent.removeprefix("agent.")
    if agent_name not in document.config.agents:
        raise typer.BadParameter(f"unknown agent {agent!r}")
    if input_text is not None and input_file is not None:
        raise typer.BadParameter("use only one of --input or --input-file")
    text = (
        input_text
        if input_text is not None
        else (input_file.read_text() if input_file else typer.get_text_stream("stdin").read())
    )
    actor = (
        Principal.model_validate_json(principal)
        if principal
        else Principal(id="developer", role="developer")
    )
    model = document.config.models[document.config.agents[agent_name].model.removeprefix("model.")]
    provider_config = document.config.providers[model.provider.removeprefix("provider.")]
    if provider_config.type == "openai":
        if not provider_config.api_key:
            raise typer.BadParameter("OpenAI provider requires api_key")
        provider = OpenAIProvider(
            provider_config.api_key,
            provider_config.endpoint or "https://api.openai.com/v1/responses",
        )
    else:
        provider = OllamaProvider(provider_config.endpoint or "http://localhost:11434/api/chat")
    schemas: dict[str, dict] = {}
    for source in document.config.sources.values():
        for operation in source.operations.values():
            schemas[operation.returns] = json.loads((document.root / operation.returns).read_text())
    sources = {}
    mcp_sources = [source for source in document.config.sources.values() if source.type == "mcp"]
    if mcp_sources:
        try:
            lock_changed = "sources" in diff_lock(document)
        except (OSError, ValueError):
            lock_changed = True
        if lock_changed and not trust_sources:
            raise typer.BadParameter(
                "MCP argv is not recorded in llmform.lock; review it and use --trust-sources"
            )
        if lock_changed:
            write_lock(document)
    for name, source in document.config.sources.items():
        if source.type == "http":
            sources[name] = HttpOperationSource(source, schemas)
        else:
            sources[name] = McpSource(source.command, trusted=trust_sources)
    loop = Loop(document, provider, sources, audit_path=document.root / "llmform.audit.jsonl")
    state = loop.start(agent_name, actor, text)
    while state.status in {"running", "suspended"}:
        if state.pending:
            typer.echo(
                f"Approval required by {state.pending.policy}: {state.pending.approvers}", err=True
            )
            state = loop.step(state, approved=typer.confirm("Approve this action?", default=False))
        else:
            state = loop.step(state)
    for source in sources.values():
        if isinstance(source, McpSource):
            source.close()
    if json_output:
        typer.echo(state.model_dump_json())
    elif state.status == "completed":
        typer.echo(state.result)
    else:
        typer.echo(f"{state.status}: {state.failure}", err=True)
    if state.status != "completed":
        raise typer.Exit(1)


@app.command()
def validate(
    path: Annotated[Path | None, typer.Argument(help="Project file or directory")] = None,
    variable: Annotated[list[str] | None, typer.Option("--var", help="Set NAME=VALUE")] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit structured diagnostics")
    ] = False,
    locked: Annotated[
        bool, typer.Option("--locked", help="Fail when llmform.lock differs from this project")
    ] = False,
    online: Annotated[
        bool, typer.Option("--online", help="Run opt-in endpoint reachability checks")
    ] = False,
) -> None:
    """Validate an llmform project offline."""
    document = load_project(path or Path.cwd())
    diagnostics = list(document.diagnostics)
    if document.files and not any(item.severity == Severity.ERROR for item in diagnostics):
        resolved, interpolation_diagnostics, scrubber = resolve_interpolations(
            document, _parse_variables(variable or [])
        )
        diagnostics.extend(interpolation_diagnostics)
        if not interpolation_diagnostics:
            try:
                diagnostics.extend(
                    validate_config_schema(
                        resolved,
                        document.positions,
                        document.key_positions,
                        document.position(()),
                    )
                )
                if not any(item.severity == Severity.ERROR for item in diagnostics):
                    document.config = ProjectConfig.model_validate(resolved)
                    attach_model_positions(document.config, document)
                    diagnostics.extend(validate_references(document))
                    diagnostics.extend(validate_schema_profiles(document))
                    diagnostics.extend(validate_policy_rules(document))
                    diagnostics.extend(validate_semantics(document))
                    if online and not any(item.severity == Severity.ERROR for item in diagnostics):
                        diagnostics.extend(validate_online(document))
                    if locked and not any(item.severity == Severity.ERROR for item in diagnostics):
                        try:
                            changed = diff_lock(document)
                        except (OSError, ValueError) as exc:
                            diagnostics.append(
                                Diagnostic(
                                    "LLMF102",
                                    Severity.ERROR,
                                    f"cannot verify {LOCK_NAME}: {exc}",
                                    Position(document.root / LOCK_NAME),
                                )
                            )
                        else:
                            if changed:
                                diagnostics.append(
                                    Diagnostic(
                                        "LLMF102",
                                        Severity.ERROR,
                                        f"{LOCK_NAME} is out of date",
                                        Position(document.root / LOCK_NAME),
                                        f"changed sections: {', '.join(changed)}",
                                    )
                                )
            except Exception as exc:  # Last-resort boundary prevents resolved-secret leakage.
                typer.echo(scrubber.scrub(str(exc)), err=True)
                raise typer.Exit(1) from None
    else:
        scrubber = None

    if scrubber:
        diagnostics = _scrub_diagnostics(diagnostics, scrubber)
    if json_output:
        output = render_json(diagnostics)
    else:
        output = render_all(diagnostics, document.root)
        if not output:
            output = f"Valid: {len(document.files)} file(s), 0 findings"
    typer.echo(output)
    if code := exit_code(diagnostics):
        raise typer.Exit(code)
