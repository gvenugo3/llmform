from __future__ import annotations

from pathlib import Path

from llmform.config.discovery import discover_files, find_project_root
from llmform.config.loader import load_project


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_nearest_ancestor_project_wins(tmp_path: Path) -> None:
    write(tmp_path / "llmform.yaml", 'version: "outer"\n')
    project = tmp_path / "packages" / "support"
    write(project / "llmform.yaml", 'version: "0.1"\n')
    nested = project / "src" / "handlers"
    nested.mkdir(parents=True)
    assert find_project_root(nested) == project
    assert load_project(nested).root == project


def test_fragments_only_form_a_project(tmp_path: Path) -> None:
    fragment = write(
        tmp_path / "providers.llmform.yaml",
        "providers:\n  local:\n    type: ollama\n",
    )
    document = load_project(tmp_path)
    assert document.files == [fragment]
    assert document.config is not None
    assert "local" in document.config.providers


def test_primary_loads_before_lexically_sorted_fragments(tmp_path: Path) -> None:
    primary = write(tmp_path / "llmform.yaml", 'version: "0.1"\n')
    zulu = write(tmp_path / "z.llmform.yaml", "agents: {}\n")
    alpha = write(tmp_path / "a.llmform.yaml", "providers: {}\n")
    assert discover_files(tmp_path) == [primary, alpha, zulu]


def test_fragment_order_cannot_change_merged_meaning(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    provider = "providers:\n  local:\n    type: ollama\n"
    model = "models:\n  default:\n    provider: provider.local\n    id: llama3.2\n"
    write(first / "a.llmform.yaml", provider)
    write(first / "z.llmform.yaml", model)
    write(second / "a.llmform.yaml", model)
    write(second / "z.llmform.yaml", provider)
    first_config = load_project(first).config
    second_config = load_project(second).config
    assert first_config is not None and second_config is not None
    assert first_config.model_dump() == second_config.model_dump()


def test_duplicate_resource_cites_first_and_keeps_its_position(tmp_path: Path) -> None:
    first = write(
        tmp_path / "a.llmform.yaml",
        "providers:\n  local:\n    type: ollama\n",
    )
    second = write(
        tmp_path / "b.llmform.yaml",
        "providers:\n  local:\n    type: openai\n",
    )
    third = write(
        tmp_path / "c.llmform.yaml",
        "providers:\n  local:\n    type: ollama\n",
    )
    document = load_project(tmp_path)
    duplicates = [item for item in document.diagnostics if item.code == "LLMF003"]
    assert [item.position.file for item in duplicates] == [second, third]
    first_position = document.key_position(("providers", "local"))
    assert first_position.file == first
    assert all(first_position.display() in (item.hint or "") for item in duplicates)


def test_duplicate_singleton_key_cites_both_files(tmp_path: Path) -> None:
    first = write(tmp_path / "llmform.yaml", 'version: "0.1"\n')
    second = write(tmp_path / "extra.llmform.yaml", 'version: "0.1"\n')
    document = load_project(tmp_path)
    duplicate = next(item for item in document.diagnostics if item.code == "LLMF004")
    assert duplicate.position.file == second
    assert str(first) in (duplicate.hint or "")
