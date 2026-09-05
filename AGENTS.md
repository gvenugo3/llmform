# Repository Guidelines

## Project Structure & Module Organization

Python source lives under `src/llmform/`. The CLI entrypoint is `cli.py`; configuration
discovery, parsing, interpolation, models, and validation live in `src/llmform/config/`.
Keep reusable diagnostics in `diagnostics.py` and expose public APIs deliberately from
package `__init__.py` files. Packaged configuration schemas live in
`src/llmform/schemas/`; regenerate them with `scripts/generate_config_schemas.py`.

Tests live in `tests/` and should mirror the source area they exercise. The accepted
design is normative in `docs/SPEC.md`; `ARCHITECTURE.md`, `RUNTIME.md`, and
`DESIGN-REVIEW.md` are historical context. CI configuration is in
`.github/workflows/ci.yml`.

## Build, Test, and Development Commands

Use Python 3.12 or newer:

```shell
python -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
python -m llmform --help
```

Run `python -m ruff check src tests scripts` for linting and
`python -m ruff format src tests scripts` for formatting. Run `python -m pytest -q` for
tests and coverage. Use `python -m compileall -q src tests scripts` as a lightweight
syntax/package check.

## Coding Style & Naming Conventions

Follow Ruff’s configuration in `pyproject.toml`: four-space indentation, 100-character
lines, sorted imports, and Python 3.12 syntax. Use `snake_case` for functions and
modules, `PascalCase` for classes, and uppercase names for constants. Prefer typed,
strict Pydantic models over unstructured dictionaries. Diagnostics require stable
codes, exact source positions, and actionable messages.

## Testing Guidelines

Tests use pytest and follow `test_*.py` / `test_*` naming. Add focused tests for success,
failure, and source-position behavior. Branch coverage is enabled for `llmform`; the
suite fails below 80%. New diagnostics should receive golden-style output coverage as
that harness develops. Tests must not access providers, the network, or launch MCP
processes unless explicitly marked as online integration tests.

## Commit & Pull Request Guidelines

History uses short, imperative subjects such as `Revise SPEC.md` and `Initial Python
implementation`. Keep commits cohesive and avoid mixing unrelated cleanup. Pull
requests should explain behavior changes, link the relevant GitHub issue, identify spec
decisions, and include test and coverage results. Include sample CLI output when user-
visible diagnostics change.

## Security & Configuration

Never commit `.env` files, credentials, resolved secrets, token-vault plaintext, or
coverage artifacts. Validation is offline by default; do not introduce implicit network
access or subprocess execution. Treat tool detokenization, approval, and MCP trust as
explicit privilege boundaries.
