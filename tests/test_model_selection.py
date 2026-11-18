import json
import sys
from types import SimpleNamespace

import pytest
from click.testing import CliRunner
from conftest import tree

from paperflow.agents.base import ProviderError
from paperflow.agents.catalog import (
    JsonChannel,
    ModelCatalog,
    ModelChoice,
    discover,
    normalize_models,
)
from paperflow.cli import app
from paperflow.config import SettingsError
from paperflow.model_selection import cached_catalog, get_catalog, preferences, remember


def catalog(provider="codex"):
    return ModelCatalog(
        provider,
        [
            ModelChoice("test-small", "試験用モデル", True),
            ModelChoice("test-large", "大きな試験用モデル"),
        ],
        "2026-11-16T00:00:00+00:00",
    )


def args(config, *extra):
    return ["--config", str(config.source.paperpile_dir.parent / "config.yaml"), *extra]


def test_normalize_claude_resolves_aliases_and_deduplicates():
    result = normalize_models(
        "claude",
        [
            {"value": "default", "resolvedModel": "test-opus[1m]", "displayName": "Default"},
            {"value": "opus[1m]", "resolvedModel": "test-opus[1m]", "displayName": "Opus"},
            {"value": "test-fable[1m]", "resolvedModel": "test-fable"},
            {"value": "sonnet", "resolvedModel": "test-sonnet", "displayName": "Sonnet\x1b\n"},
            {"value": "default"},
            {"value": "bad id"},
            {"value": "--bad"},
            None,
        ],
    )
    assert [c.model_id for c in result] == ["test-opus[1m]", "test-fable[1m]", "test-sonnet"]
    assert result[0].is_default
    assert result[0].aliases == ["default", "opus[1m]"]
    assert result[-1].display_name == "Sonnet"


def test_normalize_codex_filters_hidden_and_invalid_entries():
    choices = normalize_models(
        "codex",
        [
            {"model": "test", "isDefault": True},
            {"id": "test-id"},
            {"model": "hidden", "hidden": True},
            {},
            {"model": "a\nb"},
        ],
    )
    assert [c.model_id for c in choices] == ["test", "test-id"]
    assert choices[0].is_default
    with pytest.raises(ProviderError):
        normalize_models("codex", None)


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_discovery_only_initializes_and_lists_models(small_config, monkeypatch, provider):
    messages = []

    class Channel:
        def __init__(self, argv, cwd, **kwargs):
            assert cwd.is_dir()
            assert kwargs["timeout"] == 20
            if provider == "claude":
                assert "--tools" in argv and "--safe-mode" in argv
                assert "--no-session-persistence" in argv
            else:
                assert "app-server" in argv
                assert "mcp_servers={}" in argv

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def send(self, message):
            messages.append(message)

        def receive(self, predicate):
            request = messages[-1]
            if provider == "claude":
                response = {
                    "type": "control_response",
                    "response": {
                        "request_id": "paperflow-models",
                        "subtype": "success",
                        "response": {
                            "models": [{"value": "sonnet", "resolvedModel": "test-sonnet"}],
                            "account": {"email": "PRIVATE-DO-NOT-SAVE"},
                        },
                    },
                }
            elif request["method"] == "initialize":
                response = {"id": 0, "result": {}}
            else:
                page = request["id"]
                response = {
                    "id": page,
                    "result": {
                        "data": [{"model": f"test-{page}"}],
                        "nextCursor": "next" if page == 1 else None,
                    },
                }
            assert predicate(response)
            return response

    monkeypatch.setattr("paperflow.agents.catalog.resolve_command", lambda _: "synthetic-cli")
    monkeypatch.setattr("paperflow.agents.catalog.JsonChannel", Channel)
    before = tree(small_config.source.paperpile_dir.parent)
    found = discover(small_config, provider)
    assert "PRIVATE" not in repr(found)
    assert tree(small_config.source.paperpile_dir.parent) == before
    if provider == "claude":
        assert len(messages) == 1
        assert messages[0]["request"]["subtype"] == "initialize"
        assert found.models[0].model_id == "test-sonnet"
    else:
        assert [m["method"] for m in messages] == [
            "initialize",
            "initialized",
            "model/list",
            "model/list",
        ]
        assert messages[-1]["params"]["cursor"] == "next"
        assert len(found.models) == 2


