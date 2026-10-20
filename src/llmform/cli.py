from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from llmform import __version__
from llmform.config.discovery import discover_files, find_project_root
from llmform.config.format import format_file
from llmform.config.interpolation import SecretScrubber, resolve_interpolations
from llmform.config.loader import ConfigDocument, attach_model_positions, load_project
from llmform.config.models import ProjectConfig
from llmform.config.online import validate_online
from llmform.config.schema import validate_config_schema
from llmform.config.semantic import validate_semantics
from llmform.config.validate import validate_references
from llmform.diagnostics import Diagnostic, Position, Severity, exit_code, render_all, render_json
from llmform.lock import LOCK_NAME, diff_lock, write_lock
from llmform.plan import PlanError, build_plan, project_at_ref
from llmform.policy.cel.compiler import validate_policy_rules
from llmform.policy.cel.environment import validate_schema_profiles
from llmform.providers import ProviderError
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


def _has_error(diagnostics: list[Diagnostic]) -> bool:
    return any(item.severity == Severity.ERROR for item in diagnostics)


def _check_project(
    document: ConfigDocument, variables: list[str] | None
) -> tuple[list[Diagnostic], SecretScrubber | None]:
    """Resolve interpolations and run every offline check, filling in ``document.config``.

    load_project leaves the config unset while ``${...}`` values are pending, so every
    command that needs a typed config goes through here.
    """

    diagnostics = list(document.diagnostics)
    if not document.files or _has_error(diagnostics):
        return diagnostics, None
    resolved, interpolation_diagnostics, scrubber = resolve_interpolations(
        document, _parse_variables(variables or [])
    )
    diagnostics.extend(interpolation_diagnostics)
    if interpolation_diagnostics:
        return diagnostics, scrubber
    try:
        diagnostics.extend(
            validate_config_schema(
                resolved,
                document.positions,
                document.key_positions,
                document.position(()),
            )
        )
        if not _has_error(diagnostics):
            document.config = ProjectConfig.model_validate(resolved)
            attach_model_positions(document.config, document)
            diagnostics.extend(validate_references(document))
            diagnostics.extend(validate_schema_profiles(document))
            diagnostics.extend(validate_policy_rules(document))
            diagnostics.extend(validate_semantics(document))
    except Exception as exc:  # Last-resort boundary prevents resolved-secret leakage.
        typer.echo(scrubber.scrub(str(exc)), err=True)
        raise typer.Exit(1) from None
    return diagnostics, scrubber


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
    diagnostics, scrubber = _check_project(document, variable)
    if not _has_error(diagnostics):
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
def plan(
    path: Annotated[Path | None, typer.Argument(help="Project file or directory")] = None,
    from_ref: Annotated[
        str, typer.Option("--from", help="Git ref containing the baseline configuration")
    ] = "HEAD",
    fail_on_privilege: Annotated[
        bool, typer.Option("--fail-on-privilege", help="Exit non-zero for privilege changes")
    ] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Emit a JSON plan")] = False,
    variable: Annotated[list[str] | None, typer.Option("--var", help="Set NAME=VALUE")] = None,
) -> None:
    """Compare this project with its locked configuration at a Git ref."""

    current = load_project(path or Path.cwd())
    diagnostics, scrubber = _check_project(current, variable)
    if _has_error(diagnostics):
        if scrubber:
            diagnostics = _scrub_diagnostics(diagnostics, scrubber)
        typer.echo(render_all(diagnostics, current.root), err=True)
        raise typer.Exit(exit_code(diagnostics) or 1)

    try:
        with project_at_ref(current.root, from_ref) as baseline_root:
            baseline = load_project(baseline_root)
            baseline_diagnostics, baseline_scrubber = _check_project(baseline, variable)
            if _has_error(baseline_diagnostics):
                if baseline_scrubber:
                    baseline_diagnostics = _scrub_diagnostics(
                        baseline_diagnostics, baseline_scrubber
                    )
                typer.echo(render_all(baseline_diagnostics, baseline.root), err=True)
                raise typer.Exit(exit_code(baseline_diagnostics) or 1)
            try:
                stale_sections = diff_lock(baseline)
            except (OSError, ValueError) as exc:
                raise PlanError(f"baseline {LOCK_NAME} cannot be verified: {exc}") from exc
            if stale_sections:
                raise PlanError(
                    f"baseline {LOCK_NAME} is out of date "
                    f"(changed sections: {', '.join(stale_sections)})"
                )
            result = build_plan(baseline, current)
    except PlanError as exc:
        typer.echo(f"Cannot create plan: {exc}", err=True)
        raise typer.Exit(1) from None

    summary = result["summary"]
    assert isinstance(summary, dict)
    if json_output:
        typer.echo(json.dumps(result, indent=2, sort_keys=True))
    else:
        if not result["changes"]:
            typer.echo(f"No changes from {from_ref}.")
        else:
            typer.echo(
                f"Plan from {from_ref}: {summary['changes']} change(s), "
                f"{summary['privilege_changes']} privilege, "
                f"{summary['behavioral_changes']} behavioral, "
                f"{summary['cosmetic_changes']} cosmetic"
            )
            typer.echo("Changes:")
            for change in result["changes"]:
                assert isinstance(change, dict)
                typer.echo(
                    f"- {change['classification'].upper()} {change['action']} {change['path']}"
                )
        cost_impact = result["cost_impact"]
        assert isinstance(cost_impact, list)
        typer.echo("Declared cost impact:")
        if cost_impact:
            for line in cost_impact:
                typer.echo(f"- {line}")
        else:
            typer.echo("- none")

    if fail_on_privilege and summary["privilege_changes"]:
        raise typer.Exit(1)


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
    variable: Annotated[list[str] | None, typer.Option("--var", help="Set NAME=VALUE")] = None,
) -> None:
    """Execute an agent locally with runtime policy enforcement."""
    document = load_project(path or Path.cwd())
    diagnostics, scrubber = _check_project(document, variable)
    if document.config is None or _has_error(diagnostics):
        diagnostics = _scrub_diagnostics(diagnostics, scrubber) if scrubber else diagnostics
        typer.echo(render_all(diagnostics, document.root), err=True)
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
    try:
        state = loop.start(agent_name, actor, text)
        while state.status in {"running", "suspended"}:
            if state.pending:
                typer.echo(
                    f"Approval required by {state.pending.policy}: {state.pending.approvers}",
                    err=True,
                )
                approved = typer.confirm("Approve this action?", default=False)
                state = loop.step(state, approved=approved)
            else:
                state = loop.step(state)
    except ProviderError as exc:
        message = f"provider error ({exc.kind.value}): {exc}"
        typer.echo(scrubber.scrub(message) if scrubber else message, err=True)
        raise typer.Exit(1) from None
    finally:
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


def _lock_findings(document: ConfigDocument) -> list[Diagnostic]:
    try:
        changed = diff_lock(document)
    except (OSError, ValueError) as exc:
        return [
            Diagnostic(
                "LLMF102",
                Severity.ERROR,
                f"cannot verify {LOCK_NAME}: {exc}",
                Position(document.root / LOCK_NAME),
            )
        ]
    if not changed:
        return []
    return [
        Diagnostic(
            "LLMF102",
            Severity.ERROR,
            f"{LOCK_NAME} is out of date",
            Position(document.root / LOCK_NAME),
            f"changed sections: {', '.join(changed)}",
        )
    ]


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
    diagnostics, scrubber = _check_project(document, variable)
    if document.config is not None and not _has_error(diagnostics):
        try:
            if online:
                diagnostics.extend(validate_online(document))
            if locked and not _has_error(diagnostics):
                diagnostics.extend(_lock_findings(document))
        except Exception as exc:  # Last-resort boundary prevents resolved-secret leakage.
            typer.echo(scrubber.scrub(str(exc)) if scrubber else str(exc), err=True)
            raise typer.Exit(1) from None

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
