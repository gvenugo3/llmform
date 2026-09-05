from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

DEFAULT_DIAGNOSTIC_LIMIT = 50


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
    suggestion: str | None = None
    context: str | None = None

    def render(self, root: Path | None = None) -> str:
        lines = [
            f"{self.position.display(root)}: {self.severity.value} [{self.code}]: {self.message}"
        ]
        if self.context:
            lines.extend(f"  {line}" for line in self.context.splitlines())
        if self.hint:
            hint_lines = self.hint.splitlines()
            lines.append(f"  hint: {hint_lines[0]}")
            lines.extend(f"        {line}" for line in hint_lines[1:])
        if self.suggestion:
            lines.append(f"  did you mean {self.suggestion!r}?")
        return "\n".join(lines)

    def as_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "severity": self.severity.value,
            "message": self.message,
            "file": str(self.position.file),
            "line": self.position.line,
            "column": self.position.column,
            "context": self.context,
            "hint": self.hint,
            "suggestion": self.suggestion,
        }


@dataclass(frozen=True)
class DiagnosticSummary:
    errors: int
    warnings: int
    total: int
    shown: int

    @property
    def omitted(self) -> int:
        return self.total - self.shown

    def render(self) -> str:
        def count(value: int, noun: str) -> str:
            return f"{value} {noun if value == 1 else noun + 's'}"

        result = f"{count(self.errors, 'error')}, {count(self.warnings, 'warning')}"
        if self.omitted:
            result += f" ({self.shown} shown, {self.omitted} omitted)"
        return result

    def as_dict(self) -> dict[str, int]:
        return {
            "errors": self.errors,
            "warnings": self.warnings,
            "total": self.total,
            "shown": self.shown,
            "omitted": self.omitted,
        }


def sort_diagnostics(diagnostics: Iterable[Diagnostic]) -> list[Diagnostic]:
    return sorted(
        diagnostics,
        key=lambda d: (str(d.position.file), d.position.line, d.position.column, d.code),
    )


def summarize(diagnostics: list[Diagnostic], shown: int | None = None) -> DiagnosticSummary:
    return DiagnosticSummary(
        errors=sum(item.severity == Severity.ERROR for item in diagnostics),
        warnings=sum(item.severity == Severity.WARNING for item in diagnostics),
        total=len(diagnostics),
        shown=len(diagnostics) if shown is None else shown,
    )


def _limited(
    diagnostics: Iterable[Diagnostic], limit: int
) -> tuple[list[Diagnostic], DiagnosticSummary]:
    ordered = sort_diagnostics(diagnostics)
    visible = ordered[: max(0, limit)]
    return visible, summarize(ordered, len(visible))


def render_all(
    diagnostics: Iterable[Diagnostic],
    root: Path | None = None,
    *,
    limit: int = DEFAULT_DIAGNOSTIC_LIMIT,
) -> str:
    visible, summary = _limited(diagnostics, limit)
    if not summary.total:
        return ""
    rendered = [item.render(root) for item in visible]
    rendered.append(f"Summary: {summary.render()}")
    return "\n".join(rendered)


def render_json(diagnostics: Iterable[Diagnostic], *, limit: int = DEFAULT_DIAGNOSTIC_LIMIT) -> str:
    visible, summary = _limited(diagnostics, limit)
    payload = {
        "diagnostics": [item.as_dict() for item in visible],
        "summary": summary.as_dict(),
    }
    return json.dumps(payload, indent=2)


def exit_code(diagnostics: Iterable[Diagnostic]) -> int:
    return 1 if any(item.severity == Severity.ERROR for item in diagnostics) else 0
