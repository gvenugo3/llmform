from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from llmform.config.loader import ConfigDocument, PathKey
from llmform.diagnostics import Diagnostic, Severity

INTERPOLATION = re.compile(r"^\$\{(env|var|secret)\.([A-Za-z_][A-Za-z0-9_]*)\}$")


class SecretProvider(Protocol):
    """Resolve secrets without exposing their storage mechanism to configuration loading."""

    def get(self, name: str) -> str | None: ...


@dataclass(frozen=True)
class EnvironmentSecretProvider:
    """The v0.1 secret provider, backed by an environment mapping."""

    environ: Mapping[str, str] = field(default_factory=lambda: os.environ)

    def get(self, name: str) -> str | None:
        return self.environ.get(name)


@dataclass
class SecretScrubber:
    values: set[str] = field(default_factory=set)

    def register(self, value: str) -> None:
        if value:
            self.values.add(value)

    def scrub(self, text: str) -> str:
        for value in sorted(self.values, key=len, reverse=True):
            text = text.replace(value, "[REDACTED]")
        return text

    def scrub_value(self, value: Any) -> Any:
        """Recursively scrub a structured diagnostic or audit payload."""

        if isinstance(value, str):
            return self.scrub(value)
        if isinstance(value, dict):
            return {key: self.scrub_value(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.scrub_value(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self.scrub_value(item) for item in value)
        return value


def _parse_cli_variable(value: str, declared_type: str) -> Any:
    if declared_type == "string":
        return value
    if declared_type == "integer":
        return int(value)
    if declared_type == "number":
        return float(value)
    if declared_type == "boolean":
        normalized = value.casefold()
        if normalized in {"true", "false"}:
            return normalized == "true"
        raise ValueError("expected true or false")
    raise ValueError(f"unknown variable type {declared_type!r}")


def resolve_interpolations(
    document: ConfigDocument,
    cli_variables: Mapping[str, str] | None = None,
    environ: Mapping[str, str] | None = None,
    secret_provider: SecretProvider | None = None,
) -> tuple[dict[str, Any], list[Diagnostic], SecretScrubber]:
    environ = os.environ if environ is None else environ
    secret_provider = secret_provider or EnvironmentSecretProvider(environ)
    variables: dict[str, Any] = {}
    raw_declarations = document.raw.get("variables", {})
    declarations = raw_declarations if isinstance(raw_declarations, Mapping) else {}
    for name, declaration in declarations.items():
        if isinstance(declaration, dict) and declaration.get("default") is not None:
            variables[name] = declaration["default"]
    diagnostics: list[Diagnostic] = []
    scrubber = SecretScrubber()

    for name, value in (cli_variables or {}).items():
        declaration = declarations.get(name)
        if not isinstance(declaration, dict):
            diagnostics.append(
                Diagnostic(
                    "LLMF202",
                    Severity.ERROR,
                    f"--var names undeclared variable {name!r}",
                    document.position(("variables",)),
                )
            )
            continue
        try:
            variables[name] = _parse_cli_variable(value, declaration.get("type", "string"))
        except ValueError:
            declared_type = declaration.get("type", "string")
            diagnostics.append(
                Diagnostic(
                    "LLMF203",
                    Severity.ERROR,
                    f"--var {name!r} does not match declared type {declared_type}",
                    document.position(("variables", name, "type")),
                )
            )

    for name, declaration in declarations.items():
        if isinstance(declaration, dict) and declaration.get("required") and name not in variables:
            diagnostics.append(
                Diagnostic(
                    "LLMF200",
                    Severity.ERROR,
                    f"required variable {name!r} has no value; pass --var {name}=VALUE",
                    document.position(("variables", name)),
                )
            )

    def visit(value: Any, path: PathKey) -> Any:
        if isinstance(value, dict):
            return {key: visit(item, path + (key,)) for key, item in value.items()}
        if isinstance(value, list):
            return [visit(item, path + (index,)) for index, item in enumerate(value)]
        if not isinstance(value, str):
            return value
        match = INTERPOLATION.fullmatch(value)
        if not match:
            if "${" in value:
                diagnostics.append(
                    Diagnostic(
                        "LLMF201",
                        Severity.ERROR,
                        "interpolation must occupy the entire scalar value",
                        document.position(path),
                    )
                )
            return value
        namespace, name = match.groups()
        resolved = secret_provider.get(name) if namespace == "secret" else None
        source = variables if namespace == "var" else environ
        if namespace != "secret" and name in source:
            resolved = source[name]
        if resolved is None:
            diagnostics.append(
                Diagnostic(
                    "LLMF200",
                    Severity.ERROR,
                    f"unresolved interpolation ${{{namespace}.{name}}}: value is not defined",
                    document.position(path),
                )
            )
            return value
        if namespace == "secret":
            scrubber.register(str(resolved))
        return resolved

    return visit(document.raw, ()), diagnostics, scrubber
