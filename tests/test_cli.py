import json
import subprocess
from pathlib import Path
from time import perf_counter

from typer.testing import CliRunner

from llmform.cli import app
from llmform.config.loader import load_project
from llmform.diagnostics import Diagnostic, Severity
from llmform.lock import write_lock
from llmform.providers import (
    Completion,
    Provider,
    ProviderError,
    ProviderErrorKind,
    Usage,
)
from llmform.types import Message
from tests.test_config import VALID, write_config

runner = CliRunner()


def test_validate_command(tmp_path: Path) -> None:
    write_config(tmp_path)
    result = runner.invoke(app, ["validate", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "Valid:" in result.output


def test_validate_command_fails_for_unknown_field(tmp_path: Path) -> None:
    (tmp_path / "llmform.yaml").write_text("mdoels: {}\n", encoding="utf-8")
    result = runner.invoke(app, ["validate", str(tmp_path)])
    assert result.exit_code == 1
    assert "LLMF100" in result.output


def test_validate_json_uses_structured_diagnostic_envelope(tmp_path: Path) -> None:
    (tmp_path / "llmform.yaml").write_text("mdoels: {}\n", encoding="utf-8")
    result = runner.invoke(app, ["validate", str(tmp_path), "--json"])
    payload = json.loads(result.output)
    assert result.exit_code == 1
    assert payload["diagnostics"][0]["code"] == "LLMF100"
    assert payload["summary"] == {
        "errors": 1,
        "warnings": 0,
        "total": 1,
        "shown": 1,
        "omitted": 0,
    }


def test_validate_reports_malformed_variable_declarations_after_interpolation(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "llmform.yaml").write_text(
        "version: ${env.LLMFORM_VERSION}\nvariables: []\n", encoding="utf-8"
    )
    monkeypatch.setenv("LLMFORM_VERSION", "0.1")

    result = runner.invoke(app, ["validate", str(tmp_path), "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert any(item["code"] == "LLMF100" for item in payload["diagnostics"])


def test_validate_command_rejects_unsupported_policy_schema(tmp_path: Path) -> None:
    write_config(tmp_path)
    (tmp_path / "schemas/input.json").write_text(
        '{"oneOf": [{"type": "string"}, {"type": "integer"}]}', encoding="utf-8"
    )
    result = runner.invoke(app, ["validate", str(tmp_path)])
    assert result.exit_code == 1
    assert "LLMF400" in result.output
    assert "oneOf" in result.output


def test_validate_command_reports_warning_without_failing(tmp_path: Path) -> None:
    config = VALID.replace("method: GET", "method: POST").replace(
        "    input: schemas/input.json\n",
        "    input: schemas/input.json\n    detokenize: [query]\n",
    )
    write_config(tmp_path, config)
    result = runner.invoke(app, ["validate", str(tmp_path)])
    assert result.exit_code == 0
    assert "warning [LLMF507]" in result.output


def test_validate_locked_reports_lockfile_drift(tmp_path: Path) -> None:
    write_config(tmp_path)
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "support.md").write_text("Support", encoding="utf-8")
    write_lock(load_project(tmp_path))
    clean = runner.invoke(app, ["validate", str(tmp_path), "--locked"])
    assert clean.exit_code == 0, clean.output

    write_config(tmp_path, VALID.replace('rule: "true"', 'rule: "false"'))
    drifted = runner.invoke(app, ["validate", str(tmp_path), "--locked"])
    assert drifted.exit_code == 1
    assert "LLMF102" in drifted.output
    assert "changed sections: closure_sha256, files, policies" in drifted.output


def test_lock_command_writes_a_lockfile(tmp_path: Path) -> None:
    write_config(tmp_path)
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "support.md").write_text("Support", encoding="utf-8")

    result = runner.invoke(app, ["lock", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "Wrote" in result.output
    assert (tmp_path / "llmform.lock").is_file()


def test_lock_command_resolves_declared_cli_variables(tmp_path: Path) -> None:
    config = VALID.replace(
        'version: "0.1"\n',
        'version: "0.1"\nvariables:\n  tokens:\n    type: integer\n    required: true\n',
    ).replace("    id: llama3.2\n", "    id: llama3.2\n    max_tokens: ${var.tokens}\n")
    write_config(tmp_path, config)
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "support.md").write_text("Support", encoding="utf-8")

    result = runner.invoke(app, ["lock", str(tmp_path), "--var", "tokens=512"])

    assert result.exit_code == 0, result.output
    assert (tmp_path / "llmform.lock").is_file()


def _commit_plan_baseline(project: Path, config: str = VALID) -> None:
    write_config(project, config)
    (project / "prompts").mkdir()
    (project / "prompts" / "support.md").write_text("Support", encoding="utf-8")
    locked = runner.invoke(app, ["lock", str(project)])
    assert locked.exit_code == 0, locked.output
    for arguments in (
        ["init", "--quiet"],
        ["config", "user.email", "llmform-tests@example.test"],
        ["config", "user.name", "llmform tests"],
        ["add", "--all"],
        ["commit", "--quiet", "-m", "baseline"],
    ):
        subprocess.run(
            ["git", *arguments],
            cwd=project,
            check=True,
            capture_output=True,
            text=True,
        )


def test_plan_reports_noop_against_locked_head(tmp_path: Path) -> None:
    _commit_plan_baseline(tmp_path)

    result = runner.invoke(app, ["plan", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "No changes from HEAD." in result.output
    assert "Declared cost impact:\n- none" in result.output


def test_plan_classifies_format_only_changes_as_cosmetic(tmp_path: Path) -> None:
    _commit_plan_baseline(tmp_path)
    (tmp_path / "llmform.yaml").write_text(VALID + "# formatting-only change\n", encoding="utf-8")

    result = runner.invoke(app, ["plan", str(tmp_path), "--json", "--fail-on-privilege"])

    payload = json.loads(result.output)
    assert result.exit_code == 0
    assert payload["summary"]["cosmetic_changes"] == 1
    assert payload["changes"][0]["classification"] == "cosmetic"


def test_plan_reports_an_invalid_baseline_ref(tmp_path: Path) -> None:
    _commit_plan_baseline(tmp_path)

    result = runner.invoke(app, ["plan", str(tmp_path), "--from", "not-a-ref"])

    assert result.exit_code == 1
    assert "baseline Git ref 'not-a-ref' is not valid or unavailable" in result.output


def test_plan_flags_granted_tool_as_privilege_change(tmp_path: Path) -> None:
    _commit_plan_baseline(tmp_path)
    config = VALID.replace(
        "policies:\n",
        "  write:\n"
        "    source: source.api\n"
        "    operation: search\n"
        "    description: Write\n"
        "    input: schemas/input.json\n"
        "policies:\n",
    )
    config = config.replace("    tools: [tool.search]", "    tools: [tool.search, tool.write]")
    (tmp_path / "llmform.yaml").write_text(config, encoding="utf-8")

    result = runner.invoke(app, ["plan", str(tmp_path), "--fail-on-privilege"])

    assert result.exit_code == 1
    assert "PRIVILEGE added agents.support.tools[tool.write]" in result.output
    assert "1 privilege" in result.output


def test_plan_reports_declared_price_delta_in_json(tmp_path: Path) -> None:
    baseline = VALID.replace(
        "    id: llama3.2\n",
        "    id: llama3.2\n    price: {input_per_mtok: 1.25, output_per_mtok: 5.0}\n",
    )
    _commit_plan_baseline(tmp_path, baseline)
    config = baseline.replace("input_per_mtok: 1.25", "input_per_mtok: 2.5")
    (tmp_path / "llmform.yaml").write_text(config, encoding="utf-8")

    result = runner.invoke(app, ["plan", str(tmp_path), "--json", "--fail-on-privilege"])

    payload = json.loads(result.output)
    assert result.exit_code == 1
    assert payload["summary"]["privilege_changes"] == 1
    assert payload["cost_impact"] == [
        "model.default input: USD 1.25 -> 2.5 per million tokens (+1.25)"
    ]


def test_plan_does_not_fail_for_a_declared_price_reduction(tmp_path: Path) -> None:
    baseline = VALID.replace(
        "    id: llama3.2\n",
        "    id: llama3.2\n    price: {input_per_mtok: 1.25, output_per_mtok: 5.0}\n",
    )
    _commit_plan_baseline(tmp_path, baseline)
    config = baseline.replace("input_per_mtok: 1.25", "input_per_mtok: 0.5")
    (tmp_path / "llmform.yaml").write_text(config, encoding="utf-8")

    result = runner.invoke(app, ["plan", str(tmp_path), "--json", "--fail-on-privilege"])

    payload = json.loads(result.output)
    assert result.exit_code == 0
    assert payload["summary"]["privilege_changes"] == 0
    assert payload["summary"]["behavioral_changes"] == 1


def test_plan_reports_increased_run_budget(tmp_path: Path) -> None:
    baseline = VALID.replace(
        "    id: llama3.2\n",
        "    id: llama3.2\n    price: {input_per_mtok: 1.25, output_per_mtok: 5.0}\n",
    ).replace("    tools: [tool.search]\n", "    tools: [tool.search]\n    max_cost_usd: 0.5\n")
    _commit_plan_baseline(tmp_path, baseline)
    config = baseline.replace("max_cost_usd: 0.5", "max_cost_usd: 1.0")
    (tmp_path / "llmform.yaml").write_text(config, encoding="utf-8")

    result = runner.invoke(app, ["plan", str(tmp_path), "--fail-on-privilege"])

    assert result.exit_code == 1
    assert "PRIVILEGE changed agents.support.max_cost_usd" in result.output
    assert "agent.support max_cost_usd: $0.5 -> $1 per run" in result.output


def test_plan_never_renders_resolved_secret_values(tmp_path: Path, monkeypatch) -> None:
    baseline = VALID.replace(
        "    type: ollama\n    endpoint: http://localhost:11434",
        "    type: openai\n    api_key: ${secret.LLMFORM_PLAN_KEY_OLD}",
    )
    monkeypatch.setenv("LLMFORM_PLAN_KEY_OLD", "old-secret-value")
    monkeypatch.setenv("LLMFORM_PLAN_KEY_NEW", "new-secret-value")
    _commit_plan_baseline(tmp_path, baseline)
    current = baseline.replace("secret.LLMFORM_PLAN_KEY_OLD", "secret.LLMFORM_PLAN_KEY_NEW")
    (tmp_path / "llmform.yaml").write_text(current, encoding="utf-8")

    result = runner.invoke(app, ["plan", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "PRIVILEGE changed providers.local.api_key" in result.output
    assert "old-secret-value" not in result.output
    assert "new-secret-value" not in result.output


def test_init_creates_a_valid_locked_project(tmp_path: Path) -> None:
    project = tmp_path / "project"

    result = runner.invoke(app, ["init", str(project)])

    assert result.exit_code == 0, result.output
    assert (project / "llmform.yaml").is_file()
    assert (project / "prompts" / "assistant.md").is_file()
    assert (project / "llmform.lock").is_file()
    validation = runner.invoke(app, ["validate", str(project), "--locked"])
    assert validation.exit_code == 0, validation.output


def test_fmt_is_idempotent_on_initialized_project(tmp_path: Path) -> None:
    project = tmp_path / "project"
    assert runner.invoke(app, ["init", str(project)]).exit_code == 0

    first = runner.invoke(app, ["fmt", str(project)])
    second = runner.invoke(app, ["fmt", str(project)])

    assert first.exit_code == second.exit_code == 0
    assert first.output == second.output == "Formatted 0 file(s)\n"


def test_validate_online_delegates_to_nonexecuting_l3_checks(tmp_path: Path, monkeypatch) -> None:
    write_config(tmp_path)
    called = False

    def check_online(document):
        nonlocal called
        called = True
        return []

    monkeypatch.setattr("llmform.cli.validate_online", check_online)
    result = runner.invoke(app, ["validate", str(tmp_path), "--online"])

    assert result.exit_code == 0, result.output
    assert called


def test_validate_offline_completes_within_precommit_budget(tmp_path: Path) -> None:
    write_config(tmp_path)

    started = perf_counter()
    result = runner.invoke(app, ["validate", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert perf_counter() - started < 2


def test_validate_scrubs_secrets_from_human_and_json_diagnostics(
    tmp_path: Path, monkeypatch
) -> None:
    secret = "never-print-this-secret"
    write_config(tmp_path, VALID.replace("http://localhost:11434", "${secret.API_TOKEN}"))
    monkeypatch.setenv("API_TOKEN", secret)

    def diagnostic_with_secret(document):
        return [
            Diagnostic(
                "LLMF999",
                Severity.ERROR,
                f"invalid provider value {document.config.providers['local'].endpoint}",
                document.position(("providers", "local", "endpoint")),
            )
        ]

    monkeypatch.setattr("llmform.cli.validate_semantics", diagnostic_with_secret)

    human = runner.invoke(app, ["validate", str(tmp_path)])
    structured = runner.invoke(app, ["validate", str(tmp_path), "--json"])

    assert human.exit_code == structured.exit_code == 1
    assert secret not in human.output
    assert secret not in structured.output
    assert "[REDACTED]" in human.output
    assert "[REDACTED]" in structured.output


def test_validate_scrubs_json_escaped_secret_before_serialization(
    tmp_path: Path, monkeypatch
) -> None:
    secret = 'never-print-"this-secret'
    config = VALID.replace(
        "    id: llama3.2", "    id: llama3.2\n    max_tokens: ${secret.API_TOKEN}"
    )
    write_config(tmp_path, config)
    monkeypatch.setenv("API_TOKEN", secret)

    result = runner.invoke(app, ["validate", str(tmp_path), "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert secret not in result.output
    assert secret not in payload["diagnostics"][0]["message"]
    assert "[REDACTED]" in payload["diagnostics"][0]["message"]


def test_validate_scrubs_secret_from_unexpected_error(tmp_path: Path, monkeypatch) -> None:
    secret = "never-print-this-secret"
    write_config(tmp_path, VALID.replace("http://localhost:11434", "${secret.API_TOKEN}"))
    monkeypatch.setenv("API_TOKEN", secret)

    def fail_validation(document):
        raise RuntimeError(f"provider rejected {document.config.providers['local'].endpoint}")

    monkeypatch.setattr("llmform.cli.validate_semantics", fail_validation)
    result = runner.invoke(app, ["validate", str(tmp_path)])

    assert result.exit_code == 1
    assert secret not in result.output
    assert "provider rejected [REDACTED]" in result.output


def test_validate_accepts_typed_cli_variable_interpolation(tmp_path: Path) -> None:
    config = VALID.replace(
        'version: "0.1"\n',
        'version: "0.1"\nvariables:\n  retries:\n    type: integer\n    required: true\n',
    ).replace("    id: llama3.2\n", "    id: llama3.2\n    max_tokens: ${var.retries}\n")
    write_config(tmp_path, config)

    result = runner.invoke(app, ["validate", str(tmp_path), "--var", "retries=3"])

    assert result.exit_code == 0, result.output
    assert "Valid:" in result.output


OPENAI_PROJECT = """\
version: "0.1"
providers:
  openai:
    type: openai
    api_key: ${secret.LLMFORM_TEST_OPENAI_KEY}
models:
  default: {provider: provider.openai, id: gpt-test}
agents:
  assistant:
    model: model.default
    instructions: prompts/assistant.md
"""
SECRET = "sk-test-secret-value-1234"


class RecordingOpenAI(Provider):
    instances: list["RecordingOpenAI"] = []

    def __init__(self, api_key: str, endpoint: str, *, error: ProviderError | None = None):
        self.api_key, self.endpoint, self.error = api_key, endpoint, error
        self.messages: list[Message] = []
        RecordingOpenAI.instances.append(self)

    def complete(self, messages, *, model: str, tools, output_schema=None) -> Completion:
        if self.error:
            raise self.error
        self.messages = list(messages)
        return Completion(
            message=Message(role="assistant", content="PONG"),
            usage=Usage(input_tokens=1, output_tokens=1),
        )


def _openai_project(tmp_path: Path) -> Path:
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "assistant.md").write_text("Answer with PONG.", encoding="utf-8")
    (tmp_path / "llmform.yaml").write_text(OPENAI_PROJECT, encoding="utf-8")
    return tmp_path


def test_run_resolves_a_secret_api_key_without_printing_it(tmp_path: Path, monkeypatch) -> None:
    RecordingOpenAI.instances.clear()
    monkeypatch.setenv("LLMFORM_TEST_OPENAI_KEY", SECRET)
    monkeypatch.setattr("llmform.cli.OpenAIProvider", RecordingOpenAI)
    project = _openai_project(tmp_path)

    result = runner.invoke(app, ["run", "assistant", "--path", str(project), "--input", "ping"])

    assert result.exit_code == 0, result.output
    assert result.output.strip() == "PONG"
    assert SECRET not in result.output
    provider = RecordingOpenAI.instances[0]
    assert provider.api_key == SECRET
    assert provider.messages[0] == Message(role="system", content="Answer with PONG.")
    assert provider.messages[1] == Message(role="user", content="ping")


def test_run_fails_safely_when_the_secret_is_absent(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("LLMFORM_TEST_OPENAI_KEY", raising=False)
    project = _openai_project(tmp_path)

    result = runner.invoke(app, ["run", "assistant", "--path", str(project), "--input", "ping"])

    assert result.exit_code == 1
    assert "LLMF200" in result.output
    assert "secret.LLMFORM_TEST_OPENAI_KEY" in result.output


def test_run_reports_provider_errors_with_the_secret_scrubbed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LLMFORM_TEST_OPENAI_KEY", SECRET)
    error = ProviderError(ProviderErrorKind.AUTH, f"rejected key {SECRET}")
    monkeypatch.setattr(
        "llmform.cli.OpenAIProvider",
        lambda key, endpoint: RecordingOpenAI(key, endpoint, error=error),
    )
    project = _openai_project(tmp_path)

    result = runner.invoke(app, ["run", "assistant", "--path", str(project), "--input", "ping"])

    assert result.exit_code == 1
    assert "provider error (auth)" in result.output
    assert SECRET not in result.output


def test_run_refuses_a_project_with_error_findings(tmp_path: Path) -> None:
    write_config(tmp_path, VALID.replace("source: source.api", "source: source.missing"))

    result = runner.invoke(app, ["run", "support", "--path", str(tmp_path), "--input", "hi"])

    assert result.exit_code == 1
    assert "LLMF300" in result.output
