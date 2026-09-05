"""Offline L2 checks spanning multiple typed configuration resources."""

from __future__ import annotations

from collections.abc import Iterable

from llmform.config.loader import ConfigDocument
from llmform.config.models import HttpSource, McpSource, OperationParameter
from llmform.diagnostics import Diagnostic, Severity
from llmform.policy.cel.compiler import expression_data_classes, expression_identifiers
from llmform.policy.cel.environment import SchemaCatalog
from llmform.policy.cel.types import CelKind, CelType

_MATCH_KEYS: dict[str, set[str]] = {
    "request": set(),
    "model_call": {"model", "provider"},
    "tool_call": {"tool", "source", "operation"},
    "tool_result": {"tool", "source", "operation"},
    "response": set(),
    "loop": set(),
}
_PARAMETER_KINDS = {
    "string": CelKind.STRING,
    "integer": CelKind.INT,
    "number": CelKind.DOUBLE,
    "boolean": CelKind.BOOL,
    "object": CelKind.OBJECT,
    "array": CelKind.LIST,
}


def _name(reference: str, kind: str) -> str:
    return reference.removeprefix(f"{kind}.")


def _type_narrows(candidate: CelType, contract: CelType) -> bool:
    if candidate.kind == CelKind.INT and contract.kind == CelKind.DOUBLE:
        return True
    if candidate.kind != contract.kind:
        return False
    if candidate.kind == CelKind.STRING:
        return (
            not contract.enum or bool(candidate.enum) and set(candidate.enum) <= set(contract.enum)
        )
    if candidate.kind == CelKind.LIST:
        return (
            candidate.item is not None
            and contract.item is not None
            and _type_narrows(candidate.item, contract.item)
        )
    if candidate.kind == CelKind.OBJECT:
        return all(
            name in contract.fields and _type_narrows(field, contract.fields[name])
            for name, field in candidate.fields.items()
        )
    if candidate.kind == CelKind.MAP:
        return (
            candidate.key is not None
            and candidate.value is not None
            and contract.key is not None
            and contract.value is not None
            and _type_narrows(candidate.key, contract.key)
            and _type_narrows(candidate.value, contract.value)
        )
    return True


def _parameter_accepts(field: CelType, parameter: OperationParameter) -> bool:
    expected = _PARAMETER_KINDS[parameter.type]
    return field.kind == expected or (field.kind == CelKind.INT and expected == CelKind.DOUBLE)


def _attached_agents(document: ConfigDocument, policy_name: str) -> Iterable[tuple[str, object]]:
    assert document.config is not None
    for agent_name, agent in document.config.agents.items():
        if any(_name(reference, "policy") == policy_name for reference in agent.policies):
            yield agent_name, agent


