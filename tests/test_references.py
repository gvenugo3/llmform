from __future__ import annotations

from llmform.config.loader import load_project
from llmform.config.validate import DependencyGraph, resolve_references, validate_references
from tests.test_config import VALID, write_config


def test_unknown_reference_suggests_nearest_same_kind(tmp_path) -> None:
    write_config(tmp_path, VALID.replace("model.default", "model.defualt"))

    diagnostic = next(
        item for item in validate_references(load_project(tmp_path)) if item.code == "LLMF300"
    )

    assert diagnostic.suggestion == "model.default"
    assert diagnostic.position.line > 0


def test_unknown_policy_match_reference_is_positioned(tmp_path) -> None:
    config = VALID.replace('    rule: "true"', '    match: {tool: tool.serch}\n    rule: "true"')
    write_config(tmp_path, config)
    document = load_project(tmp_path)

    diagnostic = next(item for item in validate_references(document) if item.code == "LLMF300")

    assert diagnostic.suggestion == "tool.search"
    assert diagnostic.position == document.position(("policies", "safe", "match", "tool"))


def test_reference_must_be_a_typed_address(tmp_path) -> None:
    write_config(tmp_path, VALID.replace("model.default", "default"))
    document = load_project(tmp_path)

    diagnostic = next(item for item in validate_references(document) if item.code == "LLMF305")

    assert "model.<name>" in diagnostic.message
    assert diagnostic.suggestion == "model.default"
    assert diagnostic.position == document.position(("agents", "support", "model"))


def test_unattached_policy_is_reported_at_its_declaration(tmp_path) -> None:
    config = VALID.replace(
        "agents:\n",
        '  unused:\n    on: response\n    rule: "true"\nagents:\n',
    )
    write_config(tmp_path, config)
    document = load_project(tmp_path)

    diagnostic = next(item for item in validate_references(document) if item.code == "LLMF302")

    assert "policy.unused" in diagnostic.message
    assert diagnostic.position == document.position(("policies", "unused"))


def test_dependency_order_is_deterministic_and_dependencies_come_first(tmp_path) -> None:
    write_config(tmp_path)

    first = resolve_references(load_project(tmp_path))
    second = resolve_references(load_project(tmp_path))

    assert first.order == second.order
    assert first.order.index("provider.local") < first.order.index("model.default")
    assert first.order.index("model.default") < first.order.index("agent.support")
    assert first.order.index("source.api") < first.order.index("tool.search")
    assert first.order.index("tool.search") < first.order.index("agent.support")


def test_unreachable_dependency_chain_is_reported(tmp_path) -> None:
    config = VALID.replace(
        "providers:\n",
        "providers:\n  spare:\n    type: ollama\n",
    ).replace(
        "models:\n",
        "models:\n  spare:\n    provider: provider.spare\n    id: spare\n",
    )
    write_config(tmp_path, config)

    messages = {
        item.message
        for item in validate_references(load_project(tmp_path))
        if item.code == "LLMF303"
    }

    assert "model.spare is unreachable" in messages
    assert "provider.spare is unreachable" in messages


def test_cycle_reports_full_path_and_prevents_topological_order() -> None:
    graph = DependencyGraph(
        {
            "agent.support": ("model.default",),
            "model.default": ("provider.local",),
            "provider.local": ("agent.support",),
        }
    )

    assert graph.cycle() == (
        "agent.support",
        "model.default",
        "provider.local",
        "agent.support",
    )
    try:
        graph.topological_order()
    except ValueError as exc:
        assert str(exc).endswith(
            "agent.support -> model.default -> provider.local -> agent.support"
        )
    else:  # pragma: no cover - makes the failure message explicit
        raise AssertionError("cyclic graph unexpectedly produced an order")
