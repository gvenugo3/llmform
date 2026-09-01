from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from llmform.config.loader import ConfigDocument, PathKey
from llmform.diagnostics import Diagnostic, Severity

INTERPOLATION = re.compile(r"^\$\{(env|var|secret)\.([A-Za-z_][A-Za-z0-9_]*)\}$")


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


def resolve_interpolations(
    document: ConfigDocument,
    cli_variables: Mapping[str, str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> tuple[dict[str, Any], list[Diagnostic], SecretScrubber]:
    environ = os.environ if environ is None else environ
    variables: dict[str, Any] = {}
    for name, declaration in document.raw.get("variables", {}).items():
        if isinstance(declaration, dict) and declaration.get("default") is not None:
            variables[name] = declaration["default"]
    variables.update(cli_variables or {})
    diagnostics: list[Diagnostic] = []
    scrubber = SecretScrubber()

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
        source = variables if namespace == "var" else environ
        if name not in source:
            diagnostics.append(
                Diagnostic(
                    "LLMF200",
                    Severity.ERROR,
                    f"unresolved {namespace} value {name!r}",
                    document.position(path),
                )
            )
            return value
        resolved = source[name]
        if namespace == "secret":
            scrubber.register(str(resolved))
        return resolved

    return visit(document.raw, ()), diagnostics, scrubber
