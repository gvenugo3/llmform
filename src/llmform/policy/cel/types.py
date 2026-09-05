"""A small, concrete type algebra used to declare CEL environments."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class CelKind(StrEnum):
    BOOL = "bool"
    INT = "int"
    DOUBLE = "double"
    STRING = "string"
    DURATION = "duration"
    LIST = "list"
    MAP = "map"
    OBJECT = "object"


@dataclass(frozen=True)
class CelType:
    """A CEL type with no dynamic/unknown escape hatch."""

    kind: CelKind
    item: CelType | None = None
    key: CelType | None = None
    value: CelType | None = None
    fields: dict[str, CelType] = field(default_factory=dict)
    enum: tuple[str, ...] = ()

    def field(self, name: str) -> CelType:
        return self.fields[name]


BOOL = CelType(CelKind.BOOL)
INT = CelType(CelKind.INT)
DOUBLE = CelType(CelKind.DOUBLE)
STRING = CelType(CelKind.STRING)
DURATION = CelType(CelKind.DURATION)


def list_of(item: CelType) -> CelType:
    return CelType(CelKind.LIST, item=item)


def map_of(key: CelType, value: CelType) -> CelType:
    return CelType(CelKind.MAP, key=key, value=value)


def object_of(**fields: CelType) -> CelType:
    return CelType(CelKind.OBJECT, fields=fields)
