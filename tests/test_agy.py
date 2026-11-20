import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner
from conftest import tree
from test_agents import request_data as abstract_fixture
from test_vision import visual_data
from test_vision import visual_request as visual_fixture

from paperflow.agents.agy_cli import AgyCliProvider, agy_usage, session_settings
from paperflow.agents.base import ProviderError
from paperflow.agents.catalog import discover
from paperflow.agents.fake import FakeProvider
from paperflow.agents.process import resolve_command
from paperflow.cli import app
from paperflow.model_selection import preferences, remember


@pytest.fixture
def abstract_request():
    return abstract_fixture.__wrapped__()


@pytest.fixture
def pdf_request(tmp_path):
    return visual_fixture.__wrapped__(tmp_path)


def channel_fixture(monkeypatch, data, *, mode="success", response_text=None):
    calls, directories = [], []
    monkeypatch.setattr("paperflow.agents.agy_cli.resolve_command", lambda _: "agy.exe")
    monkeypatch.setattr("paperflow.agents.agy_cli.checked_help", lambda *a: "")

    class Channel:
        def __init__(self, argv, cwd, *, on_event, **kwargs):
            assert "--include-default-customizations=false" in argv
            assert "--dangerously-skip-permissions" not in argv
            settings = json.loads(
                Path(argv[argv.index("--settings") + 1]).read_text(encoding="utf-8")
            )
            assert "command(*)" in settings["permissions"]["deny"]
            assert "write_file(*)" in settings["permissions"]["deny"]
            assert settings["agentMode"] == "" and not settings["allowNonWorkspaceAccess"]
            self.cwd, self.callback = cwd, on_event
            directories.append(cwd)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def send(self, event):
            self.event = event
            calls.append(event)

        def receive(self, predicate):
            method = self.event["method"]
            result = {}
            if method == "session/new":
                result = dict(
                    sessionId="synthetic-session",
                    models=dict(
                        currentModelId="test-agy", availableModels=[dict(modelId="test-agy")]
                    ),
                )
            elif method == "session/prompt":
                prompt = self.event["params"]["prompt"][0]["text"]
                assert "JSON Schema" in prompt and '"tldr_ja"' in prompt
                pdfs = list(self.cwd.glob("*.pdf"))
                if pdfs and mode != "missing-read":
                    assert "read_file" in prompt
                    assert "Synthetic visual" not in prompt  # PDF本文の二重入力をしない。
                    pdf = pdfs[0] if mode != "outside" else self.cwd.parent / "outside.pdf"
                    update = dict(
                        sessionUpdate="tool_call",
                        toolCallId="read-1",
                        name="read_file" if mode != "command" else "run_command",
                        kind="read" if mode != "command" else "execute",
                        rawInput=dict(Path=str(pdf)),
                        status="in_progress",
                    )
                    self.callback(dict(method="session/update", params=dict(update=update)))
                    update = dict(
                        sessionUpdate="tool_call_update", toolCallId="read-1", status="completed"
                    )
                    self.callback(dict(method="session/update", params=dict(update=update)))
                if mode == "permission":
                    self.callback(
                        dict(jsonrpc="2.0", id=98, method="session/request_permission", params={})
                    )
                if mode == "timeout":
                    raise ProviderError("timeout", "合成")
                text = (
                    json.dumps(data, ensure_ascii=False) if mode != "invalid-json" else "malformed"
                )
                if response_text is not None:
                    text = response_text
                self.callback(
                    dict(
                        method="session/update",
                        params=dict(
                            update=dict(
                                sessionUpdate="agent_message_chunk",
                                content=dict(type="text", text=text),
                            )
                        ),
                    )
                )
                result = dict(
                    stopReason="end_turn",
                    usage=dict(
                        inputTokens=30,
                        outputTokens=5,
                        cachedReadTokens=20,
                        cachedWriteTokens=0,
                        totalTokens=55,
                    ),
                )
            response = dict(id=self.event["id"], result=result)
            assert predicate(response)
            return response

    monkeypatch.setattr("paperflow.agents.agy_cli.JsonChannel", Channel)
    return calls, directories


def test_agy_abstract_json_and_cli_reported_usage(small_config, abstract_request, monkeypatch):
    data = FakeProvider().analyze(abstract_request).data
    calls, dirs = channel_fixture(monkeypatch, data)
    result = AgyCliProvider(small_config).analyze(abstract_request)
    assert result.provider == "agy" and result.model == "test-agy" and result.data == data
    assert result.usage["input_tokens"] == 50 and result.usage["total_tokens"] == 55
    assert result.usage["cost_usd"] is None
    assert not any(d.exists() for d in dirs)
    assert calls[-1]["method"] == "session/prompt"


