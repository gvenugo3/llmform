from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from llmform.cli import app

GOLDENS = Path(__file__).with_name("golden").joinpath("validation")
runner = CliRunner()


def _fixtures() -> list[Path]:
    return sorted(path.parent for path in GOLDENS.glob("*/llmform.yaml"))


def _normalized_json(output: str, project: Path) -> str:
    payload = json.loads(output)
    for diagnostic in payload["diagnostics"]:
        diagnostic["file"] = Path(diagnostic["file"]).relative_to(project.resolve()).as_posix()
    return json.dumps(payload, indent=2).replace(str(project.resolve()), ".") + "\n"


def _normalized_human(output: str, project: Path) -> str:
    return "\n".join(line.rstrip() for line in output.replace(str(project.resolve()), ".").splitlines()) + "\n"


@pytest.mark.parametrize("project", _fixtures(), ids=lambda path: path.name)
def test_diagnostic_fixture(project: Path, request: pytest.FixtureRequest) -> None:
    update = request.config.getoption("--update-goldens")
    human = runner.invoke(app, ["validate", str(project)])
    structured = runner.invoke(app, ["validate", str(project), "--json"])

    assert human.exit_code == structured.exit_code
    outputs = {
        project / "expected.txt": _normalized_human(human.output, project),
        project / "expected.json": _normalized_json(structured.output, project),
    }
    for expected, actual in outputs.items():
        if update:
            expected.write_text(actual, encoding="utf-8")
        else:
            assert actual == expected.read_text(encoding="utf-8")
