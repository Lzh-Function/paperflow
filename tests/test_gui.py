import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
import yaml
from conftest import tree
from create_demo import make_demo
from test_batches import batch_config as batch_fixture
from test_wiki import CONTROL

from paperflow.agents.fake import FakeProvider
from paperflow.batches import execute, live_report, plan
from paperflow.config import SettingsError
from paperflow.gui_config import config_values, save_changes
from paperflow.gui_service import GuiService
from paperflow.model_selection import preferences
from paperflow.obsidian.writer import WriteConflict


def gui_config(tmp_path):
    path = make_demo(tmp_path / "合成GUI", count=3)
    original = path.read_text(encoding="utf-8")
    values = yaml.safe_load(original)
    values["destination"].update(layout="llm_wiki", copy_pdf=True, include_visuals=False)
    path.write_text(
        "# 合成設定を保持する\n" + yaml.safe_dump(values, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    vault = Path(values["destination"]["vault_dir"])
    for folder in [
        "scripts",
        "00_System/Index",
        "00_System/Locks",
        "10_Papers/20_Source_Notes",
        "50_Wiki/Concepts",
    ]:
        (vault / folder).mkdir(parents=True, exist_ok=True)
    (vault / "00_System/PaperFlow-Integration.md").write_text(
        "合成フレームワーク\n", encoding="utf-8"
    )
    (vault / "00_System/Index/log.md").write_text("既存ログ\n", encoding="utf-8")
    for script in [
        "append_log.py",
        "update_index.py",
        "validate_frontmatter.py",
        "validate_obsidian_markup.py",
        "resolve_links.py",
        "check_graph_integrity.py",
    ]:
        (vault / "scripts" / script).write_text(CONTROL, encoding="utf-8")
    return path


@pytest.fixture
def gui_batch(tmp_path):
    return batch_fixture.__wrapped__(tmp_path)


def test_settings_preserve_comments_and_unedited_values(tmp_path):
    path = gui_config(tmp_path)
    original = path.read_text(encoding="utf-8")
    values = config_values(path)
    save_changes(path, {"analysis.timeout_seconds": 600}, expected_text=original)
    after = config_values(path)
    values["analysis"]["timeout_seconds"] = 600
    assert after == values
    assert path.read_text(encoding="utf-8").startswith("# 合成設定を保持する\n")
    assert "bibtex_file: library.bib" in path.read_text(encoding="utf-8")
    assert not list(path.parent.glob(".pf-settings-*"))


def test_multiline_settings_and_invalid_edits(tmp_path):
    path = gui_config(tmp_path)
    with path.open("a", encoding="utf-8") as f:
        # 独立セクションを追加せず、既存のanalysis内へブロック文字列を挿入。
        f.write("\n")
    text = path.read_text(encoding="utf-8").replace(
        "  default_provider: fake\n",
        "  research_context: >\n    元の説明\n  default_provider: fake\n",
    )
    path.write_text(text, encoding="utf-8")
    context = "分子\n画像・NLP・ALife：複数行の説明 # 保持"
    save_changes(path, {"analysis.research_context": context})
    assert config_values(path)["analysis"]["research_context"] == context
    before = path.read_bytes()
    with pytest.raises(ValueError):
        save_changes(path, {"analysis.timeout_seconds": 0})
    assert path.read_bytes() == before
    with pytest.raises(WriteConflict):
        save_changes(path, {"analysis.model": "test"}, expected_text="古い設定")
    assert path.read_bytes() == before
    assert not list(path.parent.glob(".pf-settings-*"))


def test_settings_protect_original_and_vault(tmp_path):
    path = gui_config(tmp_path)
    service = GuiService(path)
    cfg = service.config()
    target = cfg.destination.vault_dir / "config.yaml"
    target.write_bytes(path.read_bytes())
    # 移動後も原本とVaultの絶対パスが解決されるよう全項目を具体化する。
    target.write_text(yaml.safe_dump(cfg.model_dump(mode="json")), encoding="utf-8")
    before = target.read_bytes()
    with pytest.raises(SettingsError, match="設定ファイルはC"):
        save_changes(target, {"analysis.timeout_seconds": 600})
    assert target.read_bytes() == before


def test_service_does_not_start_cli_or_write_vault_on_prepare(tmp_path, monkeypatch):
    path = gui_config(tmp_path)
    service = GuiService(path)
    cfg = service.config()
    before = tree(cfg.destination.vault_dir)

    def forbidden(*args, **kwargs):
        raise AssertionError("一覧作成で外部CLIを開始しました")

    monkeypatch.setattr("subprocess.Popen", forbidden)
    assert service.models("claude") is None
    assert service.initial_selection() == ("fake", None)
    result = service.prepare("all")
    assert result["selected_count"] == 3
    assert service.load_plan(result["plan"])["items"] == result["items"]
    assert service.history()[0]["count"] == 3
    assert tree(cfg.destination.vault_dir) == before
    with pytest.raises(SettingsError, match="全文解析"):
        service.run(result["plan"], "claude", None)
    save_changes(path, {"destination.allow_vault_write": False})
    with pytest.raises(SettingsError, match="Vault"):
        service.run(result["plan"], "fake", None)


def test_model_default_can_be_restored(tmp_path):
    service = GuiService(gui_config(tmp_path))
    service.remember("claude", "example-model")
    assert service.initial_selection() == ("claude", "example-model")
    service.remember("claude", None)
    assert service.initial_selection() == ("claude", None)
    assert preferences(service.config())["models"] == {}


def test_stop_saves_current_paper_and_resumes_same_plan(gui_batch):
    cfg = gui_batch
    saved = plan(cfg, count=3, seed=71)
    event = threading.Event()
    seen = []
    events = []

    class StoppingProvider:
        def analyze(self, request):
            seen.append(request.paper_id)
            event.set()
            return FakeProvider().analyze(request)

    result = execute(
        cfg,
        Path(saved["plan"]),
        provider=StoppingProvider(),
        should_stop=event.is_set,
        on_paper=events.append,
    )
    assert result["status"] == "stopped" and result["registered"] == 1
    assert result["remaining"] == 2
    assert [e["phase"] for e in events] == ["started", "processed"]
    assert len(result["registered_ids"]) == 1
    event.clear()
    result = execute(cfg, Path(saved["plan"]), provider=FakeProvider(), should_stop=event.is_set)
    assert result["status"] == "complete" and result["registered"] == 3
    assert result["usage"]["analysis_runs"] == 3
    assert result["usage"]["external_calls"] == 0


def test_stop_on_last_paper_is_complete(gui_batch):
    saved = plan(gui_batch, count=1)
    event = threading.Event()

    def progress(data):
        if data["phase"] == "processed":
            event.set()

    result = execute(gui_batch, Path(saved["plan"]), should_stop=event.is_set, on_paper=progress)
    assert result["status"] == "complete" and result["registered"] == 1


def test_authentication_failure_stops_batch_and_resumes(gui_batch):
    from paperflow.agents.base import ProviderError

    cfg = gui_batch
    saved = plan(cfg, count=4, seed=82)
    calls = []

    class Unauthorized:
        def analyze(self, request):
            calls.append(request.paper_id)
            if len(calls) == 1:
                return FakeProvider().analyze(request)
            raise ProviderError("authentication", "診断用の認証エラー")

    result = execute(cfg, Path(saved["plan"]), provider=Unauthorized())
    assert len(calls) == 2
    assert result["status"] == "blocked" and result["registered"] == 1
    assert result["halted"]["category"] == "authentication"
    assert "診断用の認証エラー" in Path(result["report"]).read_text(encoding="utf-8")
    result = execute(cfg, Path(saved["plan"]), provider=FakeProvider())
    assert result["status"] == "complete" and result["registered"] == 4
    assert result["halted"] is None


@pytest.mark.parametrize("diagnostic_blocked", [False, True])
def test_rejected_response_is_diagnostic_only_and_not_reused(gui_batch, diagnostic_blocked):
    from paperflow.agents.schema import validate_response

    cfg = gui_batch
    saved = plan(cfg, count=1)
    original = tree(cfg.source.paperpile_dir)
    if diagnostic_blocked:
        (cfg.storage.data_dir / "diagnostics").write_text("既存の合成ファイル", encoding="utf-8")

    class Invalid:
        def analyze(self, request):
            data = FakeProvider().analyze(request).data
            data["priority"] = 999
            return validate_response(
                data,
                request,
                usage={"input_tokens": 123},
                elapsed_seconds=2.0,
                input_receipt={"input_kind": "synthetic"},
            )

    failed = execute(cfg, Path(saved["plan"]), provider=Invalid())
    assert failed["registered"] == 0
    if diagnostic_blocked:
        assert "診断JSONの保存にも失敗" in failed["usage"]["runs"][0]["error_message"]
    else:
        files = list((cfg.storage.data_dir / "diagnostics").glob("*.rejected.json"))
        assert len(files) == 1
        diagnostic = json.loads(files[0].read_text(encoding="utf-8"))
        assert diagnostic["accepted"] is False
        assert diagnostic["response"]["priority"] == 999
        assert diagnostic["input_receipt"]["input_kind"] == "synthetic"
    result = execute(cfg, Path(saved["plan"]), provider=FakeProvider())
    assert result["registered"] == 1 and result["usage"]["analysis_runs"] == 2
    assert tree(cfg.source.paperpile_dir) == original


def test_gui_saves_each_paper_before_next_analysis(tmp_path, monkeypatch):
    from paperflow.batches import Registry

    service = GuiService(gui_config(tmp_path))
    saved = service.prepare("all")
    cfg = service.config()
    registered_before_call = []
    analyze = FakeProvider.analyze

    def observe(self, request):
        registered_before_call.append(len(Registry(cfg).hashes))
        return analyze(self, request)

    monkeypatch.setattr(FakeProvider, "analyze", observe)
    result = service.run(saved["plan"], "fake", None)
    assert result["registered"] == 3
    assert registered_before_call == [0, 1, 2]


def test_live_report_reads_failure_while_analysis_is_waiting(gui_batch, monkeypatch):
    from paperflow.agents.base import ProviderError

    cfg = gui_batch
    saved = plan(cfg, count=2, seed=53)
    entered, release = threading.Event(), threading.Event()
    results, errors = [], []

    class Waiting:
        calls = 0

        def analyze(self, request):
            self.calls += 1
            if self.calls == 1:
                raise ProviderError("visual_reading", "合成診断：図2の画像を確認できません")
            entered.set()
            assert release.wait(15)
            return FakeProvider().analyze(request)

    def execute_in_background():
        try:
            results.append(execute(cfg, Path(saved["plan"]), provider=Waiting()))
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=execute_in_background)
    worker.start()
    try:
        assert entered.wait(10)
        # 区切りの完了前でも閲覧でき、原本/Vaultの列挙やレポート生成も行わない。
        assert not Path(saved["plan"]).with_suffix(".report.md").exists()

        # Windowsの実行ロックは読み取りも排他するため、ロックファイルを除いて比較する。
        def snapshot():
            root = cfg.source.paperpile_dir.parent
            return {
                str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mtime_ns)
                for p in root.rglob("*")
                if p.is_file() and p.name != "worker.lock"
            }

        before = snapshot()
        with monkeypatch.context() as patches:

            def forbidden(*args, **kwargs):
                raise AssertionError("閲覧でVault確認または書き込みロックを呼びました")

            patches.setattr("paperflow.batches.Registry", forbidden)
            patches.setattr("paperflow.batches.local_lock", forbidden)
            summary = live_report(cfg, Path(saved["plan"]))
        assert summary["status"] == "running"
        assert summary["usage"]["analysis_runs"] == 2
        failures = [r for r in summary["usage"]["runs"] if r["status"] == "failed"]
        assert failures[0]["error_message"] == "合成診断：図2の画像を確認できません"
        assert snapshot() == before
    finally:
        release.set()
        worker.join(20)
    assert not worker.is_alive() and not errors
    assert results[0]["registered"] == 1


