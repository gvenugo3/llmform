"""Typed source operations."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class SourceError(RuntimeError):
    pass


class Source(ABC):
    @abstractmethod
    def execute(self, operation: str, params: dict[str, Any], *, timeout: float) -> Any: ...
