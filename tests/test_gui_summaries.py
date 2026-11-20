import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from test_gui import gui_config
from test_summary_versions import VisualProvider

from paperflow.gui_config import save_changes


@pytest.mark.skipif(os.name != "nt", reason="WindowsネイティブGUI")
@pytest.mark.parametrize("backend", ["claude", "agy"])
def test_native_gui_selected_reanalysis_comparison_and_adoption(tmp_path, monkeypatch, backend):
    if not os.environ.get("PAPERFLOW_SUMMARY_GUI_CHILD"):
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                "-X",
                "utf8",
                "-m",
                "pytest",
                __file__
                + "::test_native_gui_selected_reanalysis_comparison_and_adoption["
                + backend
                + "]",
                "-q",
                "--tb=short",
            ],
            env=dict(os.environ, PAPERFLOW_SUMMARY_GUI_CHILD="1"),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=85,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return
    import tkinter as tk

    from paperflow.gui import PaperFlowApp
    from paperflow.gui_summaries import SummaryDialog

    errors, confirmations, opened = [], [], []
    approve = [False]

    def confirm(title, message, **kwargs):
        confirmations.append(message)
        return approve[0]

    monkeypatch.setattr("tkinter.messagebox.showerror", lambda *a, **k: errors.append(a))
    monkeypatch.setattr("tkinter.messagebox.askyesno", confirm)
    root = tk.Tk()
    root.withdraw()
    app = PaperFlowApp(root, gui_config(tmp_path), auto_load=False)

    def pump(until, timeout=35):
        deadline = time.monotonic() + timeout
        while not until() and time.monotonic() < deadline:
            root.update()
            time.sleep(0.02)
        assert until(), errors
        assert not errors, errors

    try:
        app.prepare_plan()
        pump(lambda: not app.busy)
        app.start()
        pump(lambda: not app.busy)
        save_changes(
            app.service.path,
            {
                "analysis.policy": "fulltext_external",
                "analysis.allow_external_fulltext": True,
                "analysis.require_visual_reading": True,
                "destination.include_visuals": True,
            },
        )
        app.cfg = app.service.config()
        app.tabs.select(app.library_tab)
        app.refresh_library(reconcile=True)
        pump(lambda: app.library_loaded)
        selected = list(app.library_tree.get_children())[:2]
        app.library_tree.selection_set(selected)
        app.filter_library()
        assert set(app.library_tree.selection()) == set(selected)
        app.open_summary_versions()
        pump(lambda: not app.busy)
        # 実際のボタンから開いた画面を取得するため、生成したdialogを公開する。
        dialog = app.summary_dialog
        assert isinstance(dialog, SummaryDialog)
        assert len(dialog.paper_ids) == 2 and len(dialog.tree.get_children()) == 1
        original_id = dialog.data["current"]
        dialog.provider.set({"claude": "Claude Code CLI", "agy": "Antigravity CLI（agy）"}[backend])
        dialog.change_provider()
        dialog.model.set("合成の指定モデル")
        provider = VisualProvider("合成の指定モデル")
        models = []

        def factory(pipeline, name):
            models.append(pipeline.config.analysis.model)
            return provider

        monkeypatch.setattr("paperflow.pipeline.Pipeline._provider", factory)
        dialog.generate()
        assert not app.busy and not provider.calls
        assert "PDF全文・図表" in confirmations[-1]
        approve[0] = True
        dialog.generate()
        pump(lambda: not app.busy)
        assert set(provider.calls) == set(selected) and len(provider.calls) == 2
        assert models == ["合成の指定モデル", "合成の指定モデル"]
        assert dialog.data["current"] == original_id
        assert len(dialog.tree.get_children()) == 2
        candidate = next(i for i in dialog.tree.get_children() if i != original_id)
        dialog.tree.selection_set(candidate)
        root.update()
        assert "合成の指定モデル" in dialog.texts[1].get("1.0", "end")
        assert "123" in dialog.consumption.get()
        monkeypatch.setattr(app, "open_path", lambda path: opened.append(Path(path)))
        dialog.compare()
        pump(lambda: not app.busy)
        assert opened and '<img alt="論文の図表"' in opened[-1].read_text(encoding="utf-8")
        dialog.adopt()
        pump(lambda: not app.busy)
        assert dialog.data["current"] == candidate
        dialog.tree.selection_set(original_id)
        dialog.adopt()
        pump(lambda: not app.busy)
        assert dialog.data["current"] == original_id
        assert not errors
    finally:
        app._destroy()
