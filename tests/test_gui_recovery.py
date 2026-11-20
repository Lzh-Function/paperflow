import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from test_gui import gui_config

from paperflow.agents.base import ProviderError
from paperflow.config import load_config
from paperflow.pipeline import Pipeline
from paperflow.store import Store


@pytest.mark.skipif(os.name != "nt", reason="WindowsネイティブGUI")
def test_native_recovery_panel(tmp_path, monkeypatch):
    if not os.environ.get("PAPERFLOW_RECOVERY_GUI_TEST"):
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                "-X",
                "utf8",
                "-m",
                "pytest",
                str(Path(__file__).resolve()) + "::test_native_recovery_panel",
                "-q",
                "--tb=short",
            ],
            env=dict(os.environ, PAPERFLOW_RECOVERY_GUI_TEST="1"),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=70,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return
    import tkinter as tk

    from paperflow.gui import PaperFlowApp

    path = gui_config(tmp_path)
    cfg = load_config(path)
    Pipeline(cfg).run(scan_only=True)
    Pipeline(cfg).run(limit=1, export=False)
    with Store(cfg.storage.database, readonly=True) as store:
        ready = store.rows("SELECT paper_id FROM analysis_runs WHERE status='success'")[0][
            "paper_id"
        ]
        failed = next(
            p["paper_id"]
            for p in store.rows("SELECT paper_id FROM papers")
            if p["paper_id"] != ready
        )

    class Fail:
        def analyze(self, request):
            raise ProviderError("authentication", "合成の認証失敗")

    Pipeline(cfg, Fail()).run(paper_ids=[failed], export=False)
    errors = []
    monkeypatch.setattr("paperflow.gui.messagebox.showerror", lambda *a, **k: errors.append(a))
    root = tk.Tk()
    root.withdraw()
    app = PaperFlowApp(root, path, auto_load=False)

    def wait(condition):
        end = time.monotonic() + 25
        while not condition() and time.monotonic() < end:
            root.update()
            time.sleep(0.02)
        assert condition(), errors
        assert not errors, errors

    try:
        assert len(app.tabs.tabs()) == 6
        app.tabs.select(app.recovery_tab)
        app.refresh_recovery()
        wait(lambda: app.recovery_data is not None)
        assert app.recovery_data["counts"] == {"ready": 1, "retry": 1}
        app.recovery_tree.selection_set(ready)
        monkeypatch.setattr("paperflow.gui_recovery.messagebox.askyesno", lambda *a, **k: False)
        app.prepare_recovery_action("save")
        wait(lambda: not app.busy)
        with Store(cfg.storage.database, readonly=True) as store:
            assert store.rows("SELECT * FROM exports") == []
        monkeypatch.setattr("paperflow.gui_recovery.messagebox.askyesno", lambda *a, **k: True)

        def forbid(*a, **k):
            raise AssertionError("AIを呼びました")

        monkeypatch.setattr(Pipeline, "_provider", forbid)
        app.prepare_recovery_action("save")
        wait(
            lambda: (
                app.recovery_current_job is not None
                and app.recovery_current_job["status"] == "complete"
                and not app.busy
            )
        )
        wait(lambda: not app.read_pending)
        app.show_saved_recovery()
        assert "保存済み" in app.recovery_detail.get("1.0", "end")
        with Store(cfg.storage.database, readonly=True) as store:
            assert len(store.rows("SELECT * FROM exports")) == 1
        app.reset_recovery()
        assert app.recovery_data is None and not app.recovery_tree.get_children()
    finally:
        app._destroy()
