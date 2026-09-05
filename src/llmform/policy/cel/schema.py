"""Compile the supported JSON Schema profile into concrete CEL types."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from llmform.diagnostics import Diagnostic, Position, Severity
from llmform.policy.cel.types import BOOL, DOUBLE, INT, STRING, CelKind, CelType, list_of

_UNSUPPORTED = {
    "allOf",
    "anyOf",
    "not",
    "oneOf",
    "patternProperties",
    "prefixItems",
    "unevaluatedProperties",
}
_ANNOTATIONS = {"$comment", "$id", "$schema", "default", "description", "examples", "title"}
_VALIDATION = {
    "const",
    "deprecated",
    "format",
    "maxItems",
    "maxLength",
    "maximum",
    "minItems",
    "minLength",
    "minimum",
    "multipleOf",
    "pattern",
    "readOnly",
    "uniqueItems",
    "writeOnly",
}


@dataclass(frozen=True)
class CompiledSchema:
    type: CelType | None
    diagnostics: list[Diagnostic]
    required_fields: frozenset[str] = frozenset()


class SchemaCompiler:
    """Compile one schema document, resolving only acyclic local ``$defs`` refs."""

    def __init__(self, schema: object, source: Path):
        self.schema = schema
        self.source = source
        self.diagnostics: list[Diagnostic] = []
        self._memo: dict[str, CelType] = {}
        self._attempted_refs: set[str] = set()
        self._active_refs: list[str] = []

    def compile(self) -> CompiledSchema:
        if not isinstance(self.schema, Mapping):
            self._error((), "schema must be a JSON object")
            return CompiledSchema(None, self.diagnostics)
        result = self._compile_node(self.schema, ())
        definitions = self.schema.get("$defs", {})
        if isinstance(definitions, Mapping):
            for name in definitions:
                self._compile_ref(f"#/$defs/{name}", ("$defs", str(name)))
        required = self.schema.get("required", [])
        required_fields = (
            frozenset(required)
            if isinstance(required, list) and all(isinstance(item, str) for item in required)
            else frozenset()
        )
        return CompiledSchema(result, self.diagnostics, required_fields)

    def _compile_node(self, node: object, path: tuple[str | int, ...]) -> CelType | None:
        if not isinstance(node, Mapping):
            self._error(path, "schema must be an object")
            return None

        for keyword in sorted(_UNSUPPORTED.intersection(node)):
            self._error(path + (keyword,), f"unsupported JSON Schema keyword {keyword!r}")

        if "$ref" in node:
            siblings = set(node) - {"$defs", "$ref"} - _ANNOTATIONS
            if siblings:
                self._error(path, "$ref may not have type-affecting sibling keywords")
            return self._compile_ref(node["$ref"], path + ("$ref",))

        declared_type = node.get("type")
        if isinstance(declared_type, list):
            self._error(path + ("type",), "nullable or union type arrays are unsupported")
            return None
        if declared_type not in {"object", "array", "string", "number", "integer", "boolean"}:
            self._error(path + ("type",), f"unsupported or missing schema type {declared_type!r}")
            return None

        allowed = _ANNOTATIONS | _VALIDATION | {"$defs", "enum", "type"}
        if declared_type == "object":
            allowed |= {"additionalProperties", "properties", "required"}
        elif declared_type == "array":
            allowed |= {"items"}
        for keyword in node:
            if keyword not in allowed and keyword not in _UNSUPPORTED:
                self._error(path + (keyword,), f"unsupported JSON Schema keyword {keyword!r}")

        self._validate_defs(node.get("$defs"), path)
        if declared_type == "object":
            return self._compile_object(node, path)
        if declared_type == "array":
            return self._compile_array(node, path)
        if declared_type == "string":
            return self._compile_string(node, path)
        return {"number": DOUBLE, "integer": INT, "boolean": BOOL}[declared_type]

    def _compile_object(self, node: Mapping[str, Any], path: tuple[str | int, ...]) -> CelType:
        if node.get("additionalProperties") is not False:
            self._error(
                path + ("additionalProperties",),
                "object schemas must declare additionalProperties: false",
            )
        properties = node.get("properties", {})
        if not isinstance(properties, Mapping):
            self._error(path + ("properties",), "properties must be an object")
            properties = {}
        required = node.get("required", [])
        if not isinstance(required, list) or not all(isinstance(item, str) for item in required):
            self._error(path + ("required",), "required must be an array of property names")
            required = []
        for name in required:
            if name not in properties:
                self._error(path + ("required",), f"required property {name!r} is not declared")
        fields: dict[str, CelType] = {}
        for name, child in properties.items():
            child_type = self._compile_node(child, path + ("properties", str(name)))
            if child_type is not None:
                fields[str(name)] = child_type
        return CelType(CelKind.OBJECT, fields=fields)

    def _compile_array(
        self, node: Mapping[str, Any], path: tuple[str | int, ...]
    ) -> CelType | None:
        items = node.get("items")
        if isinstance(items, list):
            self._error(path + ("items",), "tuple-form array items are unsupported")
            return None
        if items is None:
            self._error(path + ("items",), "array schemas require a homogeneous items schema")
            return None
        item_type = self._compile_node(items, path + ("items",))
        return list_of(item_type) if item_type is not None else None

    def _compile_string(self, node: Mapping[str, Any], path: tuple[str | int, ...]) -> CelType:
        enum = node.get("enum")
        if enum is None:
            return STRING
        if (
            not isinstance(enum, list)
            or not enum
            or not all(isinstance(item, str) for item in enum)
        ):
            self._error(path + ("enum",), "enum must be a non-empty array of strings")
            return STRING
        return CelType(CelKind.STRING, enum=tuple(enum))

    def _validate_defs(self, definitions: object, path: tuple[str | int, ...]) -> None:
        if definitions is None:
            return
        if not isinstance(definitions, Mapping):
            self._error(path + ("$defs",), "$defs must be an object")

    def _compile_ref(self, reference: object, path: tuple[str | int, ...]) -> CelType | None:
        if not isinstance(reference, str) or not reference.startswith("#/$defs/"):
            self._error(path, "$ref must point to this document's $defs")
            return None
        name = reference.removeprefix("#/$defs/")
        if not name or "/" in name:
            self._error(path, "$ref must name one direct member of $defs")
            return None
        if reference in self._active_refs:
            cycle = " -> ".join([*self._active_refs, reference])
            self._error(path, f"cyclic $ref is unsupported: {cycle}")
            return None
        if reference in self._memo:
            return self._memo[reference]
        if reference in self._attempted_refs:
            return None
        self._attempted_refs.add(reference)
        definitions = self.schema.get("$defs", {}) if isinstance(self.schema, Mapping) else {}
        if not isinstance(definitions, Mapping) or name not in definitions:
            self._error(path, f"unresolved local $ref {reference!r}")
            return None
        self._active_refs.append(reference)
        result = self._compile_node(definitions[name], ("$defs", name))
        self._active_refs.pop()
        if result is not None:
            self._memo[reference] = result
        return result

    def _error(self, path: tuple[str | int, ...], message: str) -> None:
        location = "/".join(str(part) for part in path) or "<root>"
        self.diagnostics.append(
            Diagnostic(
                "LLMF400",
                Severity.ERROR,
                message,
                Position(self.source),
                f"schema path: {location}",
            )
        )


def compile_schema_file(path: Path) -> CompiledSchema:
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        return CompiledSchema(
            None,
            [Diagnostic("LLMF401", Severity.ERROR, str(exc), Position(path))],
        )
    except json.JSONDecodeError as exc:
        return CompiledSchema(
            None,
            [
                Diagnostic(
                    "LLMF401",
                    Severity.ERROR,
                    f"invalid JSON Schema: {exc.msg}",
                    Position(path, exc.lineno, exc.colno),
                )
            ],
        )
    return SchemaCompiler(schema, path).compile()
