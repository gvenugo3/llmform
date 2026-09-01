from pathlib import Path

from typer.testing import CliRunner

from llmform.cli import app
from tests.test_config import VALID

runner = CliRunner()


def test_validate_command(tmp_path: Path) -> None:
    (tmp_path / "llmform.yaml").write_text(VALID, encoding="utf-8")
    result = runner.invoke(app, ["validate", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "Valid:" in result.output


def test_validate_command_fails_for_unknown_field(tmp_path: Path) -> None:
    (tmp_path / "llmform.yaml").write_text("mdoels: {}\n", encoding="utf-8")
    result = runner.invoke(app, ["validate", str(tmp_path)])
    assert result.exit_code == 1
    assert "LLMF100" in result.output
