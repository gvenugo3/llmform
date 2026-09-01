from __future__ import annotations

from collections.abc import Iterable

from llmform.config.loader import ConfigDocument
from llmform.diagnostics import Diagnostic, Severity


def _address(value: str, kind: str) -> str:
    prefix = f"{kind}."
    return value[len(prefix) :] if value.startswith(prefix) else value


def _singular(kind: str) -> str:
    return "policy" if kind == "policies" else kind[:-1]


def validate_references(document: ConfigDocument) -> list[Diagnostic]:
    config = document.config
    if config is None:
        return []
    diagnostics: list[Diagnostic] = []
    reachable: dict[str, set[str]] = {
        "providers": set(),
        "models": set(),
        "sources": set(),
        "tools": set(),
        "policies": set(),
    }

    def require(value: str, kind: str, path: tuple[str | int, ...]) -> str | None:
        singular = _singular(kind)
        name = _address(value, singular)
        declarations = getattr(config, kind)
        if name not in declarations:
            diagnostics.append(
                Diagnostic(
                    "LLMF300",
                    Severity.ERROR,
                    f"unknown reference {singular}.{name}",
                    document.position(path),
                )
            )
            return None
        reachable[kind].add(name)
        return name

    for model_name, model in config.models.items():
        require(model.provider, "providers", ("models", model_name, "provider"))

    for tool_name, tool in config.tools.items():
        source_name = require(tool.source, "sources", ("tools", tool_name, "source"))
        if source_name and tool.operation not in config.sources[source_name].operations:
            diagnostics.append(
                Diagnostic(
                    "LLMF301",
                    Severity.ERROR,
                    f"source.{source_name} has no operation {tool.operation!r}",
                    document.position(("tools", tool_name, "operation")),
                )
            )

    for agent_name, agent in config.agents.items():
        require(agent.model, "models", ("agents", agent_name, "model"))
        for index, tool in enumerate(agent.tools):
            require(tool, "tools", ("agents", agent_name, "tools", index))
        for index, policy in enumerate(agent.policies):
            require(policy, "policies", ("agents", agent_name, "policies", index))

    for kind, names in reachable.items():
        declarations: Iterable[str] = getattr(config, kind)
        for name in declarations:
            if name not in names:
                code = "LLMF302" if kind == "policies" else "LLMF303"
                message = (
                    f"policy.{name} is not attached to any agent"
                    if kind == "policies"
                    else f"{_singular(kind)}.{name} is unreachable"
                )
                diagnostics.append(
                    Diagnostic(code, Severity.WARNING, message, document.position((kind, name)))
                )
    return diagnostics
