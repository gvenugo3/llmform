"""CEL parsing and static type-checking for policy expressions."""

from __future__ import annotations

import ast
import difflib
from dataclasses import dataclass
from pathlib import Path

from celpy import Environment
from celpy.celparser import CELParseError
from lark import Token, Tree

from llmform.diagnostics import Diagnostic, Position, Severity
from llmform.policy.cel.environment import HookEnvironment, SchemaCatalog, build_hook_environment
from llmform.policy.cel.types import (
    BOOL,
    DOUBLE,
    INT,
    STRING,
    CelKind,
    CelType,
    list_of,
    map_of,
)
from llmform.types import Hook

MAX_AST_NODES = 256
_ITERATION_MACROS = {"all", "exists", "exists_one", "filter", "map"}
_WRAPPERS = {"member", "paren_expr", "primary"}


@dataclass(frozen=True)
class CompiledExpression:
    ast: Tree | None
    result_type: CelType | None
    diagnostics: list[Diagnostic]


class ExpressionTypeChecker:
    def __init__(self, variables: dict[str, CelType], source: Position, expression: str):
        self.variables = variables
        self.source = source
        self.expression = expression
        self.diagnostics: list[Diagnostic] = []

    def check(self, tree: Tree) -> CelType | None:
        if sum(1 for _ in tree.iter_subtrees()) > MAX_AST_NODES:
            self._error(tree, "CEL expression exceeds the static cost limit", "LLMF413")
            return None
        return self._infer(tree)

    def _infer(self, node: Tree) -> CelType | None:  # noqa: C901
        name = str(node.data)
        children = [child for child in node.children if isinstance(child, Tree)]
        if name == "expr":
            if len(children) == 1:
                return self._infer(children[0])
            condition, when_true, when_false = (self._infer(child) for child in children)
            self._expect(condition, BOOL, node, "conditional test must be bool")
            if not self._compatible(when_true, when_false):
                self._error(node, "conditional branches must have compatible types")
                return None
            return when_true
        if name in _WRAPPERS:
            return self._infer(children[0]) if children else None
        if name == "unary":
            if len(children) == 2 and str(children[0].data).startswith("unary_"):
                value = self._infer(children[1])
                operator = str(children[0].data)
                expected = BOOL if operator == "unary_not" else INT
                self._expect(value, expected, node, f"{operator} has an incompatible operand")
                return expected
            return self._infer(children[0]) if children else None
        if name in {"conditionalor", "conditionaland"}:
            if len(children) == 1:
                return self._infer(children[0])
            values = [self._infer(child) for child in children]
            for value in values:
                self._expect(value, BOOL, node, "logical operators require bool operands")
            return BOOL
        if name.startswith("relation_"):
            return self._infer(children[0]) if children else None
        if name == "relation" and len(children) == 1:
            return self._infer(children[0])
        if name == "relation" and len(children) == 2:
            left = self._infer(children[0])
            right = self._infer(children[1])
            operator = str(children[0].data)
            if operator in {"relation_eq", "relation_ne"}:
                valid = self._compatible(left, right)
            else:
                valid = self._ordered(left) and self._compatible(left, right)
            if not valid:
                self._error(node, "comparison operands have incompatible types")
            return BOOL
        if name.startswith("addition_") or name.startswith("multiplication_"):
            return self._infer(children[0]) if children else None
        if name in {"addition", "multiplication"} and len(children) == 1:
            return self._infer(children[0])
        if name in {"addition", "multiplication"} and len(children) == 2:
            left = self._infer(children[0])
            right = self._infer(children[1])
            operator = str(children[0].data)
            allowed = {CelKind.INT, CelKind.DOUBLE}
            if operator == "addition_add":
                allowed |= {CelKind.STRING, CelKind.LIST}
            if not self._compatible(left, right) or left is None or left.kind not in allowed:
                self._error(node, "arithmetic operands have incompatible types")
                return None
            return left
        if name == "ident":
            token = self._token(node)
            result = self.variables.get(str(token))
            if result is None:
                suggestion = self._suggest(str(token), self.variables)
                self._error(node, f"undefined identifier {str(token)!r}", suggestion=suggestion)
            return result
        if name == "literal":
            token = self._token(node)
            if token.type == "BOOL_LIT":
                return BOOL
            if token.type == "INT_LIT":
                return INT
            if token.type == "FLOAT_LIT":
                return DOUBLE
            if token.type == "STRING_LIT":
                return STRING
            self._error(node, f"unsupported CEL literal {str(token)!r}")
            return None
        if name == "member_dot":
            receiver = self._infer(children[0])
            field_token = next(child for child in node.children if isinstance(child, Token))
            field_name = str(field_token)
            if receiver is None:
                return None
            if receiver.kind != CelKind.OBJECT:
                self._error(node, f"cannot select field {field_name!r} on {receiver.kind.value}")
                return None
            if field_name not in receiver.fields:
                suggestion = self._suggest(field_name, receiver.fields)
                self._error(
                    node,
                    f"undefined field {field_name!r}",
                    token=field_token,
                    suggestion=suggestion,
                )
                return None
            return receiver.fields[field_name]
        if name == "member_dot_arg":
            receiver = self._infer(children[0])
            method = str(next(child for child in node.children if isinstance(child, Token)))
            arguments = self._arguments(node)
            if method in _ITERATION_MACROS:
                self._error(
                    node,
                    f"CEL iteration macro {method!r} exceeds the static cost policy",
                    "LLMF413",
                )
                return None
            return self._method_type(receiver, method, arguments, node)
        if name == "ident_arg":
            function = str(next(child for child in node.children if isinstance(child, Token)))
            arguments = self._arguments(node)
            if function == "size" and len(arguments) == 1:
                if arguments[0] is None or arguments[0].kind not in {
                    CelKind.LIST,
                    CelKind.MAP,
                    CelKind.STRING,
                }:
                    self._error(node, "size() requires a list, map, or string")
                return INT
            if function == "has" and len(arguments) == 1:
                return BOOL
            self._error(node, f"unsupported CEL function {function!r}")
            return None
        if name == "list_lit":
            arguments = self._arguments(node)
            if not arguments:
                self._error(node, "empty list literals have no static element type")
                return None
            if not all(self._compatible(arguments[0], item) for item in arguments[1:]):
                self._error(node, "list literal elements must have one type")
            return list_of(arguments[0]) if arguments[0] is not None else None
        if name == "map_lit":
            arguments = self._arguments(node)
            if not arguments or len(arguments) % 2:
                self._error(node, "map literal must contain typed key/value pairs")
                return None
            keys, values = arguments[::2], arguments[1::2]
            if not all(self._compatible(keys[0], item) for item in keys[1:]) or not all(
                self._compatible(values[0], item) for item in values[1:]
            ):
                self._error(node, "map literal entries must have consistent types")
            if keys[0] is None or values[0] is None:
                return None
            return map_of(keys[0], values[0])
        if name == "member_index":
            receiver = self._infer(children[0])
            index = self._infer(children[1])
            if receiver is None:
                return None
            if receiver.kind == CelKind.LIST:
                self._expect(index, INT, node, "list index must be int")
                return receiver.item
            if receiver.kind == CelKind.MAP:
                self._expect(index, receiver.key, node, "map index has incompatible type")
                return receiver.value
            self._error(node, "indexing is supported only for lists and maps")
            return None
        self._error(node, f"unsupported CEL construct {name!r}")
        return None

    def _method_type(
        self,
        receiver: CelType | None,
        method: str,
        arguments: list[CelType | None],
        node: Tree,
    ) -> CelType | None:
        if method == "size" and not arguments:
            if receiver is None or receiver.kind not in {CelKind.LIST, CelKind.MAP, CelKind.STRING}:
                self._error(node, "size() requires a list, map, or string")
            return INT
        if method == "contains" and len(arguments) == 1:
            if receiver is None or receiver.kind not in {CelKind.LIST, CelKind.STRING}:
                self._error(node, "contains() requires a list or string")
            elif receiver.kind == CelKind.LIST:
                self._expect(
                    arguments[0], receiver.item, node, "contains() argument has wrong type"
                )
            else:
                self._expect(arguments[0], STRING, node, "contains() argument must be string")
            return BOOL
        if method in {"endsWith", "matches", "startsWith"} and len(arguments) == 1:
            self._expect(receiver, STRING, node, f"{method}() receiver must be string")
            self._expect(arguments[0], STRING, node, f"{method}() argument must be string")
            return BOOL
        self._error(node, f"unsupported CEL method {method!r}")
        return None

    def _arguments(self, node: Tree) -> list[CelType | None]:
        expression_list = next(
            (
                child
                for child in node.children
                if isinstance(child, Tree) and str(child.data) in {"exprlist", "mapinits"}
            ),
            None,
        )
        if expression_list is None:
            return []
        return [self._infer(child) for child in expression_list.children if isinstance(child, Tree)]

    @staticmethod
    def _token(node: Tree) -> Token:
        return next(child for child in node.children if isinstance(child, Token))

    @staticmethod
    def _compatible(left: CelType | None, right: CelType | None) -> bool:
        if left is None or right is None:
            return False
        if left == right:
            return True
        return {left.kind, right.kind} == {CelKind.INT, CelKind.DOUBLE}

    @staticmethod
    def _ordered(value: CelType | None) -> bool:
        return value is not None and value.kind in {CelKind.INT, CelKind.DOUBLE, CelKind.STRING}

    def _expect(
        self,
        actual: CelType | None,
        expected: CelType | None,
        node: Tree,
        message: str,
    ) -> None:
        if expected is not None and not self._compatible(actual, expected):
            self._error(node, message)

    @staticmethod
    def _suggest(name: str, choices: object) -> str | None:
        matches = difflib.get_close_matches(name, list(choices), n=1, cutoff=0.5)
        return matches[0] if matches else None

    def _position(self, line: int, column: int) -> Position:
        file = self.source.file
        if line == 1:
            try:
                yaml_line = file.read_text(encoding="utf-8").splitlines()[self.source.line - 1]
                expression_column = yaml_line.find(self.expression)
            except (OSError, IndexError):
                expression_column = -1
            if expression_column >= 0:
                return Position(file, self.source.line, expression_column + column)
        return Position(file, self.source.line + line - 1, self.source.column + column - 1)

    def _error(
        self,
        node: Tree,
        message: str,
        code: str = "LLMF411",
        *,
        token: Token | None = None,
        suggestion: str | None = None,
    ) -> None:
        line = (token.line if token is not None else node.meta.line) or 1
        column = (token.column if token is not None else node.meta.column) or 1
        self.diagnostics.append(
            Diagnostic(
                code,
                Severity.ERROR,
                message,
                self._position(line, column),
                suggestion=suggestion,
            )
        )


