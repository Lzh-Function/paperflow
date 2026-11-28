import os
import subprocess
import sys
import time

import pytest
from conftest import tree
from test_gui import gui_config

from paperflow.progress_sync import settings


def test_confirm_on_another_pc_keeps_different_settings_until_explicit_share(tmp_path):
    import shutil

    from paperflow.gui_config import save_changes
    from paperflow.gui_service import GuiService
    from paperflow.gui_sync import profile_description

    home = GuiService(gui_config(tmp_path / "home"))
    lab = GuiService(gui_config(tmp_path / "lab"))
    shutil.rmtree(lab.config().source.paperpile_dir)
    shutil.copytree(home.config().source.paperpile_dir, lab.config().source.paperpile_dir)
    home.prepare("all")
    shared = tmp_path / "shared"
    home.create_progress_folder(shared, "合成共有")
    save_changes(lab.path, {"analysis.research_context": "別の研究分野"})
    lab.record_sync_error("以前の失敗")
    lab.join_progress_folder(shared)
    assert lab.sync_error() is None
    # 起動し直しても、設定が違うだけならDrive確認まで進める。
    restarted = GuiService(lab.path)
    before = tree(shared)
    restarted.confirm_progress()
    assert restarted.progress_confirmed()
    assert tree(shared) == before
    compatible, description = profile_description(
        restarted.config(), restarted.cloud().status()["shared_profile"]
    )
    assert not compatible and "研究分野の説明" in description
    assert restarted.config().analysis.research_context == "別の研究分野"
    restarted.progress_action("share_profile")
    assert (
        restarted.cloud().status()["shared_profile"]["analysis"]["research_context"]
        == "別の研究分野"
    )


def test_typing_a_new_shared_folder_resets_drive_confirmation():
    from types import SimpleNamespace as NS

    from paperflow.gui_sync import SyncPanel

    checked = [True]
    value = ["old-shared"]
    panel = NS(
        cloud_folder=NS(get=lambda: value[0]),
        drive_checked=NS(set=lambda v: checked.__setitem__(0, v)),
        service=NS(_sync_confirmation="previous"),
        _cloud_selected_folder="old-shared",
    )
    SyncPanel.cloud_folder_changed(panel)
    assert checked[0] and panel.service._sync_confirmation == "previous"
    value[0] = "new-shared"
    SyncPanel.cloud_folder_changed(panel)
    assert not checked[0] and panel.service._sync_confirmation is None


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

    approve = [False]
    monkeypatch.setattr("tkinter.messagebox.askyesno", lambda *a, **k: approve[0])
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
        assert len(app.tabs.tabs()) == 6
        app.tabs.select(app.cloud_tab)
        app.cloud_folder.set(str(tmp_path / "shared"))
        assert "最初のPC：共有を新規作成" in app.cloud_next.get()
        app.cloud_create()
        assert not app.busy  # 作成ダイアログで拒否すれば共有先へ書き込まない。
        assert not (tmp_path / "shared").exists()
        approve[0] = True  # 初回は作成ダイアログの確認だけでよい。
        app.cloud_create()
        pump(lambda: not app.busy)
        assert not errors
        assert settings(app.service.config())["enabled"]
        assert "共有フォルダ" in app.cloud_status.get()
        assert "取り込み可能" in app.cloud_status.get()
        assert app.drive_checked.get()
        assert "共有データを作成しました" in app.cloud_result.get()
        assert (tmp_path / "shared/paperflow-workspace.json").is_file()
        assert list((tmp_path / "shared/objects").glob("*.gz"))
        assert not GuiService(app.service.path).progress_confirmed()
        root.deiconify()
        root.update()
        # 通常のウィンドウ高さで、日常操作をスクロールせずに使える。
        for key in ["confirm", "sync", "exit"]:
            widget = app.cloud_actions[key]
            assert (
                widget.winfo_rooty() + widget.winfo_height()
                < root.winfo_rooty() + root.winfo_height()
            )
        assert not app.cloud_advanced_frame.winfo_ismapped()
        shared_before = tree(tmp_path / "shared")
        app.cloud_folder.set(str(tmp_path / "not-saved"))
        assert "未保存" in app.cloud_next.get()
        assert all(
            app.cloud_actions[k].instate(["disabled"]) for k in ["confirm", "sync", "exit", "share"]
        )
        app.cloud_sync()
        assert not app.busy and tree(tmp_path / "shared") == shared_before
        assert not (tmp_path / "not-saved").exists()
        app.cloud_folder.set(str(tmp_path / "shared"))
        app.drive_checked.set(False)
        app.cloud_uncheck()
        assert not app.service.progress_confirmed()
        app.service.record_sync_error("合成の以前の同期エラー")
        app.cloud_refresh()
        assert "合成の以前の同期エラー" in app.cloud_result.get()
        app.drive_checked.set(True)
        app.cloud_confirm()
        pump(lambda: not app.busy)
        assert app.service.progress_confirmed()
        assert app.service.sync_error() is None
        assert "合成の以前の同期エラー" not in app.cloud_result.get()
        # 参照のみの成功でも解決済みエラーを表示し続けない。確認を自動で付けない。
        app.service = GuiService(app.service.path)
        app.service.record_sync_error("受信前の古い失敗")
        app.cloud_refresh()
        before = tree(tmp_path / "shared")
        app.cloud_sync()
        pump(lambda: not app.busy)
        assert app.service.sync_error() is None
        assert not app.service.progress_confirmed() and not app.drive_checked.get()
        assert tree(tmp_path / "shared") == before
        # 設定ファイルを切り替えた画面に旧PCのパスや確認を残さない。
        previous = app.service.path
        other = gui_config(tmp_path / "other-settings")
        monkeypatch.setattr("tkinter.filedialog.askopenfilename", lambda **kw: str(other))
        app.choose_config()
        assert app.cloud_paths["destination.vault_dir"].get() == str(
            app.service.config().destination.vault_dir
        )
        assert app.cloud_folder.get() != str(tmp_path / "shared")
        assert not app.drive_checked.get()
        monkeypatch.setattr("tkinter.filedialog.askopenfilename", lambda **kw: str(previous))
        app.choose_config()
        pump(lambda: not app.busy)
        assert app.cloud_folder.get() == str(tmp_path / "shared")
        app.drive_checked.set(True)
        app.cloud_uncheck()
        assert not app.cloud_actions["confirm"].instate(["disabled"])
        app.cloud_confirm()
        pump(lambda: not app.busy)
        from paperflow.gui_config import save_changes

        original = app.service.config()
        vault_before = tree(original.destination.vault_dir)
        save_changes(app.service.path, {"analysis.research_context": "変更した研究分野"})
        app._reload_settings()
        assert "研究分野の説明" in app.cloud_profile.get()
        assert "異なります" in app.cloud_next.get()
        assert not app._cloud_profile_matches
        app.cloud_apply_profile()
        pump(lambda: not app.busy)
        saved = app.service.config()
        assert saved.analysis.research_context == original.analysis.research_context
        assert saved.analysis.allow_external_fulltext == original.analysis.allow_external_fulltext
        assert saved.destination.allow_vault_write == original.destination.allow_vault_write
        assert saved.source == original.source
        assert app._cloud_profile_matches
        assert tree(original.destination.vault_dir) == vault_before
        app.close()
        assert app.busy
        pump(lambda: app._closed)
        assert not errors
    finally:
        if not app._closed:
            app._destroy()


