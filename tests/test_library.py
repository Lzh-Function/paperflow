import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
import yaml
from conftest import tree
from test_batches import batch_config as batch_fixture
from test_gui import gui_config

from paperflow.agents.base import ProviderError
from paperflow.agents.fake import FakeProvider
from paperflow.batches import Registry, execute, plan
from paperflow.gui_service import GuiService
from paperflow.library import filter_rows, snapshot
from paperflow.pipeline import Pipeline
from paperflow.store import Store


@pytest.fixture
def library_config(tmp_path):
    return batch_fixture.__wrapped__(tmp_path)


def test_library_read_only_states_search_and_registration(library_config):
    cfg = library_config
    saved = plan(cfg, count=3, seed=58)

    class PartlyFailing:
        calls = 0

        def analyze(self, request):
            self.calls += 1
            if self.calls == 2:
                raise ProviderError("visual_reading", "合成試験：図が判読できません")
            return FakeProvider().analyze(request)

    execute(cfg, Path(saved["plan"]), provider=PartlyFailing())
    original = tree(cfg.source.paperpile_dir.parent)
    result = snapshot(cfg, Registry(cfg))
    assert result["counts"]["total"] == 13
    assert result["counts"]["imported"] == 2
    assert result["counts"]["not_imported"] == 11
    assert result["counts"]["failed"] == 1
    assert len(filter_rows(result["rows"], group="processed")) == 2
    failed = filter_rows(result["rows"], group="failed")[0]
    assert failed["error"] == "合成試験：図が判読できません"
    assert failed["note_path"] is None
    done = filter_rows(result["rows"], group="processed")[0]
    assert done["summary"] and done["note_path"] and done["model"] == "offline-fixture"
    assert len(filter_rows(result["rows"], query=done["doi"])) == 1
    assert len(filter_rows(result["rows"], query="ＹＡＭＡＤＡ　２０２６")) == 13
    assert not filter_rows(result["rows"], query="存在しない語句")
    assert tree(cfg.source.paperpile_dir.parent) == original


def test_legacy_vault_registration_is_reflected_without_db_insertion(library_config):
    cfg = library_config
    item = plan(cfg, count=1)["items"][0]
    slug = "legacy-" + item["pdf_sha256"][:12]
    sid = "paper:" + slug
    root = cfg.destination.vault_dir
    source = root / "10_Papers/10_Sources" / slug
    source.mkdir(parents=True)
    (source / "manifest.yml").write_text(
        yaml.safe_dump(
            dict(
                source_id=sid,
                source_status="frozen",
                sha256=item["pdf_sha256"],
                title=item["title"],
                doi=item["doi"],
            )
        ),
        encoding="utf-8",
    )
    note = root / "10_Papers/20_Source_Notes/legacy.md"
    note.write_text(
        "---\n"
        + yaml.safe_dump(dict(id=sid, source_id=sid, title=item["title"]))
        + "---\n既存本文\n",
        encoding="utf-8",
    )
    before = tree(cfg.source.paperpile_dir.parent)
    result = snapshot(cfg, Registry(cfg))
    selected = next(r for r in result["rows"] if r["paper_id"] == item["paper_id"])
    assert selected["imported"] and selected["registration"] == "Vault照合で確認"
    assert selected["note_path"] == str(note)
    assert selected["last_attempt"] == ""
    assert tree(cfg.source.paperpile_dir.parent) == before
    with Store(cfg.storage.database, readonly=True) as store:
        assert not store.rows("SELECT * FROM exports")


def test_analysis_success_is_not_imported_and_orphan_trial_is_ignored(library_config):
    cfg = library_config
    saved = plan(cfg, count=1)
    Pipeline(cfg).run(paper_ids=[saved["items"][0]["paper_id"]], export=False)
    (cfg.storage.data_dir / "orphan-trial.json").write_text(
        '{"status":"success"}', encoding="utf-8"
    )
    result = snapshot(cfg, Registry(cfg))
    ready = filter_rows(result["rows"], group="ready")
    assert len(ready) == 1 and not ready[0]["imported"] and ready[0]["summary"]
    assert result["counts"]["imported"] == 0


def test_deleted_papers_remain_visible_under_removed_filter(library_config):
    cfg = library_config
    saved = plan(cfg, count=1)
    execute(cfg, Path(saved["plan"]))
    identifier = saved["items"][0]["paper_id"]
    with Store(cfg.storage.database) as store, store.transaction():
        store.update(identifier, source_present=0)
    result = snapshot(cfg, Registry(cfg))
    assert result["counts"]["total"] == 12 and result["counts"]["removed"] == 1
    assert len(filter_rows(result["rows"])) == 12
    removed = filter_rows(result["rows"], group="removed")
    assert len(removed) == 1 and removed[0]["imported"] and removed[0]["note_path"]


