# llmform

[![CI](https://github.com/gvenugo3/llmform/actions/workflows/ci.yml/badge.svg)](https://github.com/gvenugo3/llmform/actions/workflows/ci.yml)

`llmform` declares an LLM application—models, data access, tools, and enforceable
runtime policies—in YAML. The current target is the accepted [v0.1
specification](docs/SPEC.md).

The implementation uses Python 3.12 or newer. It is at the foundation stage: project
discovery, positioned YAML parsing, strict typed configuration, interpolation and secret
scrubbing, reference validation, structured diagnostics, and the offline `validate`
command are implemented.

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

Configuration discovery checks the nearest parent directory for `llmform.yaml` and
then loads any `*.llmform.yaml` fragments in lexical order. Duplicate resource addresses
are errors; load order never overrides a resource silently.
