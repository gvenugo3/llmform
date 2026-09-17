"""Structural HTTP operation execution."""

from __future__ import annotations

import base64
import json
from typing import Any
from urllib.parse import quote, urlencode, urljoin
from urllib.request import Request, urlopen

from jsonschema import validate

from llmform.config.models import HttpSource
from llmform.sources import Source, SourceError


class HttpOperationSource(Source):
    def __init__(self, config: HttpSource, schemas: dict[str, dict[str, Any]]):
        self.config, self.schemas = config, schemas

    def execute(self, operation: str, params: dict[str, Any], *, timeout: float) -> Any:
        spec = self.config.operations[operation]
        path = spec.path
        for name, value in params.items():
            marker = "{" + name + "}"
            if marker in path:
                path = path.replace(marker, quote(str(value), safe=""))
        if "{" in path:
            raise SourceError("missing required path parameter")
        query = {
            name: value
            for name, value in params.items()
            if "{" + name + "}" not in spec.path and spec.method in {"GET", "HEAD"}
        }
        url = urljoin(self.config.base_url.rstrip("/") + "/", path.lstrip("/"))
        if query:
            url += "?" + urlencode(query, doseq=True)
        headers = {"Accept": "application/json"}
        if self.config.auth:
            if self.config.auth.type == "bearer":
                headers["Authorization"] = f"Bearer {self.config.auth.token or ''}"
            else:
                token = base64.b64encode(
                    f"{self.config.auth.username or ''}:{self.config.auth.password or ''}".encode()
                ).decode()
                headers["Authorization"] = f"Basic {token}"
        body = None
        if spec.method not in {"GET", "HEAD"}:
            body = json.dumps(
                {name: value for name, value in params.items() if "{" + name + "}" not in spec.path}
            ).encode()
            headers["Content-Type"] = "application/json"
        try:
            with urlopen(
                Request(url, data=body, headers=headers, method=spec.method), timeout=timeout
            ) as response:  # noqa: S310
                value = json.load(response)
        except OSError as exc:
            raise SourceError(str(exc)) from exc
        try:
            validate(value, self.schemas[spec.returns])
        except Exception as exc:
            raise SourceError(f"operation result violates returns schema: {exc}") from exc
        return value
