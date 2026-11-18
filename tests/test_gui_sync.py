import os
import subprocess
import sys
import time

import pytest
from test_gui import gui_config

from paperflow.progress_sync import settings


@pytest.mark.skipif(os.name != "nt", reason="Windows Tk GUI")
def test_gui_setup_creates_shared_progress_and_syncs_before_close(tmp_path, monkeypatch):
    if not os.environ.get("PAPERFLOW_SYNC_GUI_CHILD"):
        process = subprocess.run(
            [
                sys.executable,
                "-B",
                "-X",
                "utf8",
                "-m",
                "pytest",
                __file__ + "::test_gui_setup_creates_shared_progress_and_syncs_before_close",
                "-q",
                "--tb=short",
            ],
            env=dict(os.environ, PAPERFLOW_SYNC_GUI_CHILD="1"),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=45,
        )
        assert process.returncode == 0, process.stdout + process.stderr
        return
    import tkinter as tk

    from paperflow.gui import PaperFlowApp
    from paperflow.gui_service import GuiService

    monkeypatch.setattr("tkinter.messagebox.askyesno", lambda *a, **k: True)
    monkeypatch.setattr("tkinter.messagebox.showinfo", lambda *a, **k: None)
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda *a, **k: pytest.fail("Google APIへ通信しない")
    )
    errors = []
    monkeypatch.setattr("tkinter.messagebox.showerror", lambda *a, **k: errors.append(a))
    root = tk.Tk()
    root.withdraw()
    app = PaperFlowApp(root, gui_config(tmp_path), auto_load=False)

    def pump(until):
        deadline = time.monotonic() + 10
        while not until() and time.monotonic() < deadline:
            root.update()
            time.sleep(0.01)
        assert until(), errors

    try:
        assert len(app.tabs.tabs()) == 5
        app.tabs.select(app.cloud_tab)
        app.cloud_folder.set(str(tmp_path / "shared"))
        app.cloud_create()
        assert not app.busy  # 未確認なら共有フォルダを作成しない。
        assert not (tmp_path / "shared").exists()
        app.drive_checked.set(True)
        app.cloud_create()
        pump(lambda: not app.busy)
        assert not errors
        assert settings(app.service.config())["enabled"]
        assert "共有フォルダ" in app.cloud_status.get()
        assert "取り込み可能" in app.cloud_status.get()
        assert not GuiService(app.service.path).progress_confirmed()
        app.drive_checked.set(False)
        app.cloud_uncheck()
        assert not app.service.progress_confirmed()
        app.drive_checked.set(True)
        app.cloud_confirm()
        pump(lambda: not app.busy)
        assert app.service.progress_confirmed()
        app.close()
        assert app.busy
        pump(lambda: app._closed)
        assert not errors
    finally:
        if not app._closed:
            app._destroy()
