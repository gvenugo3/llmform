from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from llmform import __version__
from llmform.config.interpolation import resolve_interpolations
from llmform.config.loader import load_project
from llmform.config.models import ProjectConfig
from llmform.config.semantic import validate_semantics
from llmform.config.validate import validate_references
from llmform.diagnostics import Severity, render_all
from llmform.policy.cel.compiler import validate_policy_rules
from llmform.policy.cel.environment import validate_schema_profiles

app = typer.Typer(no_args_is_help=True, pretty_exceptions_enable=False)


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
def validate(
    path: Annotated[Path | None, typer.Argument(help="Project file or directory")] = None,
    variable: Annotated[list[str] | None, typer.Option("--var", help="Set NAME=VALUE")] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit structured diagnostics")
    ] = False,
) -> None:
    """Validate an llmform project offline."""
    document = load_project(path or Path.cwd())
    diagnostics = list(document.diagnostics)
    if document.config is not None:
        resolved, interpolation_diagnostics, scrubber = resolve_interpolations(
            document, _parse_variables(variable or [])
        )
        diagnostics.extend(interpolation_diagnostics)
        if not interpolation_diagnostics:
            try:
                document.config = ProjectConfig.model_validate(resolved)
            except Exception as exc:  # pragma: no cover - interpolation type errors are unusual
                typer.echo(scrubber.scrub(str(exc)), err=True)
                raise typer.Exit(1) from None
            diagnostics.extend(validate_references(document))
            diagnostics.extend(validate_schema_profiles(document))
            diagnostics.extend(validate_policy_rules(document))
            diagnostics.extend(validate_semantics(document))
    else:
        scrubber = None

    if json_output:
        payload = [
            {
                "code": item.code,
                "severity": item.severity.value,
                "message": item.message,
                "file": str(item.position.file),
                "line": item.position.line,
                "column": item.position.column,
                "hint": item.hint,
            }
            for item in diagnostics
        ]
        output = json.dumps(payload, indent=2)
    else:
        output = render_all(diagnostics, document.root)
        if not output:
            output = f"Valid: {len(document.files)} file(s), 0 findings"
    typer.echo(scrubber.scrub(output) if scrubber else output)
    if any(item.severity == Severity.ERROR for item in diagnostics):
        raise typer.Exit(1)