@pytest.mark.skipif(os.name != "nt", reason="Windowsネイティブ画面を検証する")
def test_gui_report_is_readable_during_running_analysis(tmp_path, monkeypatch):
    # Tclインタプリタとデスクトップ画面を別プロセスへ隔離する。
    if os.environ.get("PAPERFLOW_GUI_VIEW_TEST") != "1":
        env = dict(os.environ, PAPERFLOW_GUI_VIEW_TEST="1", PYTHONUTF8="1")
        run = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                str(Path(__file__).resolve())
                + "::test_gui_report_is_readable_during_running_analysis",
                "-q",
                "--tb=short",
            ],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=55,
        )
        assert run.returncode == 0, run.stdout + run.stderr
        return

    import tkinter as tk

    from paperflow.agents.base import ProviderError
    from paperflow.gui import PaperFlowApp

    path = gui_config(tmp_path)
    root = tk.Tk()
    root.withdraw()
    app = PaperFlowApp(root, path, auto_load=False)
    entered, release = threading.Event(), threading.Event()
    calls = []
    original = FakeProvider.analyze
    errors = []
    monkeypatch.setattr("paperflow.gui.messagebox.showerror", lambda *a, **k: errors.append(a))

    def analyze(self, request):
        calls.append(request.paper_id)
        if len(calls) == 1:
            raise ProviderError("visual_reading", "合成診断：ページ2を確認できません")
        entered.set()
        assert release.wait(15)
        return original(self, request)

    def wait_for(condition, timeout=12):
        deadline = time.monotonic() + timeout
        while not condition() and time.monotonic() < deadline:
            root.update()
            time.sleep(0.02)
        assert condition(), errors

    try:
        app.prepare_plan()
        wait_for(lambda: not app.busy)
        monkeypatch.setattr(FakeProvider, "analyze", analyze)
        app.start()
        wait_for(entered.is_set)
        assert app.busy and app.action == "run"
        assert not app.report_button.instate(["disabled"])
        app.report_button.invoke()
        wait_for(lambda: "合成診断：ページ2" in app.usage_text.get("1.0", "end"))
        assert app.busy and app.action == "run"
        assert app.tabs.select() == str(app.history_tab)
        assert "失敗 1 本" in app.stats.get()
        # 履歴も解析キューを待たずに更新できる。
        app.refresh_history()
        wait_for(lambda: not app.read_pending)
        assert app.history_tree.get_children()
        app.stop()
        release.set()
        wait_for(lambda: not app.busy)
        assert len(calls) == 2 and not errors
    finally:
        release.set()
        if app.busy:
            wait_for(lambda: not app.busy)
        app._destroy()