@pytest.mark.parametrize(
    "mode",
    ["success", "missing-read", "outside", "command", "permission", "timeout", "invalid-json"],
)
def test_agy_pdf_readonly_and_rejected_operations(small_config, pdf_request, monkeypatch, mode):
    small_config.analysis.allow_external_fulltext = True
    small_config.analysis.policy = "fulltext_external"
    small_config.analysis.model = "test-agy"
    data = visual_data(pdf_request)
    before = tree(pdf_request.document.path.parent)
    calls, dirs = channel_fixture(monkeypatch, data, mode=mode)
    if mode == "success":
        result = AgyCliProvider(small_config).analyze(pdf_request)
        assert result.data["visual_review"]["pages_viewed"] == [1, 2]
        assert result.model == "test-agy"
    else:
        expected = {
            "missing-read": "visual_reading",
            "invalid-json": "invalid_json",
            "timeout": "timeout",
        }.get(mode, "tool_disabled")
        with pytest.raises(ProviderError) as error:
            AgyCliProvider(small_config).analyze(pdf_request)
        assert error.value.category == expected
    assert not any(d.exists() for d in dirs)
    assert (
        pdf_request.document.path.read_bytes()
        == before[str(pdf_request.document.path.relative_to(pdf_request.document.path.parent))][0]
    )
    assert sum(c["method"] == "session/prompt" for c in calls) == 1


def test_agy_direct_send_permission_and_model_availability(small_config, pdf_request, monkeypatch):
    with pytest.raises(ProviderError, match="明示許可"):
        AgyCliProvider(small_config).analyze(pdf_request)
    assert not small_config.storage.data_dir.exists()
    small_config.analysis.allow_external_fulltext = True
    small_config.analysis.policy = "fulltext_external"
    small_config.analysis.model = "unknown-agy-model"
    calls, _ = channel_fixture(monkeypatch, visual_data(pdf_request))
    with pytest.raises(ProviderError, match="モデル一覧を更新"):
        AgyCliProvider(small_config).analyze(pdf_request)
    assert not any(c["method"] == "session/prompt" for c in calls)


def test_agy_models_are_listed_without_inference_and_can_be_remembered(small_config, monkeypatch):
    monkeypatch.setattr("paperflow.agents.catalog.resolve_command", lambda _: "agy.exe")
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return json.dumps(
            dict(
                status="SUCCESS",
                command=dict(
                    name="models",
                    data=dict(models=[dict(id="model-high", label="合成モデル（高）")]),
                ),
            )
        )

    monkeypatch.setattr("paperflow.agents.process.run_bounded", run)
    result = discover(small_config, "agy")
    assert result.models[0].model_id == "model-high"
    assert result.models[0].display_name == "合成モデル（高）"
    assert calls == [["agy.exe", "--output-format", "json", "models"]]
    remember(small_config, "agy", "model-high")
    assert preferences(small_config)["models"]["agy"] == "model-high"


def test_agy_cli_dry_run_has_no_command_database_or_file_side_effects(small_config, monkeypatch):
    def forbidden(*a, **kw):
        pytest.fail("dry-runでCLIを起動した")

    monkeypatch.setattr("subprocess.Popen", forbidden)
    root = small_config.source.paperpile_dir.parent
    before = tree(root)
    result = CliRunner().invoke(
        app,
        [
            "--config",
            str(root / "config.yaml"),
            "sync",
            "--provider",
            "agy",
            "--model",
            "test-model",
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["model_selection"]["provider"] == "agy"
    assert tree(root) == before


@pytest.mark.skipif(os.name != "nt", reason="Windowsネイティブの標準導入先")
def test_standard_agy_binary_is_detected_even_if_ide_wrapper_is_on_path(tmp_path, monkeypatch):
    executable = tmp_path / "AppData/Local/agy/bin/agy.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"synthetic-exe-placeholder")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr("shutil.which", lambda _: str(tmp_path / "agy.cmd"))
    assert resolve_command("agy") == str(executable)


def test_agy_unknown_usage_is_not_estimated():
    assert agy_usage(None)["input_tokens"] is None
    assert agy_usage(None)["cost_usd"] is None
    assert "read_file(*)" in session_settings()["permissions"]["deny"]


def test_cli_help_on_stderr_is_still_detected(tmp_path):
    import sys

    from paperflow.agents.process import run_bounded

    text = run_bounded(
        [sys.executable, "-c", "import sys; print('--settings --stdio',file=sys.stderr)"],
        cwd=tmp_path,
        include_stderr=True,
    )
    assert "--settings" in text and "--stdio" in text


def test_unrelated_agy_command_does_not_invalidate_an_existing_batch(small_config):
    from paperflow.agents.schema import PROMPT, VISUAL_PROMPT, VISUAL_SCHEMA
    from paperflow.batches import runtime_key
    from paperflow.models import digest

    analysis = small_config.analysis.model_dump(mode="json", exclude={"agy_command"})
    expected = digest(
        dict(
            analysis=analysis,
            taxonomy=small_config.taxonomy(),
            visuals=small_config.destination.visual_max_items,
            prompt=PROMPT + VISUAL_PROMPT,
            schema=VISUAL_SCHEMA,
        )
    )
    assert runtime_key(small_config) == expected
    small_config.analysis.agy_command = "other-pc-agy.exe"
    assert runtime_key(small_config) == expected
