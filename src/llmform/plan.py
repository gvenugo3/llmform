"""Deterministic configuration planning against a committed project closure."""

from __future__ import annotations

import json
import subprocess
import tarfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal

from llmform.config.loader import ConfigDocument
from llmform.config.models import ProjectConfig
from llmform.lock import build_lock

Classification = Literal["cosmetic", "behavioral", "privilege"]
_MISSING = object()


class PlanError(ValueError):
    """Raised when a committed project snapshot cannot be loaded safely."""


@dataclass(frozen=True)
class PlanChange:
    classification: Classification
    path: str
    action: Literal["added", "removed", "changed"]


def _git(
    repository: Path, *arguments: str, binary: bool = False
) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
    try:
        result = subprocess.run(
            ["git", "-C", str(repository), *arguments],
            check=True,
            capture_output=True,
            text=not binary,
            timeout=30,
        )
    except FileNotFoundError as exc:
        raise PlanError("git executable was not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise PlanError("git command timed out while loading the baseline") from exc
    except subprocess.CalledProcessError as exc:
        stderr = (
            exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else exc.stderr
        )
        raise PlanError(f"git command failed: {(stderr or '').strip()}") from exc
    return result


@contextmanager
def project_at_ref(project_root: Path, ref: str):
    """Yield a safely extracted, tracked project snapshot at ``ref``."""

    project_root = project_root.resolve()
    repository = Path(_git(project_root, "rev-parse", "--show-toplevel").stdout.strip())
    relative_root = project_root.relative_to(repository)
    try:
        resolved_ref = _git(
            repository,
            "rev-parse",
            "--verify",
            "--quiet",
            "--end-of-options",
            f"{ref}^{{tree}}",
        ).stdout.strip()
    except PlanError as exc:
        raise PlanError(f"baseline Git ref {ref!r} is not valid or unavailable") from exc
    archive_arguments = ["archive", "--format=tar", resolved_ref]
    if relative_root != Path("."):
        archive_arguments.extend(["--", relative_root.as_posix()])
    archive = _git(repository, *archive_arguments, binary=True).stdout

    with TemporaryDirectory(prefix="llmform-plan-") as temporary:
        snapshot = Path(temporary) / relative_root
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        try:
            with tarfile.open(fileobj=BytesIO(archive), mode="r:") as bundle:
                bundle.extractall(Path(temporary), filter="data")
        except (OSError, tarfile.TarError) as exc:
            raise PlanError(f"could not extract the baseline archive: {exc}") from exc
        if not snapshot.is_dir():
            raise PlanError(f"project directory is not present at baseline ref {ref!r}")
        yield snapshot


def _path_value(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _referenced_files(document: ConfigDocument) -> dict[str, set[str]]:
    assert document.config is not None
    references: dict[str, set[str]] = {}

    def add(reference: str | None, role: str) -> None:
        if reference:
            path = Path(reference)
            source = path if path.is_absolute() else document.root / path
            relative = _path_value(source, document.root)
            references.setdefault(relative, set()).add(role)

    for agent in document.config.agents.values():
        add(agent.instructions, "prompt")
        add(agent.input, "schema")
        add(agent.output, "schema")
    for tool in document.config.tools.values():
        add(tool.input, "tool_schema")
        add(tool.output, "tool_schema")
    for source in document.config.sources.values():
        for operation in source.operations.values():
            add(operation.returns, "schema")
    return references


def _config_files(document: ConfigDocument) -> dict[str, bytes]:
    return {
        path.relative_to(document.root).as_posix(): path.read_bytes() for path in document.files
    }


def _scalar_list(value: list[object]) -> bool:
    return all(item is None or isinstance(item, (str, int, float, bool)) for item in value)


def _sort_key(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _display_path(path: tuple[str, ...]) -> str:
    result = ""
    for segment in path:
        if segment.startswith("["):
            result += segment
        else:
            result += f".{segment}" if result else segment
    return result


def _differences(before: object, after: object, path: tuple[str, ...] = ()):
    if isinstance(before, dict) and isinstance(after, dict):
        for key in sorted(before.keys() | after.keys(), key=str):
            if key not in before:
                yield (*path, str(key)), _MISSING, after[key]
            elif key not in after:
                yield (*path, str(key)), before[key], _MISSING
            else:
                yield from _differences(before[key], after[key], (*path, str(key)))
    elif isinstance(before, list) and isinstance(after, list) and _scalar_list(before + after):
        before_values = {_sort_key(item): item for item in before}
        after_values = {_sort_key(item): item for item in after}
        for key in sorted(before_values.keys() - after_values.keys()):
            yield (*path, f"[{before_values[key]}]"), before_values[key], _MISSING
        for key in sorted(after_values.keys() - before_values.keys()):
            yield (*path, f"[{after_values[key]}]"), _MISSING, after_values[key]
    elif before != after:
        yield path, before, after


def _action(before: object, after: object) -> Literal["added", "removed", "changed"]:
    if before is _MISSING:
        return "added"
    if after is _MISSING:
        return "removed"
    return "changed"


def _increased(before: object, after: object) -> bool:
    if before is _MISSING:
        return False
    if after is _MISSING or after is None:
        return before is not None
    if before is None:
        return False
    return isinstance(before, (int, float)) and isinstance(after, (int, float)) and after > before


def _classification(
    path: tuple[str, ...],
    before: object,
    after: object,
    before_config: ProjectConfig,
    after_config: ProjectConfig,
) -> Classification:
    if path and path[-1] == "description":
        return "cosmetic"
    action = _action(before, after)

    if len(path) >= 3 and path[0] == "agents":
        field = path[2]
        if (field == "tools" and action == "added") or (
            field == "policies" and action == "removed"
        ):
            return "privilege"
        if field == "default_tool_posture" and after == "allow":
            return "privilege"
        if field in {"max_cost_usd", "max_iterations"} and _increased(before, after):
            return "privilege"
        if field == "max_elapsed":
            return "privilege"
        if field == "model" and isinstance(before, str) and isinstance(after, str):
            old_name = before.partition(".")[2]
            new_name = after.partition(".")[2]
            old_model = before_config.models.get(old_name)
            new_model = after_config.models.get(new_name)
            if old_model and new_model and old_model.provider != new_model.provider:
                return "privilege"

    if len(path) >= 3 and path[0] == "tools" and path[2] == "detokenize":
        if action == "added":
            return "privilege"
    if len(path) >= 3 and path[0] == "tools" and path[2] in {"source", "operation"}:
        return "privilege"

    if (
        len(path) >= 3
        and path[0] == "providers"
        and path[2]
        in {
            "type",
            "endpoint",
            "api_key",
        }
    ):
        return "privilege"

    if len(path) >= 3 and path[0] == "policies":
        if path[2] in {"rule", "match", "otherwise", "approval", "transform"}:
            return "privilege"
        if action == "removed":
            return "privilege"

    if len(path) >= 5 and path[0] == "sources" and path[-1].startswith("["):
        if path[-2] == "classes" and action == "removed":
            return "privilege"

    if len(path) >= 3 and path[0] == "sources" and path[2] == "command":
        source_names = (path[1],)
        is_mcp = any(
            (source := sources.get(name)) is not None and source.type == "mcp"
            for sources in (before_config.sources, after_config.sources)
            for name in source_names
        )
        if is_mcp:
            return "privilege"
    if (
        len(path) >= 3
        and path[0] == "sources"
        and path[2]
        in {
            "type",
            "base_url",
            "auth",
        }
    ):
        return "privilege"

    if len(path) >= 3 and path[0] == "models":
        field = path[2]
        if field == "provider":
            return "privilege"
        if field == "max_tokens" and _increased(before, after):
            return "privilege"
        if field == "price" and len(path) >= 4:
            if path[3] == "currency":
                return "privilege"
            if path[3] in {"input_per_mtok", "output_per_mtok"} and _increased(before, after):
                return "privilege"

    return "behavioral"


def _format_number(value: float) -> str:
    return f"{value:.6g}"


def _cost_impact(before: ProjectConfig, after: ProjectConfig) -> list[str]:
    lines: list[str] = []
    model_names = sorted(before.models.keys() | after.models.keys())
    for name in model_names:
        old = before.models.get(name)
        new = after.models.get(name)
        old_price = old.price if old else None
        new_price = new.price if new else None
        if old_price == new_price:
            continue
        if old_price is None or new_price is None:
            if new_price is None:
                lines.append(f"model.{name}: declared price removed; cost impact is unknown")
            else:
                lines.append(
                    f"model.{name}: declared price added "
                    f"({new_price.currency} input {_format_number(new_price.input_per_mtok)}, "
                    f"output {_format_number(new_price.output_per_mtok)} per million tokens)"
                )
            continue
        if old_price.currency != new_price.currency:
            lines.append(
                f"model.{name}: price currency changed {old_price.currency} -> "
                f"{new_price.currency}; no exchange-rate conversion is applied"
            )
            continue
        for token_kind in ("input", "output"):
            old_rate = getattr(old_price, f"{token_kind}_per_mtok")
            new_rate = getattr(new_price, f"{token_kind}_per_mtok")
            if old_rate != new_rate:
                delta = new_rate - old_rate
                sign = "+" if delta > 0 else ""
                lines.append(
                    f"model.{name} {token_kind}: {old_price.currency} "
                    f"{_format_number(old_rate)} -> {_format_number(new_rate)} per "
                    f"million tokens ({sign}{_format_number(delta)})"
                )

    for name in sorted(before.agents.keys() | after.agents.keys()):
        old = before.agents.get(name)
        new = after.agents.get(name)
        old_limit = old.max_cost_usd if old else None
        new_limit = new.max_cost_usd if new else None
        if old_limit != new_limit:
            old_text = "unlimited" if old_limit is None else f"${_format_number(old_limit)}"
            new_text = "unlimited" if new_limit is None else f"${_format_number(new_limit)}"
            lines.append(f"agent.{name} max_cost_usd: {old_text} -> {new_text} per run")

    for name in sorted(before.models.keys() & after.models.keys()):
        old = before.models[name]
        new = after.models[name]
        if old.max_tokens == new.max_tokens:
            continue
        old_limit = old.max_tokens
        new_limit = new.max_tokens
        if old_limit is None or new_limit is None:
            lines.append(
                f"model.{name} max_tokens: {old_limit or 'provider default'} -> "
                f"{new_limit or 'provider default'}; output cost impact is unknown"
            )
            continue
        price = new.price or old.price
        if price is None:
            lines.append(
                f"model.{name} max_tokens: {old_limit} -> {new_limit}; "
                "output cost impact is unknown without a declared price"
            )
            continue
        delta = (new_limit - old_limit) * price.output_per_mtok / 1_000_000
        sign = "+" if delta > 0 else ""
        lines.append(
            f"model.{name} max_tokens: {old_limit} -> {new_limit}; "
            f"output ceiling changes by {price.currency} {sign}{_format_number(delta)} "
            "per call at the declared output rate"
        )
    return lines


def build_plan(before: ConfigDocument, after: ConfigDocument) -> dict[str, object]:
    """Compare two validated project documents without exposing configured values."""

    if before.config is None or after.config is None:
        raise ValueError("cannot plan an invalid project")

    before_data = before.config.model_dump(mode="json")
    after_data = after.config.model_dump(mode="json")
    config_is_equal = before_data == after_data
    entries = [
        PlanChange(
            _classification(path, old, new, before.config, after.config),
            _display_path(path),
            _action(old, new),
        )
        for path, old, new in _differences(before_data, after_data)
    ]

    before_files = build_lock(before)["files"]
    after_files = build_lock(after)["files"]
    before_config_files = _config_files(before)
    after_config_files = _config_files(after)
    config_files = before_config_files.keys() | after_config_files.keys()
    if config_is_equal:
        for path in sorted(config_files):
            if before_config_files.get(path) == after_config_files.get(path):
                continue
            action = (
                "added"
                if path not in before_config_files
                else "removed"
                if path not in after_config_files
                else "changed"
            )
            entries.append(PlanChange("cosmetic", path, action))

    before_references = _referenced_files(before)
    after_references = _referenced_files(after)
    for path in sorted(before_files.keys() | after_files.keys()):
        if path in config_files or before_files.get(path) == after_files.get(path):
            continue
        roles = before_references.get(path, set()) | after_references.get(path, set())
        classification: Classification = "privilege" if "tool_schema" in roles else "behavioral"
        action = (
            "added"
            if path not in before_files
            else "removed"
            if path not in after_files
            else "changed"
        )
        entries.append(PlanChange(classification, path, action))

    rank = {"privilege": 0, "behavioral": 1, "cosmetic": 2}
    entries.sort(key=lambda item: (rank[item.classification], item.path, item.action))
    cost = _cost_impact(before.config, after.config)
    summary = {
        "changes": len(entries),
        "privilege_changes": sum(entry.classification == "privilege" for entry in entries),
        "behavioral_changes": sum(entry.classification == "behavioral" for entry in entries),
        "cosmetic_changes": sum(entry.classification == "cosmetic" for entry in entries),
    }
    return {
        "changes": [asdict(entry) for entry in entries],
        "cost_impact": cost,
        "summary": summary,
    }
