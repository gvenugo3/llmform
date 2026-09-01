from __future__ import annotations

from pathlib import Path

CONFIG_NAME = "llmform.yaml"
CONFIG_GLOB = "*.llmform.yaml"


def find_project_root(start: Path) -> Path | None:
    current = start.resolve()
    if current.is_file():
        current = current.parent
    for candidate in (current, *current.parents):
        if (candidate / CONFIG_NAME).is_file() or any(candidate.glob(CONFIG_GLOB)):
            return candidate
    return None


def discover_files(root: Path) -> list[Path]:
    primary = root / CONFIG_NAME
    fragments = sorted(path for path in root.glob(CONFIG_GLOB) if path != primary)
    return ([primary] if primary.is_file() else []) + fragments