def compile_expression(
    expression: str,
    environment: HookEnvironment,
    position: Position,
) -> CompiledExpression:
    parser = Environment()
    try:
        ast = parser.compile(expression)
    except CELParseError as exc:
        line = getattr(exc, "line", 1) or 1
        column = getattr(exc, "column", 1) or 1
        checker = ExpressionTypeChecker(environment.variables, position, expression)
        diagnostic = Diagnostic(
            "LLMF410",
            Severity.ERROR,
            "invalid CEL expression",
            checker._position(line, column),
            str(exc).splitlines()[0],
        )
        return CompiledExpression(None, None, [diagnostic])
    checker = ExpressionTypeChecker(environment.variables, position, expression)
    result_type = checker.check(ast)
    if result_type is not None and result_type != BOOL:
        checker.diagnostics.append(
            Diagnostic(
                "LLMF412",
                Severity.ERROR,
                f"policy rule must evaluate to bool, not {result_type.kind.value}",
                checker._position(ast.meta.line, ast.meta.column),
            )
        )
    return CompiledExpression(ast, result_type, checker.diagnostics)


def expression_identifiers(expression: str) -> set[str]:
    """Return root identifiers from a syntactically valid CEL expression."""

    try:
        tree = Environment().compile(expression)
    except CELParseError:
        return set()
    return {
        str(node.children[0])
        for node in tree.find_data("ident")
        if node.children and isinstance(node.children[0], Token)
    }