@pytest.mark.parametrize(
    "script,category",
    [
        ("import time; time.sleep(5)", "timeout"),
        ("print('x' * 8192, flush=True)", "output_limit"),
        ("import sys; print('not logged in', file=sys.stderr); sys.exit(1)", "authentication"),
    ],
)
def test_catalog_process_is_bounded_and_closed(tmp_path, script, category):
    with pytest.raises(ProviderError) as exc:
        with JsonChannel(
            [sys.executable, "-u", "-c", script], tmp_path, timeout=0.5, max_bytes=1024
        ) as channel:
            channel.receive(lambda _: True)
    assert exc.value.category == category
    assert channel.process.poll() is not None
    assert not channel.thread.is_alive()


def test_catalog_process_refuses_server_requested_tool(tmp_path):
    script = """
import json, sys
print(json.dumps({'id': 1, 'method': 'executeTool'}), flush=True)
reply = json.loads(sys.stdin.readline())
print(json.dumps({'reply': reply}), flush=True)
"""
    with JsonChannel([sys.executable, "-u", "-c", script], tmp_path) as channel:
        event = channel.receive(lambda e: "reply" in e)
    assert event["reply"]["error"]["code"] == -32601


def test_preferences_keep_models_separate_and_do_not_edit_config(small_config):
    path = small_config.source.paperpile_dir.parent / "config.yaml"
    before = path.read_bytes()
    remember(small_config, "claude", "test-sonnet")
    remember(small_config, "codex", "test-codex")
    remember(small_config, "fake", None)
    saved = preferences(small_config)
    assert saved["models"] == {"claude": "test-sonnet", "codex": "test-codex"}
    assert saved["last_provider"] == "fake"
    assert path.read_bytes() == before


def test_cache_failure_fallback_and_no_cache_error(small_config, monkeypatch):
    monkeypatch.setattr("paperflow.model_selection.discover", lambda *a: catalog())
    get_catalog(small_config, "codex")
    before = tree(small_config.storage.data_dir)

    def fail(*a):
        raise ProviderError("authentication", "認証を確認してください")

    monkeypatch.setattr("paperflow.model_selection.discover", fail)
    found = get_catalog(small_config, "codex")
    assert found.source == "cache" and "認証" in found.warning
    assert tree(small_config.storage.data_dir) == before
    assert cached_catalog(small_config, "claude") is None
    with pytest.raises(ProviderError):
        get_catalog(small_config, "claude")


def test_select_model_saves_choice_without_running_pipeline(small_config, monkeypatch):
    monkeypatch.setattr("paperflow.model_selection.discover", lambda *a: catalog())
    monkeypatch.setattr(
        "paperflow.cli.Pipeline.run", lambda *a, **k: pytest.fail("解析してはいけない")
    )
    result = CliRunner().invoke(app, args(small_config, "select-model"), input="2\n2\n")
    assert result.exit_code == 0, result.output
    assert "使用するProviderの番号" in result.output
    assert preferences(small_config)["models"] == {"codex": "test-large"}
    assert not small_config.storage.database.exists()
    assert not (small_config.destination.vault_dir / "PaperFlow").exists()


def test_manual_fallback_validates_and_saves_model(small_config, monkeypatch):
    def fail(*a):
        raise ProviderError("missing_cli", "CLIがありません")

    monkeypatch.setattr("paperflow.model_selection.discover", fail)
    result = CliRunner().invoke(
        app,
        args(small_config, "select-model", "--provider", "claude"),
        input="1\nbad model\ntest-manual\n",
    )
    assert result.exit_code == 0, result.output
    assert "空白" in result.output
    assert preferences(small_config)["models"]["claude"] == "test-manual"


