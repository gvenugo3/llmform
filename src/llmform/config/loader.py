from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

from llmform.config.discovery import discover_files, find_project_root
from llmform.config.models import FieldLocation, ProjectConfig, StrictModel
from llmform.diagnostics import Diagnostic, Position, Severity

PathKey = tuple[str | int, ...]
INTERPOLATION_VALUE = re.compile(r"^\$\{(?:env|var|secret)\.[A-Za-z_][A-Za-z0-9_]*\}$")


class LlmformLoader(yaml.SafeLoader):
    """Safe YAML loader with YAML 1.2 boolean semantics.

    PyYAML defaults to YAML 1.1, where keys such as ``on`` and ``off`` are booleans.
    Those spellings are ordinary strings in YAML 1.2 and ``on`` is part of our policy
    grammar.
    """


LlmformLoader.yaml_implicit_resolvers = {
    key: list(resolvers) for key, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
for first_character, resolvers in LlmformLoader.yaml_implicit_resolvers.items():
    LlmformLoader.yaml_implicit_resolvers[first_character] = [
        resolver for resolver in resolvers if resolver[0] != "tag:yaml.org,2002:bool"
    ]
LlmformLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool", re.compile(r"^(?:true|false)$", re.IGNORECASE), list("tTfF")
)


@dataclass
class YamlSource:
    """One parsed source file, retaining text for comment-preserving round trips."""

    file: Path
    text: str
    node: Node | None

    def render(self) -> str:
        return self.text


@dataclass
class ConfigDocument:
    root: Path
    files: list[Path]
    sources: list[YamlSource]
    raw: dict[str, Any]
    positions: dict[PathKey, Position]
    key_positions: dict[PathKey, Position]
    config: ProjectConfig | None
    diagnostics: list[Diagnostic]

    def position(self, path: PathKey) -> Position:
        probe = path
        while probe:
            if probe in self.positions:
                return self.positions[probe]
            probe = probe[:-1]
        return Position(self.files[0] if self.files else self.root)

    def key_position(self, path: PathKey) -> Position:
        if path in self.key_positions:
            return self.key_positions[path]
        return self.position(path)


def _attach_model_positions(
    value: object,
    path: PathKey,
    positions: dict[PathKey, Position],
    key_positions: dict[PathKey, Position],
) -> None:
    if isinstance(value, StrictModel):
        for name in type(value).model_fields:
            field_path = path + (name,)
            value_position = positions.get(field_path)
            if value_position is not None:
                value._set_field_location(
                    name,
                    FieldLocation(key_positions.get(field_path, value_position), value_position),
                )
            _attach_model_positions(getattr(value, name), field_path, positions, key_positions)
    elif isinstance(value, dict):
        for name, item in value.items():
            _attach_model_positions(item, path + (name,), positions, key_positions)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _attach_model_positions(item, path + (index,), positions, key_positions)


def attach_model_positions(config: ProjectConfig, document: ConfigDocument) -> None:
    """Attach a document's source map to a freshly validated typed model tree."""

    _attach_model_positions(config, (), document.positions, document.key_positions)


def _scalar_key(node: Node) -> str:
    if not isinstance(node, ScalarNode):
        raise TypeError("mapping keys must be scalar values")
    return node.value


def _collect_positions(
    node: Node,
    file: Path,
    positions: dict[PathKey, Position],
    key_positions: dict[PathKey, Position],
    diagnostics: list[Diagnostic],
    path: PathKey = (),
) -> None:
    positions[path] = Position(file, node.start_mark.line + 1, node.start_mark.column + 1)
    if isinstance(node, MappingNode):
        seen: dict[str, Node] = {}
        for key_node, value_node in node.value:
            try:
                key = _scalar_key(key_node)
            except TypeError:
                diagnostics.append(
                    Diagnostic(
                        "LLMF001",
                        Severity.ERROR,
                        "mapping keys must be scalar values",
                        Position(
                            file,
                            key_node.start_mark.line + 1,
                            key_node.start_mark.column + 1,
                        ),
                    )
                )
                continue
            if key in seen:
                first = seen[key]
                diagnostics.append(
                    Diagnostic(
                        "LLMF002",
                        Severity.ERROR,
                        f"duplicate key {key!r}",
                        Position(
                            file,
                            key_node.start_mark.line + 1,
                            key_node.start_mark.column + 1,
                        ),
                        (
                            f"first declared at {file}:{first.start_mark.line + 1}:"
                            f"{first.start_mark.column + 1}"
                        ),
                    )
                )
            else:
                seen[key] = key_node
            key_positions[path + (key,)] = Position(
                file, key_node.start_mark.line + 1, key_node.start_mark.column + 1
            )
            positions[path + (key,)] = Position(
                file, value_node.start_mark.line + 1, value_node.start_mark.column + 1
            )
            _collect_positions(
                value_node,
                file,
                positions,
                key_positions,
                diagnostics,
                path + (key,),
            )
    elif isinstance(node, SequenceNode):
        for index, item in enumerate(node.value):
            _collect_positions(item, file, positions, key_positions, diagnostics, path + (index,))