def expression_data_classes(expression: str) -> set[str]:
    """Return literal class names used in ``data.classes.contains(...)`` calls."""

    try:
        tree = Environment().compile(expression)
    except CELParseError:
        return set()
    classes: set[str] = set()
    for call in tree.find_data("member_dot_arg"):
        token = next((child for child in call.children if isinstance(child, Token)), None)
        receiver = next((child for child in call.children if isinstance(child, Tree)), None)
        if (
            token is None
            or str(token) != "contains"
            or _member_path(receiver) != ("data", "classes")
        ):
            continue
        expression_list = next(
            (
                child
                for child in call.children
                if isinstance(child, Tree) and str(child.data) == "exprlist"
            ),
            None,
        )
        if expression_list is None or len(expression_list.children) != 1:
            continue
        literals = list(expression_list.children[0].find_data("literal"))
        if len(literals) != 1 or not literals[0].children:
            continue
        literal = literals[0].children[0]
        if isinstance(literal, Token) and literal.type == "STRING_LIT":
            try:
                value = ast.literal_eval(str(literal))
            except (SyntaxError, ValueError):
                continue
            if isinstance(value, str):
                classes.add(value)
    return classes


def _member_path(node: Tree | None) -> tuple[str, ...]:
    if node is None:
        return ()
    name = str(node.data)
    tree_children = [child for child in node.children if isinstance(child, Tree)]
    if name in _WRAPPERS and tree_children:
        return _member_path(tree_children[0])
    if name == "ident" and node.children:
        return (str(node.children[0]),)
    if name == "member_dot" and tree_children:
        token = next((child for child in node.children if isinstance(child, Token)), None)
        return (*_member_path(tree_children[0]), str(token)) if token is not None else ()
    return ()