def test_cancel_does_not_save_or_analyze(small_config, monkeypatch):
    monkeypatch.setattr("paperflow.model_selection.discover", lambda *a: catalog())
    monkeypatch.setattr(
        "paperflow.cli.Pipeline.run", lambda *a, **k: pytest.fail("解析してはいけない")
    )
    result = CliRunner().invoke(app, args(small_config, "select-model"), input="2\n")
    assert result.exit_code != 0
    assert preferences(small_config)["last_provider"] is None
    assert not small_config.storage.database.exists()


@pytest.mark.parametrize("with_cache", [False, True])
def test_interactive_dry_run_has_no_disk_or_process_side_effects(
    small_config, monkeypatch, with_cache
):
    monkeypatch.setattr("paperflow.model_selection.discover", lambda *a: catalog())
    if with_cache:
        get_catalog(small_config, "codex")
    before = tree(small_config.source.paperpile_dir.parent)
    monkeypatch.setattr(
        "subprocess.Popen", lambda *a, **k: pytest.fail("dry-runで外部プロセス禁止")
    )
    monkeypatch.setattr(
        "paperflow.model_selection.discover", lambda *a: pytest.fail("モデル照会禁止")
    )
    result = CliRunner().invoke(
        app,
        args(small_config, "sync", "--dry-run", "--interactive"),
        input="2\n2\n" if with_cache else "2\n1\ntest-manual\n",
    )
    assert result.exit_code == 0, (result.output, result.exception)
    data = json.loads(result.stdout)
    assert data["model_selection"]["provider"] == "codex"
    assert data["model_selection"]["model"] == ("test-large" if with_cache else "test-manual")
    assert tree(small_config.source.paperpile_dir.parent) == before


@pytest.mark.parametrize("extra", [[], ["--json"]])
def test_non_terminal_does_not_prompt_or_discover(small_config, monkeypatch, extra):
    monkeypatch.setattr("paperflow.model_selection.discover", lambda *a: pytest.fail("照会禁止"))
    monkeypatch.setattr("paperflow.model_selection.menu", lambda *a: pytest.fail("対話禁止"))
    result = CliRunner().invoke(app, args(small_config, *extra, "sync", "--dry-run"))
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["model_selection"]["provider"] == "fake"


def test_terminal_auto_selection(small_config, monkeypatch):
    tty = SimpleNamespace(isatty=lambda: True)
    monkeypatch.setattr("paperflow.model_selection.sys", SimpleNamespace(stdin=tty, stdout=tty))
    result = CliRunner().invoke(app, args(small_config, "sync", "--dry-run"), input="3\n")
    assert result.exit_code == 0, result.output
    assert "使用するProviderの番号" in result.output


@pytest.mark.parametrize(
    "command,tail",
    [
        ("sync", ["--dry-run"]),
        ("analyze", ["--paper-id", "test"]),
        ("retry", ["--dry-run"]),
        ("watch", ["--once"]),
    ],
)
def test_explicit_model_reaches_each_command(small_config, monkeypatch, command, tail):
    seen = []

    def run(self, **kwargs):
        seen.append((self.config.analysis.default_provider, self.config.analysis.model, kwargs))
        return {"failures": []}

    monkeypatch.setattr("paperflow.cli.Pipeline.run", run)
    result = CliRunner().invoke(
        app,
        args(
            small_config,
            "--json",
            command,
            *tail,
            "--provider",
            "codex",
            "--model",
            "test-explicit",
        ),
    )
    assert result.exit_code == 0, (result.output, result.exception)
    assert seen and all(row[:2] == ("codex", "test-explicit") for row in seen)
    assert not small_config.storage.data_dir.exists()


