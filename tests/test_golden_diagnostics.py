from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import CliRunner

from llmform.cli import app
from llmform.config.loader import load_project
from llmform.diagnostics import render_all, render_json
from llmform.policy.cel.environment import build_hook_environment
from llmform.types import Hook

GOLDEN_ROOT = Path(__file__).with_name("golden")
GOLDENS = GOLDEN_ROOT / "validation"
INTERNAL_GOLDENS = GOLDEN_ROOT / "internal"
SOURCE = Path(__file__).parents[1] / "src" / "llmform"
CODE = re.compile(r'"(LLMF\d{3})"')
runner = CliRunner()

MINIMAL = 'version: "0.1"\n'
MODEL = """\
version: "0.1"
providers:
  local:
    type: ollama
models:
  default:
    provider: provider.local
    id: llama3.2
"""


def _fixtures() -> list[Path]:
    return sorted(path.parent for path in GOLDENS.glob("*/llmform.yaml"))


def _arguments(project: Path) -> list[str]:
    """Read optional extra CLI arguments, one per line, from the fixture's args.txt."""

    path = project / "args.txt"
    if not path.is_file():
        return []
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line]


def _normalized_json(output: str, project: Path) -> str:
    payload = json.loads(output)
    for diagnostic in payload["diagnostics"]:
        diagnostic["file"] = Path(diagnostic["file"]).relative_to(project.resolve()).as_posix()
    return json.dumps(payload, indent=2).replace(project.resolve().as_posix(), ".") + "\n"


def _normalized_human(output: str, project: Path) -> str:
    normalized = output.replace(project.resolve().as_posix(), ".")
    return "\n".join(line.rstrip() for line in normalized.splitlines()) + "\n"


def _check(outputs: dict[Path, str], update: bool) -> None:
    for expected, actual in outputs.items():
        if update:
            expected.parent.mkdir(parents=True, exist_ok=True)
            expected.write_text(actual, encoding="utf-8")
        else:
            assert actual == expected.read_text(encoding="utf-8")


@pytest.mark.parametrize("project", _fixtures(), ids=lambda path: path.name)
def test_diagnostic_fixture(project: Path, request: pytest.FixtureRequest) -> None:
    arguments = _arguments(project)
    human = runner.invoke(app, ["validate", str(project), *arguments])
    structured = runner.invoke(app, ["validate", str(project), "--json", *arguments])

    assert human.exit_code == structured.exit_code
    _check(
        {
            project / "expected.txt": _normalized_human(human.output, project),
            project / "expected.json": _normalized_json(structured.output, project),
        },
        request.config.getoption("--update-goldens"),
    )


# Some diagnostics guard internal invariants that no configuration can violate. Each case
# forces its invariant to break, then renders through the same text and JSON paths.


def _validate(project: Path) -> tuple[str, str]:
    human = runner.invoke(app, ["validate", str(project)])
    structured = runner.invoke(app, ["validate", str(project), "--json"])
    assert human.exit_code == structured.exit_code == 1
    return human.output, structured.output


def _no_configuration(project: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, str]:
    return _validate(project)


def _schema_version_mismatch(project: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, str]:
    (project / "llmform.yaml").write_text(MINIMAL, encoding="utf-8")
    monkeypatch.setattr("llmform.config.schema.CONFIG_SCHEMA_VERSION", "0.0")
    return _validate(project)


def _dependency_cycle(project: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, str]:
    (project / "llmform.yaml").write_text(MODEL, encoding="utf-8")
    cycle = ("model.default", "provider.local", "model.default")
    monkeypatch.setattr("llmform.config.validate.DependencyGraph.cycle", lambda self: cycle)
    return _validate(project)


def _unknown_hook_agent(project: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, str]:
    (project / "llmform.yaml").write_text(MODEL, encoding="utf-8")
    _, diagnostics = build_hook_environment(load_project(project), "agent.missing", Hook.REQUEST)
    return render_all(diagnostics, project.resolve()) + "\n", render_json(diagnostics)


INTERNAL_CASES: dict[str, Callable[[Path, pytest.MonkeyPatch], tuple[str, str]]] = {
    "no_configuration": _no_configuration,
    "schema_version_mismatch": _schema_version_mismatch,
    "dependency_cycle": _dependency_cycle,
    "unknown_hook_agent": _unknown_hook_agent,
}


@pytest.mark.parametrize("name", sorted(INTERNAL_CASES))
def test_internal_diagnostic(
    name: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    human, structured = INTERNAL_CASES[name](tmp_path, monkeypatch)
    folder = INTERNAL_GOLDENS / name
    _check(
        {
            folder / "expected.txt": _normalized_human(human, tmp_path),
            folder / "expected.json": _normalized_json(structured, tmp_path),
        },
        request.config.getoption("--update-goldens"),
    )


def test_every_diagnostic_code_has_a_golden_fixture() -> None:
    defined = {
        code
        for path in SOURCE.rglob("*.py")
        for code in CODE.findall(path.read_text(encoding="utf-8"))
    }
    covered = {
        item["code"]
        for path in GOLDEN_ROOT.glob("*/*/expected.json")
        for item in json.loads(path.read_text(encoding="utf-8"))["diagnostics"]
    }
    assert sorted(defined - covered) == []
