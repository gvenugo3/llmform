import io
import json

import pytest

from llmform.config.models import HttpOperation, HttpSource
from llmform.sources import SourceError
from llmform.sources.http import HttpOperationSource
from llmform.sources.mcp import McpSource


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def source() -> HttpOperationSource:
    return HttpOperationSource(
        HttpSource(
            type="http",
            base_url="https://api.example.test",
            operations={
                "lookup": HttpOperation(
                    method="GET", path="items/{id}", params={}, returns="return.json"
                )
            },
        ),
        {"return.json": {"type": "object", "required": ["ok"]}},
    )


def test_http_source_quotes_path_and_validates_result(monkeypatch) -> None:
    seen = []
    monkeypatch.setattr(
        "llmform.sources.http.urlopen",
        lambda request, **_: (
            seen.append(request.full_url) or Response(json.dumps({"ok": True}).encode())
        ),
    )
    assert source().execute("lookup", {"id": "a/b"}, timeout=1) == {"ok": True}
    assert seen == ["https://api.example.test/items/a%2Fb"]


def test_http_source_rejects_nonconforming_result(monkeypatch) -> None:
    monkeypatch.setattr("llmform.sources.http.urlopen", lambda *_args, **_kwargs: Response(b"{}"))
    with pytest.raises(SourceError, match="violates returns schema"):
        source().execute("lookup", {"id": "one"}, timeout=1)


def test_untrusted_mcp_command_never_spawns(monkeypatch) -> None:
    monkeypatch.setattr(
        "llmform.sources.mcp.subprocess.Popen",
        lambda *_args, **_kwargs: pytest.fail("untrusted command was launched"),
    )
    with pytest.raises(SourceError, match="untrusted"):
        McpSource(["untrusted-server"], trusted=False)
