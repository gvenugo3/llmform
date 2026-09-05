from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from llmform import __version__
from llmform.config.discovery import discover_files, find_project_root
from llmform.config.format import format_file
from llmform.config.interpolation import SecretScrubber, resolve_interpolations
from llmform.config.loader import attach_model_positions, load_project
from llmform.config.models import ProjectConfig
from llmform.config.schema import validate_config_schema
from llmform.config.semantic import validate_semantics
from llmform.config.validate import validate_references
from llmform.diagnostics import Diagnostic, Position, Severity, exit_code, render_all, render_json
from llmform.lock import LOCK_NAME, diff_lock, write_lock
from llmform.policy.cel.compiler import validate_policy_rules
from llmform.policy.cel.environment import validate_schema_profiles

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
def validate(
    path: Annotated[Path | None, typer.Argument(help="Project file or directory")] = None,
    variable: Annotated[list[str] | None, typer.Option("--var", help="Set NAME=VALUE")] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit structured diagnostics")
    ] = False,
    locked: Annotated[
        bool, typer.Option("--locked", help="Fail when llmform.lock differs from this project")
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
