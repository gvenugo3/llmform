"""Construct the statically typed variable set available at each policy hook."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from llmform.config.loader import ConfigDocument
from llmform.diagnostics import Diagnostic, Position, Severity
from llmform.policy.cel.schema import CompiledSchema, compile_schema_file
from llmform.policy.cel.types import (
    BOOL,
    DOUBLE,
    DURATION,
    INT,
    STRING,
    CelType,
    list_of,
    map_of,
    object_of,
)
from llmform.types import Hook

_DEFAULT_INPUT = object_of(message=STRING)
_DEFAULT_OUTPUT = object_of(text=STRING)

_PRINCIPAL = object_of(
    id=STRING,
    tenant=STRING,
    role=STRING,
    scopes=list_of(STRING),
    attrs=map_of(STRING, STRING),
)
_TOOL_CALL = object_of(id=STRING, name=STRING, arguments=map_of(STRING, STRING))
_TOOL_RESULT = object_of(tool_call_id=STRING, name=STRING, content=STRING, is_error=BOOL)
_MESSAGE = object_of(
    role=STRING,
    content=STRING,
    tool_calls=list_of(_TOOL_CALL),
    tool_results=list_of(_TOOL_RESULT),
)
_RUN = object_of(
    id=STRING,
    iteration=INT,
    cost_usd=DOUBLE,
    elapsed=DURATION,
    principal=_PRINCIPAL,
)


@dataclass(frozen=True)
class HookEnvironment:
    hook: Hook
    variables: dict[str, CelType]


class SchemaCatalog:
    """Load each project-local schema once."""

    def __init__(self, root: Path):
        self.root = root
        self._cache: dict[Path, CompiledSchema] = {}

    def load(self, reference: str) -> CompiledSchema:
        path = Path(reference)
        if not path.is_absolute():
            path = self.root / path
        path = path.resolve()
        if path not in self._cache:
            self._cache[path] = compile_schema_file(path)
        return self._cache[path]


def _name(reference: str, kind: str) -> str:
    return reference.removeprefix(f"{kind}.")


def _identity(**fields: CelType) -> CelType:
    return object_of(name=STRING, **fields)


def _schema_type(
    catalog: SchemaCatalog,
    reference: str,
    diagnostics: list[Diagnostic],
) -> CelType | None:
    compiled = catalog.load(reference)
    diagnostics.extend(compiled.diagnostics)
    return compiled.type


def build_hook_environment(
    document: ConfigDocument,
    agent_name: str,
    hook: Hook,
    *,
    tool_name: str | None = None,
    catalog: SchemaCatalog | None = None,
) -> tuple[HookEnvironment | None, list[Diagnostic]]:
    """Build one hook environment for an agent and optional tool invocation."""

    diagnostics: list[Diagnostic] = []
    config = document.config
    if config is None:
        return None, diagnostics
    agent_name = _name(agent_name, "agent")
    agent = config.agents.get(agent_name)
    if agent is None:
        diagnostics.append(
            Diagnostic(
                "LLMF402",
                Severity.ERROR,
                f"unknown agent agent.{agent_name}",
                Position(document.files[0] if document.files else document.root),
            )
        )
        return None, diagnostics

    catalog = catalog or SchemaCatalog(document.root)
    variables: dict[str, CelType] = {
        "principal": _PRINCIPAL,
        "agent": _identity(),
        "run": _RUN,
    }

    if hook == Hook.REQUEST:
        variables["input"] = (
            _schema_type(catalog, agent.input, diagnostics) if agent.input else _DEFAULT_INPUT
        )
    elif hook == Hook.MODEL_CALL:
        variables.update(
            model=_identity(id=STRING, provider=STRING),
            messages=list_of(_MESSAGE),
            data=object_of(classes=list_of(STRING)),
        )
    elif hook in {Hook.TOOL_CALL, Hook.TOOL_RESULT}:
        if tool_name is None:
            diagnostics.append(
                Diagnostic(
                    "LLMF402",
                    Severity.ERROR,
                    f"{hook.value} environment requires a tool",
                    document.position(("agents", agent_name, "tools")),
                )
            )
            return None, diagnostics
        tool_name = _name(tool_name, "tool")
        tool = config.tools.get(tool_name)
        if tool is None:
            diagnostics.append(
                Diagnostic(
                    "LLMF402",
                    Severity.ERROR,
                    f"unknown tool tool.{tool_name}",
                    document.position(("agents", agent_name, "tools")),
                )
            )
            return None, diagnostics
        source_name = _name(tool.source, "source")
        source = config.sources.get(source_name)
        variables.update(
            tool=_identity(source=STRING, operation=STRING),
            source=_identity(type=STRING),
        )
        if hook == Hook.TOOL_CALL:
            schema_reference = tool.input
            variable = "args"
        else:
            operation = source.operations.get(tool.operation) if source is not None else None
            schema_reference = tool.output or (operation.returns if operation is not None else "")
            variable = "result"
        if schema_reference:
            schema_type = _schema_type(catalog, schema_reference, diagnostics)
            if schema_type is not None:
                variables[variable] = schema_type
    elif hook == Hook.RESPONSE:
        variables["draft"] = (
            _schema_type(catalog, agent.output, diagnostics) if agent.output else _DEFAULT_OUTPUT
        )
    elif hook == Hook.LOOP:
        variables.update(iteration=INT, cost_usd=DOUBLE, elapsed=DURATION)

    if diagnostics:
        return None, diagnostics
    return HookEnvironment(hook, variables), diagnostics


def validate_schema_profiles(document: ConfigDocument) -> list[Diagnostic]:
    """Validate every policy-facing schema in the project exactly once."""

    config = document.config
    if config is None:
        return []
    catalog = SchemaCatalog(document.root)
    references: set[str] = set()
    for agent in config.agents.values():
        references.update(reference for reference in (agent.input, agent.output) if reference)
    for source in config.sources.values():
        references.update(operation.returns for operation in source.operations.values())
    for tool in config.tools.values():
        references.add(tool.input)
        if tool.output:
            references.add(tool.output)
    diagnostics: list[Diagnostic] = []
    for reference in sorted(references):
        diagnostics.extend(catalog.load(reference).diagnostics)
    return diagnostics
