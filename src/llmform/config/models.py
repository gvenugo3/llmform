from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationInfo, field_validator

from llmform.diagnostics import Position

Scalar = str | int | float | bool


@dataclass(frozen=True)
class FieldLocation:
    key: Position
    value: Position


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, validate_default=True)

    _field_locations: dict[str, FieldLocation] = PrivateAttr(default_factory=dict)

    def field_location(self, name: str) -> FieldLocation | None:
        """Return the declaration locations attached by the positioned loader."""

        return self._field_locations.get(name)

    def field_position(self, name: str, *, key: bool = False) -> Position | None:
        location = self.field_location(name)
        if location is None:
            return None
        return location.key if key else location.value

    def _set_field_location(self, name: str, location: FieldLocation) -> None:
        self._field_locations[name] = location


class Variable(StrictModel):
    type: Literal["string", "integer", "number", "boolean"] = "string"
    default: Scalar | None = None
    description: str | None = None
    required: bool = False

    @field_validator("default")
    @classmethod
    def validate_default_type(cls, value: Scalar | None, info: ValidationInfo) -> Scalar | None:
        if value is None:
            return value
        declared_type = info.data.get("type", "string")
        valid = {
            "string": type(value) is str,
            "integer": type(value) is int,
            "number": type(value) in {int, float},
            "boolean": type(value) is bool,
        }[declared_type]
        if not valid:
            raise ValueError(f"default must have declared type {declared_type}")
        return value


class Provider(StrictModel):
    type: Literal["openai", "ollama"]
    api_key: str | None = None
    endpoint: str | None = None


class Price(StrictModel):
    input_per_mtok: float
    output_per_mtok: float
    currency: str = "USD"


class Model(StrictModel):
    provider: str
    id: str
    temperature: float | None = None
    max_tokens: int | None = Field(default=None, gt=0)
    price: Price | None = None


class OperationParameter(StrictModel):
    type: Literal["string", "integer", "number", "boolean", "object", "array"]
    required: bool = False


class HttpOperation(StrictModel):
    method: Literal["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"]
    path: str
    params: dict[str, OperationParameter] = Field(default_factory=dict)
    returns: str
    classes: list[str] = Field(default_factory=list)


class McpOperation(StrictModel):
    params: dict[str, OperationParameter] = Field(default_factory=dict)
    returns: str
    classes: list[str] = Field(default_factory=list)
    read_only: bool | None = None


class HttpAuth(StrictModel):
    type: Literal["bearer", "basic"]
    token: str | None = None
    username: str | None = None
    password: str | None = None


class HttpSource(StrictModel):
    type: Literal["http"]
    base_url: str
    auth: HttpAuth | None = None
    operations: dict[str, HttpOperation]


class McpSource(StrictModel):
    type: Literal["mcp"]
    command: list[str]
    # MCP catalogs must be checked in so offline validation never executes a server.
    operations: dict[str, McpOperation]


Source = Annotated[HttpSource | McpSource, Field(discriminator="type")]


class Tool(StrictModel):
    source: str
    operation: str
    description: str
    input: str
    output: str | None = None
    timeout: str = "30s"
    retries: int = Field(default=0, ge=0)
    detokenize: list[str] = Field(default_factory=list)


class Match(StrictModel):
    tool: str | None = None
    source: str | None = None
    operation: str | None = None
    model: str | None = None
    provider: str | None = None


class Transform(StrictModel):
    kind: Literal["redact", "tokenize"]
    fields: list[str]


class Approval(StrictModel):
    approvers: str = Field(min_length=1)


class Policy(StrictModel):
    on: Literal["request", "model_call", "tool_call", "tool_result", "response", "loop"]
    match: Match | None = None
    rule: str | None = None
    otherwise: Literal["ALLOW", "DENY", "REQUIRE_APPROVAL"] = "DENY"
    transform: list[Transform] = Field(default_factory=list)
    approval: Approval | None = None


class Agent(StrictModel):
    model: str
    instructions: str
    input: str | None = None
    output: str | None = None
    tools: list[str] = Field(default_factory=list)
    policies: list[str] = Field(default_factory=list)
    max_iterations: int = Field(default=8, gt=0)
    max_cost_usd: float | None = Field(default=None, gt=0)
    max_elapsed: str | None = None
    default_tool_posture: Literal["allow", "deny"] = "allow"


class Audit(StrictModel):
    record_payloads: bool = False


class ProjectConfig(StrictModel):
    version: str = "0.1"
    variables: dict[str, Variable] = Field(default_factory=dict)
    providers: dict[str, Provider] = Field(default_factory=dict)
    models: dict[str, Model] = Field(default_factory=dict)
    sources: dict[str, Source] = Field(default_factory=dict)
    tools: dict[str, Tool] = Field(default_factory=dict)
    policies: dict[str, Policy] = Field(default_factory=dict)
    agents: dict[str, Agent] = Field(default_factory=dict)
    audit: Audit = Field(default_factory=Audit)