def validate_semantics(document: ConfigDocument) -> list[Diagnostic]:  # noqa: C901
    config = document.config
    if config is None:
        return []
    diagnostics: list[Diagnostic] = []
    catalog = SchemaCatalog(document.root)

    for tool_name, tool in config.tools.items():
        source_name = _name(tool.source, "source")
        source = config.sources.get(source_name)
        operation = source.operations.get(tool.operation) if source is not None else None
        if operation is None:
            continue
        input_schema = catalog.load(tool.input)
        input_type = input_schema.type
        if input_type is not None:
            if input_type.kind != CelKind.OBJECT:
                diagnostics.append(
                    Diagnostic(
                        "LLMF500",
                        Severity.ERROR,
                        f"tool.{tool_name} input schema must be an object",
                        document.position(("tools", tool_name, "input")),
                    )
                )
            else:
                for field_name, field_type in input_type.fields.items():
                    parameter = operation.params.get(field_name)
                    if parameter is None:
                        diagnostics.append(
                            Diagnostic(
                                "LLMF500",
                                Severity.ERROR,
                                f"tool input field {field_name!r} is not accepted by "
                                f"source.{source_name}.{tool.operation}",
                                document.position(("tools", tool_name, "input")),
                            )
                        )
                    elif not _parameter_accepts(field_type, parameter):
                        diagnostics.append(
                            Diagnostic(
                                "LLMF500",
                                Severity.ERROR,
                                f"tool input field {field_name!r} has type "
                                f"{field_type.kind.value}; "
                                f"operation parameter requires {parameter.type}",
                                document.position(("tools", tool_name, "input")),
                            )
                        )
                for parameter_name, parameter in operation.params.items():
                    if parameter.required and parameter_name not in input_schema.required_fields:
                        diagnostics.append(
                            Diagnostic(
                                "LLMF500",
                                Severity.ERROR,
                                "tool input does not require operation parameter "
                                f"{parameter_name!r}",
                                document.position(("tools", tool_name, "input")),
                            )
                        )

        if tool.output:
            output_type = catalog.load(tool.output).type
            returns_type = catalog.load(operation.returns).type
            if (
                output_type is not None
                and returns_type is not None
                and not _type_narrows(output_type, returns_type)
            ):
                diagnostics.append(
                    Diagnostic(
                        "LLMF501",
                        Severity.ERROR,
                        f"tool.{tool_name} output does not narrow the operation returns schema",
                        document.position(("tools", tool_name, "output")),
                    )
                )

        if input_type is not None and input_type.kind == CelKind.OBJECT:
            for index, field_name in enumerate(tool.detokenize):
                if field_name not in input_type.fields:
                    diagnostics.append(
                        Diagnostic(
                            "LLMF506",
                            Severity.ERROR,
                            f"detokenize field {field_name!r} is absent from the tool input schema",
                            document.position(("tools", tool_name, "detokenize", index)),
                        )
                    )
            if tool.detokenize:
                writes = (
                    isinstance(source, HttpSource) and operation.method not in {"GET", "HEAD"}
                ) or (isinstance(source, McpSource) and operation.read_only is not True)
                if writes:
                    diagnostics.append(
                        Diagnostic(
                            "LLMF507",
                            Severity.WARNING,
                            f"tool.{tool_name} detokenizes data into a write operation",
                            document.position(("tools", tool_name, "detokenize")),
                            "re-substitution can disclose tokenized values outside the run",
                        )
                    )

    for policy_name, policy in config.policies.items():
        if policy.match is not None:
            for match_key, value in policy.match.model_dump().items():
                if value is not None and match_key not in _MATCH_KEYS[policy.on]:
                    diagnostics.append(
                        Diagnostic(
                            "LLMF502",
                            Severity.ERROR,
                            f"match key {match_key!r} is not available at the {policy.on} hook",
                            document.position(("policies", policy_name, "match", match_key)),
                        )
                    )
        if policy.otherwise == "REQUIRE_APPROVAL" and policy.approval is None:
            diagnostics.append(
                Diagnostic(
                    "LLMF504",
                    Severity.ERROR,
                    f"policy.{policy_name} requires approval.approvers",
                    document.position(("policies", policy_name, "otherwise")),
                    "there is no implicit approver",
                )
            )

        if policy.rule:
            referenced_classes = expression_data_classes(policy.rule)
            for agent_name, agent in _attached_agents(document, policy_name):
                declared_classes: set[str] = set()
                for tool_reference in agent.tools:
                    tool = config.tools.get(_name(tool_reference, "tool"))
                    if tool is None:
                        continue
                    source = config.sources.get(_name(tool.source, "source"))
                    operation = (
                        source.operations.get(tool.operation) if source is not None else None
                    )
                    if operation is not None:
                        declared_classes.update(operation.classes)
                for class_name in sorted(referenced_classes - declared_classes):
                    diagnostics.append(
                        Diagnostic(
                            "LLMF503",
                            Severity.ERROR,
                            f"data class {class_name!r} is not declared by an operation "
                            f"reachable from agent.{agent_name}",
                            document.position(("policies", policy_name, "rule")),
                        )
                    )

    for agent_name, agent in config.agents.items():
        cost_required = agent.max_cost_usd is not None
        for policy_reference in agent.policies:
            policy = config.policies.get(_name(policy_reference, "policy"))
            if policy is not None and policy.rule:
                cost_required = cost_required or "cost_usd" in expression_identifiers(policy.rule)
        if not cost_required:
            continue
        model_name = _name(agent.model, "model")
        model = config.models.get(model_name)
        provider = (
            config.providers.get(_name(model.provider, "provider")) if model is not None else None
        )
        if (
            model is not None
            and model.price is None
            and getattr(provider, "type", None) != "ollama"
        ):
            diagnostics.append(
                Diagnostic(
                    "LLMF505",
                    Severity.ERROR,
                    f"model.{model_name} requires a declared price for "
                    f"agent.{agent_name} cost enforcement",
                    document.position(("agents", agent_name, "model")),
                )
            )

    return diagnostics