def _merge(
    target: dict[str, Any],
    incoming: dict[str, Any],
    file: Path,
    source_positions: dict[PathKey, Position],
    positions: dict[PathKey, Position],
    source_key_positions: dict[PathKey, Position],
    key_positions: dict[PathKey, Position],
    diagnostics: list[Diagnostic],
) -> None:
    resource_blocks = {"variables", "providers", "models", "sources", "tools", "policies", "agents"}
    rejected_prefixes: list[PathKey] = []
    for key, value in incoming.items():
        if key in resource_blocks and isinstance(value, dict):
            block = target.setdefault(key, {})
            if not isinstance(block, dict):
                continue
            for name, resource in value.items():
                path = (key, name)
                if name in block:
                    rejected_prefixes.append(path)
                    first = key_positions.get(path, positions.get(path))
                    diagnostics.append(
                        Diagnostic(
                            "LLMF003",
                            Severity.ERROR,
                            f"duplicate resource {key[:-1]}.{name}",
                            source_key_positions.get(
                                path, source_positions.get(path, Position(file))
                            ),
                            f"first declared at {first.display() if first else 'an earlier file'}",
                        )
                    )
                    continue
                block[name] = resource
        elif key in target:
            rejected_prefixes.append((key,))
            first = key_positions.get((key,), positions.get((key,)))
            diagnostics.append(
                Diagnostic(
                    "LLMF004",
                    Severity.ERROR,
                    f"top-level key {key!r} may be declared only once",
                    source_key_positions.get((key,), source_positions.get((key,), Position(file))),
                    f"first declared at {first.display() if first else 'an earlier file'}",
                )
            )
        else:
            target[key] = value
    for path, position in source_positions.items():
        if any(path[: len(prefix)] == prefix for prefix in rejected_prefixes):
            continue
        # Shared block/root positions belong to the first file; positions for a newly
        # accepted resource or field have no existing entry and are added normally.
        positions.setdefault(path, position)
    for path, position in source_key_positions.items():
        if any(path[: len(prefix)] == prefix for prefix in rejected_prefixes):
            continue
        key_positions.setdefault(path, position)


def _contains_interpolation(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_contains_interpolation(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_interpolation(item) for item in value)
    return isinstance(value, str) and INTERPOLATION_VALUE.fullmatch(value) is not None


def load_project(start: Path) -> ConfigDocument:
    root = find_project_root(start)
    if root is None:
        position = Position(start.resolve())
        return ConfigDocument(
            start.resolve(),
            [],
            [],
            {},
            {},
            {},
            None,
            [Diagnostic("LLMF000", Severity.ERROR, "no llmform configuration found", position)],
        )

    files = discover_files(root)
    raw: dict[str, Any] = {}
    sources: list[YamlSource] = []
    positions: dict[PathKey, Position] = {}
    key_positions: dict[PathKey, Position] = {}
    diagnostics: list[Diagnostic] = []
    for file in files:
        try:
            text = file.read_text(encoding="utf-8")
            node = yaml.compose(text, Loader=LlmformLoader)
            loaded = yaml.load(text, Loader=LlmformLoader)
        except (OSError, yaml.YAMLError) as exc:
            mark = getattr(exc, "problem_mark", None)
            diagnostics.append(
                Diagnostic(
                    "LLMF001",
                    Severity.ERROR,
                    str(exc),
                    Position(file, getattr(mark, "line", 0) + 1, getattr(mark, "column", 0) + 1),
                )
            )
            continue
        sources.append(YamlSource(file, text, node))
        if node is None:
            loaded = {}
            continue
        local_positions: dict[PathKey, Position] = {}
        local_key_positions: dict[PathKey, Position] = {}
        _collect_positions(node, file, local_positions, local_key_positions, diagnostics)
        if not isinstance(loaded, dict):
            diagnostics.append(
                Diagnostic(
                    "LLMF005",
                    Severity.ERROR,
                    "configuration root must be a mapping",
                    Position(file),
                )
            )
            continue
        _merge(
            raw,
            loaded,
            file,
            local_positions,
            positions,
            local_key_positions,
            key_positions,
            diagnostics,
        )

    config: ProjectConfig | None = None
    pending_interpolation = _contains_interpolation(raw)
    if not pending_interpolation and not any(
        item.severity == Severity.ERROR for item in diagnostics
    ):
        # Imported lazily to avoid a loader/schema import cycle around PathKey.
        from llmform.config.schema import validate_config_schema

        diagnostics.extend(
            validate_config_schema(
                raw,
                positions,
                key_positions,
                Position(files[0]),
            )
        )
    if not pending_interpolation and not any(
        item.severity == Severity.ERROR for item in diagnostics
    ):
        try:
            config = ProjectConfig.model_validate(raw)
        except ValidationError as exc:
            for error in exc.errors(include_url=False):
                path = tuple(error["loc"])
                position = (
                    key_positions.get(path)
                    if error["type"] == "extra_forbidden"
                    else positions.get(path)
                )
                diagnostics.append(
                    Diagnostic(
                        "LLMF100",
                        Severity.ERROR,
                        error["msg"],
                        position or positions.get(path[:-1], Position(files[0])),
                    )
                )
    document = ConfigDocument(
        root,
        files,
        sources,
        raw,
        positions,
        key_positions,
        config,
        diagnostics,
    )
    if config is not None:
        attach_model_positions(config, document)
    return document
