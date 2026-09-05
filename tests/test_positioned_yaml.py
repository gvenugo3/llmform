from __future__ import annotations

from llmform.config.loader import load_project


def test_key_and_value_positions_are_distinct(tmp_path) -> None:
    text = """\
providers:
  local:
    type: ollama
    retires: 2
"""
    (tmp_path / "llmform.yaml").write_text(text, encoding="utf-8")
    document = load_project(tmp_path)
    path = ("providers", "local", "retires")
    assert document.key_position(path).line == 4
    assert document.key_position(path).column == 5
    assert document.position(path).line == 4
    assert document.position(path).column == 14
    unknown = next(item for item in document.diagnostics if item.code == "LLMF100")
    assert unknown.position == document.key_position(path)


def test_bad_scalar_diagnostic_uses_value_position(tmp_path) -> None:
    text = """\
models:
  default:
    provider: provider.local
    id: test
    temperature: hot
"""
    (tmp_path / "llmform.yaml").write_text(text, encoding="utf-8")
    document = load_project(tmp_path)
    path = ("models", "default", "temperature")
    wrong_type = next(item for item in document.diagnostics if item.code == "LLMF100")
    assert wrong_type.position == document.position(path)
    assert wrong_type.position.line == 5
    assert wrong_type.position.column == 18


def test_duplicate_key_reports_second_and_cites_first(tmp_path) -> None:
    text = """\
audit:
  record_payloads: false
  record_payloads: true
"""
    path = tmp_path / "llmform.yaml"
    path.write_text(text, encoding="utf-8")
    duplicate = next(item for item in load_project(tmp_path).diagnostics if item.code == "LLMF002")
    assert duplicate.position.line == 3
    assert duplicate.position.column == 3
    assert f"{path}:2:3" in (duplicate.hint or "")


def test_recursive_yaml_alias_is_a_positioned_diagnostic(tmp_path) -> None:
    path = tmp_path / "llmform.yaml"
    path.write_text("value: &value [*value]\n", encoding="utf-8")

    document = load_project(tmp_path)

    diagnostic = next(item for item in document.diagnostics if item.code == "LLMF006")
    assert diagnostic.message == "recursive YAML aliases are unsupported"
    assert diagnostic.position.file == path
    assert diagnostic.position.line == 1


def test_source_round_trip_preserves_comments_order_and_spelling(tmp_path) -> None:
    text = """\
# project comment
version: "0.1"  # keep the spelling and inline comment
audit:
  # nested comment
  record_payloads: false
providers: {}
"""
    path = tmp_path / "llmform.yaml"
    path.write_text(text, encoding="utf-8")
    document = load_project(tmp_path)
    assert [source.file for source in document.sources] == [path]
    assert document.sources[0].node is not None
    assert document.sources[0].render() == text
    assert list(document.raw) == ["version", "audit", "providers"]


def test_comment_only_source_is_retained(tmp_path) -> None:
    text = "# this fragment intentionally contains no resources\n"
    path = tmp_path / "notes.llmform.yaml"
    path.write_text(text, encoding="utf-8")
    document = load_project(tmp_path)
    assert document.sources[0].node is None
    assert document.sources[0].render() == text
