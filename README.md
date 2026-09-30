# llmform

[![CI](https://github.com/gvenugo3/llmform/actions/workflows/ci.yml/badge.svg)](https://github.com/gvenugo3/llmform/actions/workflows/ci.yml)

`llmform` declares an LLM application—models, data access, tools, and enforceable
runtime policies—in YAML. The current target is the accepted [v0.1
specification](docs/SPEC.md).

The implementation uses Python 3.12 or newer. v0.1 provides an offline-first workflow:

- Positioned YAML, deterministic discovery/merge, strict Pydantic configuration,
  packaged JSON Schemas, interpolation with secret scrubbing, reference checks, CEL
  policy compilation, semantic validation, and structured diagnostics.
- `init`, `fmt`, `validate`, and `lock`, including closure-lock verification with
  `validate --locked`.
- Runtime policy enforcement at model, tool, response, and loop hooks; transforms,
  approvals, data classes, declared-price cost ceilings, and a serializable run state.
- Chained, tamper-evident audit records that avoid plaintext payload and token leakage.
- OpenAI Responses and Ollama providers, typed HTTP sources, and MCP stdio sources with
  an explicit argv-lock trust boundary.
- `run <agent>` for local execution, including input flags/files/stdin, a local principal,
  interactive approvals, `--json`, and `--trust-sources` for reviewed MCP commands.

The accepted [v0.1 specification](docs/SPEC.md) is normative; the other design documents
are retained as explicitly labeled historical context.

## Development

Create an environment and install the package:

```shell
python -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
```

Run the primary workflows and tests:

```shell
llmform init demo
llmform validate demo
llmform lock demo
llmform validate --locked demo
llmform run assistant --path demo --input 'Summarize the request.'
python -m pytest -q
```

`run` denies policy-blocked actions in the runtime, not through prompt cooperation. It
writes chained audit records to `llmform.audit.jsonl` in the project directory. MCP
sources require their argv to be present in `llmform.lock`; use `--trust-sources` only
after reviewing an unrecorded or changed command.

Tests collect branch coverage for `llmform` and fail below 80%. CI publishes the
Python 3.13 `coverage.xml` report as a downloadable workflow artifact.

Configuration discovery uses the nearest parent containing either `llmform.yaml` or a
`*.llmform.yaml` fragment. It loads the primary file first, then fragments in lexical
order. Fragments-only projects are valid. Duplicate resource addresses are errors; load
order never overrides a resource silently.

## OpenAI integration test

The default test suite and pull-request CI stay offline: they need no provider
credentials, network access, or MCP processes. A separate, opt-in suite in
`tests/integration/` calls the live OpenAI Responses API. It checks that `llmform run`
resolves `${secret.OPENAI_API_KEY}`, completes a deterministic prompt, and writes an
audit record without the key. It also checks a tool round trip in which tokenized values
never reach the model.

### Configure it (maintainers, once)

1. In **Settings → Environments**, create an environment named `e2`.
   Add required reviewers or restrict it to `main` if you want an approval gate.
2. Add a secret named `OPENAI_API_KEY` to that environment, or to the repository. Use a
   key from a project with a low spending limit.

### Run it

In **Actions → OpenAI integration → Run workflow**, choose a branch, optionally change
the model ID (default `gpt-4.1-mini`), and start the run. The workflow runs only on
manual dispatch, so pull-request code never runs with the secret. If the secret is
missing, the workflow fails at its first step with an explanation.

To run the suite locally:

```shell
export OPENAI_API_KEY=...            # never commit this value
LLMFORM_OPENAI_INTEGRATION=1 LLMFORM_OPENAI_MODEL=gpt-4.1-mini \
  python -m pytest tests/integration -m openai --no-cov -v
```

Without `LLMFORM_OPENAI_INTEGRATION=1` the suite is skipped. With it but without
`OPENAI_API_KEY`, the tests fail instead of skipping, so a configured run cannot pass
without calling the API. GitHub masks the secret in workflow logs, and the tests assert
that the key never appears in command output or audit records.
