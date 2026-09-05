"""Opt-in L3 reachability checks that never execute project commands."""

from __future__ import annotations

import shutil
from urllib.error import URLError
from urllib.request import Request, urlopen

from llmform.config.loader import ConfigDocument
from llmform.config.models import HttpSource, McpSource
from llmform.diagnostics import Diagnostic, Severity


def _reachable(url: str) -> str | None:
    try:
        with urlopen(Request(url, method="HEAD"), timeout=5):  # noqa: S310
            return None
    except (OSError, URLError) as exc:
        return str(exc)


def validate_online(document: ConfigDocument) -> list[Diagnostic]:
    """Check configured endpoints and MCP command resolution without execution."""

    if document.config is None:
        return []
    diagnostics: list[Diagnostic] = []
    for name, provider in document.config.providers.items():
        if provider.endpoint and (error := _reachable(provider.endpoint)):
            diagnostics.append(
                Diagnostic(
                    "LLMF600",
                    Severity.ERROR,
                    f"provider.{name} is unreachable: {error}",
                    document.position(("providers", name, "endpoint")),
                )
            )
    for name, source in document.config.sources.items():
        if isinstance(source, HttpSource) and (error := _reachable(source.base_url)):
            diagnostics.append(
                Diagnostic(
                    "LLMF600",
                    Severity.ERROR,
                    f"source.{name} is unreachable: {error}",
                    document.position(("sources", name, "base_url")),
                )
            )
        if isinstance(source, McpSource) and not shutil.which(source.command[0]):
            diagnostics.append(
                Diagnostic(
                    "LLMF600",
                    Severity.ERROR,
                    f"MCP command {source.command[0]!r} is not on PATH",
                    document.position(("sources", name, "command", 0)),
                )
            )
    return diagnostics
