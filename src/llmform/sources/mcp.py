"""Stdio MCP client with an explicit argv-lock trust boundary."""

from __future__ import annotations

import json
import subprocess
from typing import Any

from llmform.sources import Source, SourceError


class McpSource(Source):
    def __init__(self, command: list[str], *, trusted: bool):
        if not trusted:
            raise SourceError("MCP argv is untrusted; run with --trust-sources after reviewing it")
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.ident = 0
        self.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "llmform", "version": "0.1"},
            },
        )

    def request(self, method: str, params: dict[str, Any]) -> Any:
        self.ident += 1
        assert self.process.stdin and self.process.stdout
        self.process.stdin.write(
            json.dumps({"jsonrpc": "2.0", "id": self.ident, "method": method, "params": params})
            + "\n"
        )
        self.process.stdin.flush()
        response = json.loads(self.process.stdout.readline())
        if "error" in response:
            raise SourceError(str(response["error"]))
        return response.get("result")

    def execute(self, operation: str, params: dict[str, Any], *, timeout: float) -> Any:
        return self.request("tools/call", {"name": operation, "arguments": params})

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=2)
