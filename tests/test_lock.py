from __future__ import annotations

from llmform.config.loader import load_project
from llmform.lock import build_lock, diff_lock, write_lock
from tests.test_config import VALID, write_config


def test_lock_closure_is_stable_when_yaml_keys_are_reordered(tmp_path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    reordered = VALID.replace('version: "0.1"\nproviders:', "providers:").replace(
        "models:\n", 'version: "0.1"\nmodels:\n'
    )
    for project, config in (
        (first, VALID),
        (second, reordered),
    ):
        write_config(project, config)
        (project / "prompts").mkdir()
        (project / "prompts" / "support.md").write_text("Support", encoding="utf-8")

    first_lock = build_lock(load_project(first))
    second_lock = build_lock(load_project(second))

    assert first_lock["closure_sha256"] == second_lock["closure_sha256"]


def test_lock_changes_when_a_policy_rule_changes(tmp_path) -> None:
    write_config(tmp_path)
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "support.md").write_text("Support", encoding="utf-8")
    before = build_lock(load_project(tmp_path))
    write_config(tmp_path, VALID.replace('rule: "true"', 'rule: "false"'))

    after = build_lock(load_project(tmp_path))

    assert before["closure_sha256"] != after["closure_sha256"]


def test_lock_round_trip_reports_only_changed_sections(tmp_path) -> None:
    write_config(tmp_path)
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "support.md").write_text("Support", encoding="utf-8")
    document = load_project(tmp_path)
    write_lock(document)
    assert diff_lock(document) == ()

    write_config(tmp_path, VALID.replace('rule: "true"', 'rule: "false"'))
    assert diff_lock(load_project(tmp_path)) == ("closure_sha256", "files", "policies")