def validate_policy_rules(document: object) -> list[Diagnostic]:
    """Compile each attached policy against every hook context it can encounter."""

    # Import here keeps the compiler's public expression API independent of config loading.
    from llmform.config.loader import ConfigDocument

    if not isinstance(document, ConfigDocument) or document.config is None:
        return []
    config = document.config
    catalog = SchemaCatalog(document.root)
    diagnostics: list[Diagnostic] = []
    for agent_name, agent in config.agents.items():
        for policy_reference in agent.policies:
            policy_name = policy_reference.removeprefix("policy.")
            policy = config.policies.get(policy_name)
            if policy is None or policy.rule is None:
                continue
            hook = Hook(policy.on)
            tool_names: list[str | None] = [None]
            if hook in {Hook.TOOL_CALL, Hook.TOOL_RESULT}:
                tool_names = []
                for tool_reference in agent.tools:
                    candidate = tool_reference.removeprefix("tool.")
                    tool = config.tools.get(candidate)
                    if tool is None:
                        continue
                    match = policy.match
                    if match is not None and match.tool is not None:
                        if candidate != match.tool.removeprefix("tool."):
                            continue
                    if match is not None and match.source is not None:
                        if tool.source.removeprefix("source.") != match.source.removeprefix(
                            "source."
                        ):
                            continue
                    if match is not None and match.operation is not None:
                        if tool.operation != match.operation:
                            continue
                    tool_names.append(candidate)
            for tool_name in tool_names:
                environment, environment_diagnostics = build_hook_environment(
                    document,
                    agent_name,
                    hook,
                    tool_name=tool_name,
                    catalog=catalog,
                )
                diagnostics.extend(environment_diagnostics)
                if environment is None:
                    continue
                compiled = compile_expression(
                    policy.rule,
                    environment,
                    document.position(("policies", policy_name, "rule")),
                )
                for diagnostic in compiled.diagnostics:
                    context = f"on agent.{agent_name}"
                    if tool_name is not None:
                        tool = config.tools[tool_name]
                        schema = tool.input if hook == Hook.TOOL_CALL else tool.output
                        if hook == Hook.TOOL_RESULT and schema is None:
                            source = config.sources.get(tool.source.removeprefix("source."))
                            operation = (
                                source.operations.get(tool.operation)
                                if source is not None
                                else None
                            )
                            schema = operation.returns if operation is not None else None
                        context = f"on tool.{tool_name}"
                        if schema:
                            context += f" (schema: {Path(schema).name})"
                    diagnostics.append(
                        Diagnostic(
                            diagnostic.code,
                            diagnostic.severity,
                            diagnostic.message,
                            diagnostic.position,
                            diagnostic.hint,
                            diagnostic.suggestion,
                            context,
                        )
                    )
    return diagnostics
