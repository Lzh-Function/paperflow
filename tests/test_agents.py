import json
import sys
from pathlib import Path

import pytest

from paperflow.agents.base import AnalysisRequest, ProviderError
from paperflow.agents.claude_cli import ClaudeCliProvider
from paperflow.agents.codex_cli import CodexCliProvider
from paperflow.agents.fake import FakeProvider
from paperflow.agents.process import run_bounded
from paperflow.agents.schema import SCHEMA, validate


@pytest.fixture
def request_data():
    return AnalysisRequest(
        "123", {"title": "Synthetic"}, "Synthetic abstract.", "abstract", [], ["other"]
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("priority", 6),
        ("priority", True),
        ("extra", "x"),
        ("tags", ["unregistered"]),
        ("coverage", "fulltext"),
        ("evidence", [{"claim": "x", "page": 99, "locator": None}]),
    ],
)
def test_common_schema_and_semantic_validation(request_data, field, value):
    data = FakeProvider().analyze(request_data).data
    data[field] = value
    with pytest.raises(ProviderError):
        validate(data, request_data)


def test_claude_uses_structured_output_and_no_tools(small_config, request_data, monkeypatch):
    small_config.analysis.model = "test-claude-model"
    data = FakeProvider().analyze(request_data).data
    monkeypatch.setattr("paperflow.agents.claude_cli.resolve_command", lambda _: "claude")
    monkeypatch.setattr(
        "paperflow.agents.claude_cli.checked_help",
        lambda *a: "--no-session-persistence --no-chrome",
    )

    def run(argv, **kwargs):
        assert argv[argv.index("--model") + 1] == "test-claude-model"
        assert argv[argv.index("--tools") + 1] == ""
        assert argv[argv.index("--disallowedTools") + 1] == "mcp__*"
        assert "--strict-mcp-config" in argv
        assert kwargs["cwd"].is_relative_to(small_config.storage.data_dir)
        assert "Synthetic abstract" in kwargs["text"]
        return json.dumps({"result": "NOT THE ANALYSIS", "structured_output": data})

    monkeypatch.setattr("paperflow.agents.claude_cli.run_bounded", run)
    assert ClaudeCliProvider(small_config).analyze(request_data).data == data
    monkeypatch.setattr(
        "paperflow.agents.claude_cli.run_bounded", lambda *a, **k: '{"result": "{}"}'
    )
    with pytest.raises(ProviderError, match="structured_output"):
        ClaudeCliProvider(small_config).analyze(request_data)


def test_codex_uses_schema_output_file_and_readonly(small_config, request_data, monkeypatch):
    small_config.analysis.model = "test-codex-model"
    data = FakeProvider().analyze(request_data).data
    monkeypatch.setattr("paperflow.agents.codex_cli.resolve_command", lambda _: "codex")
    monkeypatch.setattr("paperflow.agents.codex_cli.checked_help", lambda *a: "")

    def run(argv, **kwargs):
        assert argv[argv.index("--model") + 1] == "test-codex-model"
        assert argv[argv.index("--sandbox") + 1] == "read-only"
        assert "--ignore-user-config" in argv
        assert "features.shell_tool=false" in argv
        assert 'web_search="disabled"' in argv
        assert argv[-1] == "-"
        schema = Path(argv[argv.index("--output-schema") + 1])
        assert json.loads(schema.read_text()) == SCHEMA
        Path(argv[argv.index("-o") + 1]).write_text(json.dumps(data), encoding="utf-8")
        return "progress, ignored"

    monkeypatch.setattr("paperflow.agents.codex_cli.run_bounded", run)
    assert CodexCliProvider(small_config).analyze(request_data).data == data
    monkeypatch.setattr("paperflow.agents.codex_cli.run_bounded", lambda *a, **k: "{}")
    with pytest.raises(ProviderError, match="最終JSON"):
        CodexCliProvider(small_config).analyze(request_data)