@pytest.mark.skipif(os.name != "nt", reason="Windows Tk GUI")
def test_path_fields_update_live_and_save_from_either_tab(tmp_path, monkeypatch):
    if not os.environ.get("PAPERFLOW_PATH_FIELDS_CHILD"):
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                "-X",
                "utf8",
                "-m",
                "pytest",
                __file__ + "::test_path_fields_update_live_and_save_from_either_tab",
                "-q",
                "--tb=short",
            ],
            env=dict(os.environ, PAPERFLOW_PATH_FIELDS_CHILD="1"),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=45,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return
    import shutil
    import tkinter as tk
    from tkinter import ttk

    from paperflow.gui import PaperFlowApp
    from paperflow.gui_service import GuiService
    from paperflow.pipeline import Pipeline
    from paperflow.store import Store

    path = gui_config(tmp_path)
    service = GuiService(path)
    plan = service.prepare("all")
    original = service.config().destination.vault_dir
    Pipeline(service.config()).run(paper_ids=[plan["items"][0]["paper_id"]])
    moved = tmp_path / "new-vault"
    shutil.copytree(original, moved)
    contents = tree(original)
    errors = []
    monkeypatch.setattr("tkinter.messagebox.showerror", lambda *a, **k: errors.append(a))
    monkeypatch.setattr("tkinter.messagebox.showinfo", lambda *a, **k: None)
    root = tk.Tk()
    root.withdraw()
    app = PaperFlowApp(root, path, auto_load=False)

    def pump():
        until = time.monotonic() + 12
        while app.busy and time.monotonic() < until:
            root.update()
            time.sleep(0.01)
        assert not app.busy

    def entries(widget):
        for child in widget.winfo_children():
            if isinstance(child, ttk.Entry):
                yield child
            yield from entries(child)

    try:
        field = next(
            w
            for w in entries(app.setting_tabs)
            if str(w.cget("textvariable")) == str(app.cloud_paths["destination.vault_dir"])
        )
        config_before = path.read_bytes()
        field.delete(0, "end")
        field.insert(0, str(moved))
        assert app.cloud_paths["destination.vault_dir"].get() == str(moved)
        assert app.cloud_paths_dirty() and "未保存" in app.cloud_path_hint.get()
        assert path.read_bytes() == config_before
        app.cloud_refresh()
        assert app.fields["destination.vault_dir"]() == str(moved)
        app.cloud_save_paths()
        pump()
        assert not errors and app.service.config().destination.vault_dir == moved
        assert not app.cloud_paths_dirty()
        # PC間同期側の編集も設定タブへ即時反映し、設定タブから保存できる。
        app.cloud_paths["destination.vault_dir"].set(str(original))
        assert app.fields["destination.vault_dir"]() == str(original)
        app.save_settings()
        pump()
        assert not errors and app.service.config().destination.vault_dir == original
        invalid = str(tmp_path / "unrelated-vault")
        config_before = path.read_bytes()
        app.cloud_paths["destination.vault_dir"].set(invalid)
        app.cloud_save_paths()
        pump()
        assert errors and path.read_bytes() == config_before
        assert app.fields["destination.vault_dir"]() == invalid
        assert app.cloud_paths["destination.vault_dir"].get() == invalid
        app._reload_settings()
        assert app.fields["destination.vault_dir"]() == str(original)
        assert not app.cloud_paths_dirty()
        assert tree(original) == contents and tree(moved) == contents
        with Store(app.service.config().storage.database, readonly=True) as store:
            assert len(store.rows("SELECT * FROM analysis_runs")) == 1
    finally:
        app._destroy()