@pytest.mark.skipif(os.name != "nt", reason="Windowsネイティブ画面を検証する")
def test_native_gui_prepare_and_offline_import(tmp_path, monkeypatch):
    import tkinter as tk

    from paperflow.gui import PaperFlowApp

    path = gui_config(tmp_path)
    errors = []
    monkeypatch.setattr("paperflow.gui.messagebox.showerror", lambda *a, **k: errors.append(a))
    root = tk.Tk()
    root.withdraw()
    app = PaperFlowApp(root, path, auto_load=False)

    def wait_idle():
        deadline = time.monotonic() + 35
        while app.busy and time.monotonic() < deadline:
            root.update()
            time.sleep(0.02)
        assert not app.busy, "GUIワーカーが完了しませんでした"
        assert not errors, errors

    try:
        assert len(app.fields) == 40
        root.deiconify()
        root.update()
        assert (
            app.start_button.winfo_rooty() + app.start_button.winfo_height()
            < root.winfo_rooty() + root.winfo_height()
        )
        assert app.start_button.instate(["disabled"])
        app.prepare_plan()
        wait_idle()
        assert len(app.tree.get_children()) == 3
        assert not app.start_button.instate(["disabled"])
        # 長いCLI待機を模したワーカー動作中も画面のイベントを処理する。
        ticks = []
        app.submit("run", lambda: time.sleep(0.3), lambda result: None)
        root.after(50, lambda: ticks.append(True))
        wait_idle()
        assert ticks
        # 不正な進捗イベントの後も更新ループが止まらない。
        app.events.put(("paper", {"phase": "invalid"}))
        app.events.put(("done", lambda result: ticks.append("recovered"), None))
        deadline = time.monotonic() + 2
        while "recovered" not in ticks and time.monotonic() < deadline:
            root.update()
            time.sleep(0.02)
        assert "recovered" in ticks
        # Fakeによる合成PDFのみ。実Vault・外部CLIには接続しない。
        app.start()
        wait_idle()
        assert "登録済み 3 本" in app.stats.get()
        assert app.progress["value"] == 3
        assert "入力トークン" in app.usage_text.get("1.0", "end")
        # 設定画面を繰り返し構築してもwheel callbackを残さない。
        app._reload_settings()
        app._reload_settings()
        root.event_generate("<MouseWheel>", delta=-120, x=30, y=30)
        root.update()
        assert not errors
        app.external_allowed.set(True)
        app.vault_allowed.set(False)
        app.save_quick_settings()
        wait_idle()
        saved = app.service.config()
        assert saved.analysis.policy == "fulltext_external"
        assert saved.analysis.allow_external_fulltext
        assert not saved.destination.allow_vault_write
        assert app.start_button.instate(["disabled"])
    finally:
        app._destroy()
