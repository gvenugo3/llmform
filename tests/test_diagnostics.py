from __future__ import annotations

from pathlib import Path

from llmform.diagnostics import (
    Diagnostic,
    Position,
    Severity,
    exit_code,
    render_all,
    render_json,
)

GOLDEN = Path(__file__).with_name("golden")
ROOT = Path("/workspace/project")


def example_diagnostics() -> list[Diagnostic]:
    return [
        Diagnostic(
            "LLMF507",
            Severity.WARNING,
            "detokenization targets a write operation",
            Position(ROOT / "b.llmform.yaml", 9, 5),
            "re-substitution may disclose a value",
        ),
        Diagnostic(
            "LLMF411",
            Severity.ERROR,
            "undefined field 'amount'",
            Position(ROOT / "a.llmform.yaml", 3, 17),
            "field is checked against refund_input.json",
            "amount_cents",
            "on tool.issue_refund (schema: refund_input.json)",
        ),
    ]


def test_human_renderer_matches_golden() -> None:
    rendered = render_all(example_diagnostics(), ROOT)
    assert rendered + "\n" == (GOLDEN / "diagnostics.txt").read_text(encoding="utf-8")


def test_json_renderer_matches_golden() -> None:
    rendered = render_json(example_diagnostics())
    assert rendered + "\n" == (GOLDEN / "diagnostics.json").read_text(encoding="utf-8")


def test_renderer_caps_findings_and_reports_omitted_count() -> None:
    rendered = render_all(example_diagnostics(), ROOT, limit=1)
    assert "undefined field" in rendered
    assert "detokenization targets" not in rendered
    assert "1 shown, 1 omitted" in rendered


def test_exit_code_reflects_highest_severity() -> None:
    warning = example_diagnostics()[0]
    assert exit_code([]) == 0
    assert exit_code([warning]) == 0
    assert exit_code(example_diagnostics()) == 1
