from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

from llmform.config.discovery import discover_files, find_project_root
from llmform.config.models import ProjectConfig
from llmform.diagnostics import Diagnostic, Position, Severity

PathKey = tuple[str | int, ...]


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
class ConfigDocument:
    root: Path
    files: list[Path]
    raw: dict[str, Any]
    positions: dict[PathKey, Position]
    config: ProjectConfig | None
    diagnostics: list[Diagnostic]

    def position(self, path: PathKey) -> Position:
        probe = path
        while probe:
            if probe in self.positions:
                return self.positions[probe]
            probe = probe[:-1]
        return Position(self.files[0] if self.files else self.root)


def _scalar_key(node: Node) -> str:
    if not isinstance(node, ScalarNode):
        raise TypeError("mapping keys must be scalar values")
    return node.value


def _collect_positions(
    node: Node,
    file: Path,
    positions: dict[PathKey, Position],
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
            positions[path + (key,)] = Position(
                file, value_node.start_mark.line + 1, value_node.start_mark.column + 1
            )
            _collect_positions(value_node, file, positions, diagnostics, path + (key,))
    elif isinstance(node, SequenceNode):
        for index, item in enumerate(node.value):
            _collect_positions(item, file, positions, diagnostics, path + (index,))


def _merge(
    target: dict[str, Any],
    incoming: dict[str, Any],
    file: Path,
    source_positions: dict[PathKey, Position],
    positions: dict[PathKey, Position],
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
                    first = positions.get(path)
                    diagnostics.append(
                        Diagnostic(
                            "LLMF003",
                            Severity.ERROR,
                            f"duplicate resource {key[:-1]}.{name}",
                            source_positions.get(path, Position(file)),
                            f"first declared at {first.display() if first else 'an earlier file'}",
                        )
                    )
                    continue
                block[name] = resource
        elif key in target:
            rejected_prefixes.append((key,))
            first = positions.get((key,))
            diagnostics.append(
                Diagnostic(
                    "LLMF004",
                    Severity.ERROR,
                    f"top-level key {key!r} may be declared only once",
                    source_positions.get((key,), Position(file)),
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


def load_project(start: Path) -> ConfigDocument:
    root = find_project_root(start)
    if root is None:
        position = Position(start.resolve())
        return ConfigDocument(
            start.resolve(),
            [],
            {},
            {},
            None,
            [Diagnostic("LLMF000", Severity.ERROR, "no llmform configuration found", position)],
        )

    files = discover_files(root)
    raw: dict[str, Any] = {}
    positions: dict[PathKey, Position] = {}
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
        if node is None:
            loaded = {}
            continue
        local_positions: dict[PathKey, Position] = {}
        _collect_positions(node, file, local_positions, diagnostics)
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
        _merge(raw, loaded, file, local_positions, positions, diagnostics)

    config: ProjectConfig | None = None
    if not any(item.severity == Severity.ERROR for item in diagnostics):
        try:
            config = ProjectConfig.model_validate(raw)
        except ValidationError as exc:
            for error in exc.errors(include_url=False):
                path = tuple(error["loc"])
                diagnostics.append(
                    Diagnostic(
                        "LLMF100",
                        Severity.ERROR,
                        error["msg"],
                        positions.get(path, positions.get(path[:-1], Position(files[0]))),
                    )
                )
    return ConfigDocument(root, files, raw, positions, config, diagnostics)
