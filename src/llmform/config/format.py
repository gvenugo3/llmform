"""Comment-preserving canonical YAML formatting."""

from __future__ import annotations

from io import StringIO
from pathlib import Path

from ruamel.yaml import YAML


def format_yaml(text: str) -> str:
    """Return canonical YAML while retaining comments, ordering, and quote choices."""

    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    yaml.indent(mapping=2, sequence=4, offset=2)
    yaml.width = 100
    value = yaml.load(text)
    output = StringIO()
    yaml.dump(value, output)
    return output.getvalue()


def format_file(path: Path) -> bool:
    """Format one file in place and report whether it changed."""

    original = path.read_text(encoding="utf-8")
    formatted = format_yaml(original)
    if formatted == original:
        return False
    path.write_text(formatted, encoding="utf-8")
    return True
