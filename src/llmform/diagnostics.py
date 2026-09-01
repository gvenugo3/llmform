from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True, order=True)
class Position:
    file: Path
    line: int = 1
    column: int = 1

    def display(self, root: Path | None = None) -> str:
        file = self.file
        if root is not None:
            try:
                file = file.relative_to(root)
            except ValueError:
                pass
        return f"{file}:{self.line}:{self.column}"


@dataclass(frozen=True)
class Diagnostic:
    code: str
    severity: Severity
    message: str
    position: Position
    hint: str | None = None

    def render(self, root: Path | None = None) -> str:
        result = (
            f"{self.position.display(root)}: {self.severity.value} [{self.code}]: {self.message}"
        )
        if self.hint:
            result += f"\n  hint: {self.hint}"
        return result


def render_all(diagnostics: Iterable[Diagnostic], root: Path | None = None) -> str:
    ordered = sorted(
        diagnostics,
        key=lambda d: (str(d.position.file), d.position.line, d.position.column, d.code),
    )
    return "\n".join(item.render(root) for item in ordered)
