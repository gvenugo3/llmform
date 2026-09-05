"""Regenerate the committed llmform configuration JSON Schemas."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import TypeAdapter

from llmform.config.models import (
    Agent,
    Model,
    Policy,
    ProjectConfig,
    Provider,
    Source,
    Tool,
    Variable,
)

SCHEMA_VERSION = "0.1"
OUTPUT = Path(__file__).parents[1] / "src" / "llmform" / "schemas"
MODELS = {
    "agent": Agent,
    "model": Model,
    "policy": Policy,
    "project": ProjectConfig,
    "provider": Provider,
    "source": Source,
    "tool": Tool,
    "variable": Variable,
}


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    for name, model in MODELS.items():
        schema = TypeAdapter(model).json_schema(mode="validation")
        schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": f"https://llmform.dev/schemas/v{SCHEMA_VERSION}/{name}.json",
            "x-llmform-schema-version": SCHEMA_VERSION,
            **schema,
        }
        (OUTPUT / f"{name}.json").write_text(
            json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )


if __name__ == "__main__":
    main()
