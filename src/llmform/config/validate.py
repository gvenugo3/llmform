from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from difflib import get_close_matches

from llmform.config.loader import ConfigDocument
from llmform.diagnostics import Diagnostic, Position, Severity


def _address(value: str, kind: str) -> str:
    prefix = f"{kind}."
    return value[len(prefix) :] if value.startswith(prefix) else value


def _singular(kind: str) -> str:
    return "policy" if kind == "policies" else kind[:-1]


@dataclass(frozen=True)
class DependencyGraph:
    """A graph whose edges point from resources to their dependencies."""

    dependencies: Mapping[str, tuple[str, ...]]

    def cycle(self) -> tuple[str, ...] | None:
        visited: set[str] = set()
        active: list[str] = []
        active_set: set[str] = set()

        def visit(node: str) -> tuple[str, ...] | None:
            if node in active_set:
                start = active.index(node)
                return (*active[start:], node)
            if node in visited:
                return None
            active.append(node)
            active_set.add(node)
            for dependency in sorted(self.dependencies.get(node, ())):
                if result := visit(dependency):
                    return result
            active.pop()
            active_set.remove(node)
            visited.add(node)
            return None

        for node in sorted(self.dependencies):
            if result := visit(node):
                return result
        return None

    def topological_order(self) -> tuple[str, ...]:
        """Return dependencies before consumers, with lexical tie-breaking."""

        if cycle := self.cycle():
            raise ValueError(f"dependency cycle: {' -> '.join(cycle)}")
        visited: set[str] = set()
        ordered: list[str] = []

        def visit(node: str) -> None:
            if node in visited:
                return
            for dependency in sorted(self.dependencies.get(node, ())):
                visit(dependency)
            visited.add(node)
            ordered.append(node)

        for node in sorted(self.dependencies):
            visit(node)
        return tuple(ordered)

    def reachable_from(self, roots: Iterable[str]) -> set[str]:
        reachable: set[str] = set()
        pending = list(roots)
        while pending:
            node = pending.pop()
            if node in reachable:
                continue
            reachable.add(node)
            pending.extend(self.dependencies.get(node, ()))
        return reachable


@dataclass(frozen=True)
class ReferenceResult:
    graph: DependencyGraph
    order: tuple[str, ...]
    diagnostics: tuple[Diagnostic, ...]


def resolve_references(document: ConfigDocument) -> ReferenceResult:
    config = document.config
    if config is None:
        return ReferenceResult(DependencyGraph({}), (), ())

    diagnostics: list[Diagnostic] = []
    positions: dict[str, Position] = {}
    dependencies: dict[str, set[str]] = {}
    kinds = ("providers", "models", "sources", "tools", "policies", "agents")
    for kind in kinds:
        singular = _singular(kind)
        for name in getattr(config, kind):
            address = f"{singular}.{name}"
            dependencies[address] = set()
            positions[address] = document.position((kind, name))

    def require(
        consumer: str,
        value: str,
        kind: str,
        path: tuple[str | int, ...],
    ) -> str | None:
        singular = _singular(kind)
        if not value.startswith(f"{singular}."):
            declarations = getattr(config, kind)
            bare_name = value.rsplit(".", 1)[-1]
            matches = get_close_matches(bare_name, sorted(declarations), n=1, cutoff=0.6)
            diagnostics.append(
                Diagnostic(
                    "LLMF305",
                    Severity.ERROR,
                    f"reference {value!r} must be a {singular}.<name> address",
                    document.position(path),
                    suggestion=f"{singular}.{matches[0]}" if matches else None,
                )
            )
            return None
        name = _address(value, singular)
        address = f"{singular}.{name}"
        declarations = getattr(config, kind)
        if name not in declarations:
            matches = get_close_matches(name, sorted(declarations), n=1, cutoff=0.6)
            diagnostics.append(
                Diagnostic(
                    "LLMF300",
                    Severity.ERROR,
                    f"unknown reference {address}",
                    document.position(path),
                    suggestion=f"{singular}.{matches[0]}" if matches else None,
                )
            )
            return None
        dependencies[consumer].add(address)
        return name

    for model_name, model in config.models.items():
        require(
            f"model.{model_name}",
            model.provider,
            "providers",
            ("models", model_name, "provider"),
        )

    for tool_name, tool in config.tools.items():
        consumer = f"tool.{tool_name}"
        source_name = require(consumer, tool.source, "sources", ("tools", tool_name, "source"))
        if source_name and tool.operation not in config.sources[source_name].operations:
            matches = get_close_matches(
                tool.operation,
                sorted(config.sources[source_name].operations),
                n=1,
                cutoff=0.6,
            )
            diagnostics.append(
                Diagnostic(
                    "LLMF301",
                    Severity.ERROR,
                    f"source.{source_name} has no operation {tool.operation!r}",
                    document.position(("tools", tool_name, "operation")),
                    suggestion=matches[0] if matches else None,
                )
            )

    match_kinds = {
        "tool": "tools",
        "source": "sources",
        "model": "models",
        "provider": "providers",
    }
    for policy_name, policy in config.policies.items():
        if policy.match is None:
            continue
        for field, kind in match_kinds.items():
            if value := getattr(policy.match, field):
                require(
                    f"policy.{policy_name}",
                    value,
                    kind,
                    ("policies", policy_name, "match", field),
                )

    attached_policies: set[str] = set()
    for agent_name, agent in config.agents.items():
        consumer = f"agent.{agent_name}"
        require(consumer, agent.model, "models", ("agents", agent_name, "model"))
        for index, tool in enumerate(agent.tools):
            require(consumer, tool, "tools", ("agents", agent_name, "tools", index))
        for index, policy in enumerate(agent.policies):
            name = require(
                consumer,
                policy,
                "policies",
                ("agents", agent_name, "policies", index),
            )
            if name is not None:
                attached_policies.add(name)

    graph = DependencyGraph({node: tuple(sorted(items)) for node, items in dependencies.items()})
    cycle = graph.cycle()
    if cycle:
        diagnostics.append(
            Diagnostic(
                "LLMF304",
                Severity.ERROR,
                f"dependency cycle: {' -> '.join(cycle)}",
                positions[cycle[0]],
            )
        )
        order: tuple[str, ...] = ()
    else:
        order = graph.topological_order()

    reachable = graph.reachable_from(f"agent.{name}" for name in config.agents)
    for kind in ("providers", "models", "sources", "tools", "policies"):
        singular = _singular(kind)
        declarations: Iterable[str] = getattr(config, kind)
        for name in declarations:
            address = f"{singular}.{name}"
            if kind == "policies" and name not in attached_policies:
                diagnostics.append(
                    Diagnostic(
                        "LLMF302",
                        Severity.WARNING,
                        f"{address} is not attached to any agent",
                        positions[address],
                    )
                )
            elif kind != "policies" and address not in reachable:
                diagnostics.append(
                    Diagnostic(
                        "LLMF303",
                        Severity.WARNING,
                        f"{address} is unreachable",
                        positions[address],
                    )
                )

    return ReferenceResult(graph, order, tuple(diagnostics))


def validate_references(document: ConfigDocument) -> list[Diagnostic]:
    return list(resolve_references(document).diagnostics)