def test_service_cache_avoids_repeated_vault_scans_and_sync_does_not_analyze(tmp_path, monkeypatch):
    path = gui_config(tmp_path)
    service = GuiService(path)
    cfg = service.config()
    before_source, before_vault = tree(cfg.source.paperpile_dir), tree(cfg.destination.vault_dir)

    def forbidden(*a, **k):
        raise AssertionError("一覧の同期で外部プロセスを呼びました")

    monkeypatch.setattr("subprocess.Popen", forbidden)
    assert service.library()["counts"]["total"] == 0
    service.sync_library()
    assert service.library()["counts"]["total"] == 3
    with monkeypatch.context() as patches:
        patches.setattr("paperflow.batches.Registry", forbidden)
        assert service.library()["counts"]["total"] == 3
    assert tree(cfg.source.paperpile_dir) == before_source
    assert tree(cfg.destination.vault_dir) == before_vault
    with Store(cfg.storage.database, readonly=True) as store:
        assert not store.rows("SELECT * FROM analysis_runs")


def test_library_does_not_count_exports_from_another_vault(library_config):
    cfg = library_config
    item = plan(cfg, count=1)["items"][0]
    with Store(cfg.storage.database) as store, store.transaction():
        store.db.execute(
            "INSERT INTO exports VALUES (?,?,?,?,?,?,?)",
            (
                item["paper_id"],
                "WikiSource",
                str(cfg.storage.data_dir / "outside.md"),
                "x",
                None,
                "2026-01-01",
                "written",
            ),
        )
    assert snapshot(cfg)["counts"]["imported"] == 0


@pytest.mark.skipif(os.name != "nt", reason="Windowsネイティブ画面を検証する")
def test_native_library_search_and_read_during_analysis(tmp_path, monkeypatch):
    if os.environ.get("PAPERFLOW_LIBRARY_TEST") != "1":
        run = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                str(Path(__file__).resolve())
                + "::test_native_library_search_and_read_during_analysis",
                "-q",
                "--tb=short",
            ],
            env=dict(os.environ, PAPERFLOW_LIBRARY_TEST="1", PYTHONUTF8="1"),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        )
        assert run.returncode == 0, run.stdout + run.stderr
        return
    import tkinter as tk

    from paperflow.gui import PaperFlowApp

    path = gui_config(tmp_path)
    service = GuiService(path)
    service.prepare("all")
    root = tk.Tk()
    root.withdraw()
    app = PaperFlowApp(root, path, auto_load=False)
    entered, release = threading.Event(), threading.Event()
    calls, errors = [], []
    original = FakeProvider.analyze
    monkeypatch.setattr("paperflow.gui.messagebox.showerror", lambda *a, **k: errors.append(a))

    def analyze(self, request):
        calls.append(request.paper_id)
        entered.set()
        assert release.wait(20)
        return original(self, request)

    def wait_for(condition, timeout=15):
        deadline = time.monotonic() + timeout
        while not condition() and time.monotonic() < deadline:
            root.update()
            time.sleep(0.02)
        assert condition(), errors

    try:
        app.tabs.select(app.library_tab)
        wait_for(lambda: app.library_loaded and not app.library_pending)
        assert len(app.library_tree.get_children()) == 3
        row = app.library_rows[0]
        app.library_query.set(row["doi"])
        app.filter_library()
        assert len(app.library_tree.get_children()) == 1
        app.library_tree.selection_set(row["paper_id"])
        app.library_details()
        assert row["title"] in app.library_detail.get("1.0", "end")
        opened = []
        monkeypatch.setattr(app, "open_path", opened.append)
        app.library_pdf_button.invoke()
        assert opened and opened[0] == Path(row["pdf_path"])
        app.library_query.set("")
        app.filter_library()
        app.prepare_plan()
        wait_for(lambda: not app.busy)
        monkeypatch.setattr(FakeProvider, "analyze", analyze)
        app.start()
        wait_for(entered.is_set)
        app.tabs.select(app.library_tab)
        app.library_refresh_button.invoke()
        wait_for(
            lambda: (
                not app.library_pending and any(r["group"] == "running" for r in app.library_rows)
            )
        )
        assert app.busy and not app.library_refresh_button.instate(["disabled"])
        assert app.library_sync_button.instate(["disabled"])
        app.library_filter.set("解析中")
        app.filter_library()
        assert len(app.library_tree.get_children()) == 1
        app.stop()
        release.set()
        wait_for(lambda: not app.busy)
        wait_for(lambda: not app.library_pending)
        assert len(calls) == 1 and not errors
    finally:
        release.set()
        if app.busy:
            wait_for(lambda: not app.busy)
        app._destroy()
