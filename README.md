# llmform

[![CI](https://github.com/gvenugo3/llmform/actions/workflows/ci.yml/badge.svg)](https://github.com/gvenugo3/llmform/actions/workflows/ci.yml)

`llmform` declares an LLM application—models, data access, tools, and enforceable
runtime policies—in YAML. The current target is the accepted [v0.1
specification](docs/SPEC.md).

The implementation uses Python 3.12 or newer. The offline validation foundation is in
place: project discovery and deterministic merging, positioned YAML, strict Pydantic
configuration, packaged JSON Schemas, interpolation and secret scrubbing, reference
validation, typed CEL environments and policy compilation, cross-resource L2 checks,
structured diagnostics, public run-state types, and the `validate` command.

Runtime policy enforcement, providers and sources, the resumable agent loop, lockfiles,
and the `init`, `fmt`, `lock`, and `run` commands remain roadmap work. The accepted
[v0.1 specification](docs/SPEC.md) is normative; the other design documents are retained
as explicitly labeled historical context.

## Development

Create an environment and install the package:

```shell
python -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
```

Run the validator and tests:

```shell
llmform validate path/to/project
pytest
```

Tests collect branch coverage for `llmform` and fail below 80%. CI publishes the
Python 3.13 `coverage.xml` report as a downloadable workflow artifact.

Configuration discovery uses the nearest parent containing either `llmform.yaml` or a
`*.llmform.yaml` fragment. It loads the primary file first, then fragments in lexical
order. Fragments-only projects are valid. Duplicate resource addresses are errors; load
order never overrides a resource silently.
