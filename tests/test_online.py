from __future__ import annotations

from pathlib import Path

from llmform.config.loader import load_project
from llmform.config.online import validate_online
from tests.test_config import write_config


def test_online_mcp_check_resolves_without_execution(tmp_path: Path, monkeypatch) -> None:
    config = """\
version: "0.1"
sources:
  api:
    type: mcp
    command: [missing-mcp]
    operations:
      search:
        returns: schemas/result.json
"""
    write_config(tmp_path, config)
    monkeypatch.setattr("llmform.config.online.shutil.which", lambda command: None)
    monkeypatch.setattr("llmform.config.online._reachable", lambda url: None)

    diagnostics = validate_online(load_project(tmp_path))

    assert len(diagnostics) == 1
    assert any("missing-mcp" in item.message for item in diagnostics)