@pytest.mark.parametrize(
    "script,category",
    [
        ("import time; time.sleep(5)", "timeout"),
        ("print('a' * 10000)", "output_limit"),
        ("import sys; print('429 rate limit', file=sys.stderr); sys.exit(1)", "rate_limit"),
        ("import sys; print('not logged in', file=sys.stderr); sys.exit(1)", "authentication"),
        (
            "import sys; print('Failed to authenticate: OAuth session expired'); sys.exit(1)",
            "authentication",
        ),
        ("raise SystemExit(7)", "cli_exit"),
    ],
)
def test_real_process_bounds_and_errors(tmp_path, script, category):
    with pytest.raises(ProviderError) as exc:
        run_bounded([sys.executable, "-c", script], cwd=tmp_path, timeout=0.4, max_bytes=1024)
    assert exc.value.category == category


def test_cli_not_installed(tmp_path):
    from paperflow.agents.process import resolve_command

    with pytest.raises(ProviderError) as exc:
        resolve_command("paperflow-no-such-cli-1234567890")
    assert exc.value.category == "missing_cli"


def test_prompt_and_schema_packaged_copies_match():
    from paperflow.agents.schema import PROMPT

    repo = Path(__file__).resolve().parents[1]
    assert json.loads((repo / "schemas/analysis.schema.json").read_text()) == SCHEMA
    assert (repo / "prompts/analyze_v1.md").read_text(encoding="utf-8") == PROMPT


@pytest.mark.parametrize("cls", [ClaudeCliProvider, CodexCliProvider])
def test_provider_direct_fulltext_is_blocked(small_config, request_data, cls):
    request_data.coverage = "fulltext"
    with pytest.raises(ProviderError) as exc:
        cls(small_config).analyze(request_data)
    assert exc.value.category == "fulltext_permission"
    assert not small_config.storage.data_dir.exists()


@pytest.mark.parametrize(
    "envelope,category",
    [
        (
            {
                "subtype": "error_max_budget_usd",
                "result": "Budget reached",
                "usage": {"input_tokens": 40100},
            },
            "budget_limit",
        ),
        (
            {"subtype": "error_max_turns", "result": "", "usage": {"input_tokens": 42900}},
            "turn_limit",
        ),
        (
            {"subtype": "error_during_execution", "api_error_status": 503, "result": "error"},
            "service_error",
        ),
        ({"result": "prompt is too long"}, "context_limit"),
        (
            {"result": "unknown failure", "usage": {"input_tokens": 40100, "output_tokens": 429}},
            "cli_exit",
        ),
    ],
)
def test_cli_failure_envelope_does_not_confuse_usage_with_http_errors(envelope, category):
    from paperflow.agents.process import classify_failure

    error = classify_failure(json.dumps(envelope), "", 1)
    assert error.category == category
    assert "40100" not in str(error)


@pytest.mark.parametrize(
    "subtype,category",
    [("error_max_budget_usd", "budget_limit"), ("error_max_turns", "turn_limit")],
)
def test_stream_json_classifies_final_error_only(subtype, category):
    from paperflow.agents.process import classify_failure

    stream = "\n".join(
        json.dumps(e)
        for e in [
            {"type": "system", "description": "login required is quoted here"},
            {"type": "result", "subtype": subtype, "is_error": True},
        ]
    )
    assert classify_failure(stream, "", 1).category == category


@pytest.mark.parametrize(
    "models,expected", [({"synthetic-model": {}}, "synthetic-model"), (None, None)]
)
def test_claude_model_metadata_is_optional(
    small_config, request_data, monkeypatch, models, expected
):
    data = FakeProvider().analyze(request_data).data
    monkeypatch.setattr("paperflow.agents.claude_cli.resolve_command", lambda _: "claude")
    monkeypatch.setattr("paperflow.agents.claude_cli.checked_help", lambda *args: "")
    monkeypatch.setattr(
        "paperflow.agents.claude_cli.run_bounded",
        lambda *args, **kwargs: json.dumps({"structured_output": data, "modelUsage": models}),
    )
    assert ClaudeCliProvider(small_config).analyze(request_data).model == expected
