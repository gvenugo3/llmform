"""Canonical, offline lockfile construction for an llmform project."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

from llmform import __version__
from llmform.config.loader import ConfigDocument, LlmformLoader
from llmform.schemas import CONFIG_SCHEMA_VERSION

LOCK_VERSION = 1
LOCK_NAME = "llmform.lock"


def _digest(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _config_digest(path: Path) -> str:
    """Hash parsed YAML so comments, spelling, and mapping order do not cause drift."""

    parsed = yaml.load(path.read_text(encoding="utf-8"), Loader=LlmformLoader)
    return _digest(_canonical_json(parsed))


def _project_path(root: Path, reference: str) -> Path:
    path = Path(reference)
    return path if path.is_absolute() else root / path


def _closure_files(document: ConfigDocument) -> dict[str, str]:
    assert document.config is not None
    config_files = set(document.files)
    referenced: set[Path] = set()
    for agent in document.config.agents.values():
        referenced.add(_project_path(document.root, agent.instructions))
        referenced.update(
            _project_path(document.root, item) for item in (agent.input, agent.output) if item
        )
    for source in document.config.sources.values():
        referenced.update(
            _project_path(document.root, operation.returns)
            for operation in source.operations.values()
        )
    for tool in document.config.tools.values():
        referenced.add(_project_path(document.root, tool.input))
        if tool.output:
            referenced.add(_project_path(document.root, tool.output))

    hashes: dict[str, str] = {}
    for path in sorted(config_files | referenced):
        relative = path.relative_to(document.root).as_posix()
        hashes[relative] = (
            _config_digest(path) if path in config_files else _digest(path.read_bytes())
        )
    return hashes


def build_lock(document: ConfigDocument) -> dict[str, Any]:
    """Build the deterministic lock payload for an already validated project."""

    if document.config is None:
        raise ValueError("cannot lock an invalid project")
    config = document.config
    sources: dict[str, object] = {}
    for name, source in config.sources.items():
        catalog = {
            "type": source.type,
            "operations": {
                operation_name: operation.model_dump(mode="json")
                for operation_name, operation in source.operations.items()
            },
        }
        if source.type == "mcp":
            catalog["command_sha256"] = _digest(_canonical_json(source.command))
        sources[name] = catalog
    payload: dict[str, Any] = {
        "lock_version": LOCK_VERSION,
        "config_schema_version": CONFIG_SCHEMA_VERSION,
        "llmform_version": ".".join(__version__.split(".")[:2]),
        "files": _closure_files(document),
        "models": {
            name: {
                "id": model.id,
                "price": model.price.model_dump(mode="json") if model.price else None,
            }
            for name, model in config.models.items()
        },
        "policies": {
            name: policy.model_dump(mode="json") for name, policy in config.policies.items()
        },
        "policy_attachments": {name: agent.policies for name, agent in config.agents.items()},
        "sources": sources,
    }
    normalized = json.loads(_canonical_json(payload))
    normalized["closure_sha256"] = _digest(_canonical_json(payload))
    return normalized


def write_lock(document: ConfigDocument) -> Path:
    """Write the project's canonical lockfile and return its path."""

    path = document.root / LOCK_NAME
    path.write_text(json.dumps(build_lock(document), indent=2) + "\n", encoding="utf-8")
    return path


def read_lock(root: Path) -> dict[str, Any]:
    """Read one lockfile without accepting an unstructured payload."""

    value = json.loads((root / LOCK_NAME).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("lockfile root must be an object")
    return value


def diff_lock(document: ConfigDocument) -> tuple[str, ...]:
    """Return top-level lock sections that differ from the current project closure."""

    recorded = read_lock(document.root)
    current = build_lock(document)
    changed = (key for key in set(recorded) | set(current) if recorded.get(key) != current.get(key))
    return tuple(sorted(changed))
