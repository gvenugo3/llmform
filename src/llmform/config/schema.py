"""L0 validation against the packaged configuration JSON Schema."""

from __future__ import annotations

import json
from importlib.resources import files
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from llmform.config.loader import PathKey
from llmform.diagnostics import Diagnostic, Position, Severity
from llmform.schemas import CONFIG_SCHEMA_VERSION


def _project_schema() -> dict[str, Any]:
    resource = files("llmform.schemas").joinpath("project.json")
    return json.loads(resource.read_text(encoding="utf-8"))


def _unexpected_properties(error: ValidationError) -> list[str]:
    if error.validator != "additionalProperties" or not isinstance(error.instance, dict):
        return []
    allowed = set(error.schema.get("properties", {}))
    return sorted(set(error.instance) - allowed)


def validate_config_schema(
    raw: object,
    positions: dict[PathKey, Position],
    key_positions: dict[PathKey, Position],
    fallback: Position,
) -> list[Diagnostic]:
    """Return positioned L0 findings without raising raw jsonschema errors."""

    schema = _project_schema()
    if schema.get("x-llmform-schema-version") != CONFIG_SCHEMA_VERSION:
        return [
            Diagnostic(
                "LLMF101",
                Severity.ERROR,
                "packaged configuration schema version does not match the runtime",
                fallback,
            )
        ]
    validator = Draft202012Validator(schema)
    diagnostics: list[Diagnostic] = []
    errors = sorted(
        validator.iter_errors(raw),
        key=lambda item: tuple(str(part) for part in item.absolute_path),
    )
    for error in errors:
        path = tuple(error.absolute_path)
        unexpected = _unexpected_properties(error)
        if unexpected:
            for name in unexpected:
                item_path = path + (name,)
                diagnostics.append(
                    Diagnostic(
                        "LLMF100",
                        Severity.ERROR,
                        f"unknown field {name!r}",
                        key_positions.get(item_path, positions.get(item_path, fallback)),
                    )
                )
            continue
        diagnostics.append(
            Diagnostic(
                "LLMF100",
                Severity.ERROR,
                error.message,
                positions.get(path, positions.get(path[:-1], fallback)),
                f"schema path: {'/'.join(str(part) for part in error.absolute_schema_path)}",
            )
        )
    return diagnostics
