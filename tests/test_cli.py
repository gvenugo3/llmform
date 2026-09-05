from pathlib import Path

from typer.testing import CliRunner

from llmform.cli import app
from tests.test_config import VALID, write_config

runner = CliRunner()


def test_validate_command(tmp_path: Path) -> None:
    write_config(tmp_path)
    result = runner.invoke(app, ["validate", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "Valid:" in result.output


def test_validate_command_fails_for_unknown_field(tmp_path: Path) -> None:
    (tmp_path / "llmform.yaml").write_text("mdoels: {}\n", encoding="utf-8")
    result = runner.invoke(app, ["validate", str(tmp_path)])
    assert result.exit_code == 1
    assert "LLMF100" in result.output


def test_validate_command_rejects_unsupported_policy_schema(tmp_path: Path) -> None:
    write_config(tmp_path)
    (tmp_path / "schemas/input.json").write_text(
        '{"oneOf": [{"type": "string"}, {"type": "integer"}]}', encoding="utf-8"
    )
    result = runner.invoke(app, ["validate", str(tmp_path)])
    assert result.exit_code == 1
    assert "LLMF400" in result.output
    assert "oneOf" in result.output


def test_validate_command_reports_warning_without_failing(tmp_path: Path) -> None:
    config = VALID.replace("method: GET", "method: POST").replace(
        "    input: schemas/input.json\n",
        "    input: schemas/input.json\n    detokenize: [query]\n",
    )
    write_config(tmp_path, config)
    result = runner.invoke(app, ["validate", str(tmp_path)])
    assert result.exit_code == 0
    assert "warning [LLMF507]" in result.output
