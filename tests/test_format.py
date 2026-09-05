from __future__ import annotations

from llmform.config.format import format_yaml


def test_format_preserves_comments_and_is_idempotent() -> None:
    source = "# heading\nproviders:\n  local: {type: ollama} # inline\n"

    formatted = format_yaml(source)

    assert "# heading" in formatted
    assert "# inline" in formatted
    assert format_yaml(formatted) == formatted