def test_saved_provider_precedence_and_explicit_override(small_config, monkeypatch):
    remember(small_config, "claude", "test-claude")
    remember(small_config, "codex", "test-codex")
    before = tree(small_config.storage.data_dir)
    runner = CliRunner()
    for options, expected in [
        ([], ("codex", "test-codex")),
        (["--provider", "claude"], ("claude", "test-claude")),
        (["--model", "test-override"], ("codex", "test-override")),
        (["--provider", "fake"], ("fake", None)),
    ]:
        result = runner.invoke(app, args(small_config, "--json", "sync", "--dry-run", *options))
        assert result.exit_code == 0, result.output
        selected = json.loads(result.output)["model_selection"]
        assert (selected["provider"], selected["model"]) == expected
    assert tree(small_config.storage.data_dir) == before


def test_shared_config_model_does_not_leak_to_other_provider(small_config, monkeypatch):
    import yaml

    path = small_config.source.paperpile_dir.parent / "config.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["analysis"].update(default_provider="claude", model="test-sonnet")
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    result = CliRunner().invoke(app, args(small_config, "sync", "--dry-run", "--provider", "codex"))
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["model_selection"]["model"] is None


@pytest.mark.parametrize(
    "tail",
    [
        ["--interactive"],
        ["--provider", "codex", "--model", "bad model"],
        ["--provider", "fake", "--model", "some-model"],
    ],
)
def test_invalid_cli_combinations_do_not_run(small_config, monkeypatch, tail):
    monkeypatch.setattr("paperflow.cli.Pipeline.run", lambda *a, **k: pytest.fail("実行禁止"))
    result = CliRunner().invoke(app, args(small_config, "--json", "sync", "--dry-run", *tail))
    assert result.exit_code == 2, result.output
    assert not small_config.storage.data_dir.exists()


def test_corrupt_preferences_fail_without_replacing_them(small_config):
    small_config.storage.data_dir.mkdir()
    path = small_config.storage.data_dir / "model-preferences.json"
    path.write_text('{"version": 100}', encoding="utf-8")
    before = tree(small_config.storage.data_dir)
    with pytest.raises(SettingsError):
        remember(small_config, "codex", "test")
    assert path.read_text(encoding="utf-8") == '{"version": 100}'
    assert before[path.name] == tree(small_config.storage.data_dir)[path.name]


def test_models_cached_command_is_read_only(small_config, monkeypatch):
    monkeypatch.setattr("paperflow.model_selection.discover", lambda *a: catalog())
    result = CliRunner().invoke(app, args(small_config, "--json", "models", "--provider", "codex"))
    assert result.exit_code == 0, result.output
    before = tree(small_config.storage.data_dir)
    monkeypatch.setattr("paperflow.model_selection.discover", lambda *a: pytest.fail("照会禁止"))
    result = CliRunner().invoke(
        app, args(small_config, "--json", "models", "--provider", "codex", "--cached")
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["source"] == "cache"
    assert tree(small_config.storage.data_dir) == before


def test_interactive_watch_chooses_once_for_multiple_cycles(small_config, monkeypatch):
    monkeypatch.setattr("paperflow.model_selection.discover", lambda *a: catalog())
    runs = []

    def run(self, **kwargs):
        runs.append((self.config.analysis.model, kwargs))
        return {"failures": []}

    sleeps = []

    def sleep(_):
        sleeps.append(True)
        if len(sleeps) == 2:
            raise KeyboardInterrupt

    monkeypatch.setattr("paperflow.cli.Pipeline.run", run)
    monkeypatch.setattr("paperflow.cli.time.sleep", sleep)
    result = CliRunner().invoke(
        app, args(small_config, "watch", "--interactive", "--provider", "codex"), input="2\n"
    )
    assert result.exit_code != 0
    assert len(runs) == 4
    assert all(row[0] == "test-large" for row in runs)
    assert result.output.count("使用するモデルの番号") == 1


def test_select_model_dry_run_uses_manual_without_saving(small_config, monkeypatch):
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: pytest.fail("プロセス起動禁止"))
    before = tree(small_config.source.paperpile_dir.parent)
    result = CliRunner().invoke(
        app,
        args(small_config, "select-model", "--provider", "claude", "--dry-run"),
        input="1\ntest-id\n",
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["saved"] is False
    assert tree(small_config.source.paperpile_dir.parent) == before
