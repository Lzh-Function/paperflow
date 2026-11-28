import json
import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import tree
from test_gui import gui_config

from paperflow import batches, paths
from paperflow.gui_config import save_changes
from paperflow.gui_service import GuiService
from paperflow.pipeline import Pipeline
from paperflow.store import Store
from paperflow.sync_snapshot import vault_ready
from paperflow.wiki.bundle import target
from paperflow.wiki.export import run_script

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows cryptoFs互換")


def volume_error(code=1005):
    error = OSError("合成の仮想ボリュームエラー")
    error.winerror = code
    return error


def emulate_crypto(monkeypatch, root):
    original = paths._NATIVE_RESOLVE

    def resolve(path, *, strict=False):
        absolute = path if path.is_absolute() else Path.cwd() / path
        if absolute == root or absolute.is_relative_to(root):
            raise volume_error()
        return original(path, strict=strict)

    monkeypatch.setattr(paths, "_NATIVE_RESOLVE", resolve)
    monkeypatch.setattr(paths, "_cryptomator_volume", lambda root: True)


def test_cryptofs_resolution_keeps_existing_and_uncreated_paths_without_writes(
    tmp_path, monkeypatch
):
    root = tmp_path / "crypto"
    root.mkdir()
    note = root / "日本語.md"
    note.write_text("合成本文", encoding="utf-8")
    before = tree(root)
    emulate_crypto(monkeypatch, root)
    assert paths.resolve_path(note) == note
    assert paths.resolve_path(root / "未作成" / "note.md") == root / "未作成/note.md"
    with pytest.raises(FileNotFoundError):
        paths.resolve_path(root / "未作成/note.md", strict=True)
    with pytest.raises(OSError):
        paths.resolve_path(root / ".." / "outside.md")
    assert tree(root) == before


def test_relative_paths_in_virtual_working_directory_keep_safe_resolution(tmp_path, monkeypatch):
    root = tmp_path / "crypto"
    root.mkdir()
    emulate_crypto(monkeypatch, root)
    monkeypatch.chdir(root)
    with paths.script_paths():
        assert Path("future/note.md").resolve() == root / "future/note.md"
        with pytest.raises(OSError):
            Path("../outside.md").resolve()


@pytest.mark.parametrize("code,cryptofs", [(5, True), (1, True), (1005, False)])
def test_unrelated_filesystem_and_access_errors_are_not_bypassed(
    tmp_path, monkeypatch, code, cryptofs
):
    error = volume_error(code)

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(paths, "_NATIVE_RESOLVE", fail)
    monkeypatch.setattr(paths, "_cryptomator_volume", lambda root: cryptofs)
    with pytest.raises(OSError) as caught:
        paths.resolve_path(tmp_path)
    assert caught.value is error


def test_reparse_points_and_unavailable_volumes_still_stop(tmp_path, monkeypatch):
    root = tmp_path / "crypto"
    root.mkdir()
    unsafe = root / "redirect"
    unsafe.mkdir()
    emulate_crypto(monkeypatch, root)
    original = Path.lstat

    def lstat(path):
        if path == unsafe:
            return SimpleNamespace(st_mode=0o40755, st_file_attributes=0x410)
        return original(path)

    monkeypatch.setattr(Path, "lstat", lstat)
    with pytest.raises(ValueError, match="リパースポイント"):
        paths.resolve_path(unsafe / "note.md")
    with pytest.raises(ValueError, match="リパースポイント"):
        target(root, "redirect/note.md")

    # パス解決前にボリュームを利用できない場合も、単なる文字列へフォールバックしない。
    def locked(root):
        raise PermissionError("合成のロック済みボリューム")

    monkeypatch.setattr(paths, "_cryptomator_volume", locked)
    with pytest.raises(PermissionError, match="ロック済み"):
        paths.resolve_path(root)


def test_crypto_destination_saves_and_resumes_then_shares_without_second_analysis(
    tmp_path, monkeypatch
):
    service = GuiService(gui_config(tmp_path))
    plan = service.prepare("all")
    cfg = service.config()
    first = plan["items"][0]["paper_id"]
    assert Pipeline(cfg).run(paper_ids=[first])["analyzed"] == 1
    moved = tmp_path / "仮想Vault"
    shutil.move(cfg.destination.vault_dir, moved)
    emulate_crypto(monkeypatch, moved)
    cfg = save_changes(service.path, {"destination.vault_dir": str(moved)})
    vault_ready(cfg)
    assert batches.binding(cfg)["vault"] == os.path.normcase(str(moved))
    result = service.run(plan["plan"], "fake", None)
    assert result["registered"] == 3 and result["usage"]["analysis_runs"] == 2
    service.create_progress_folder(tmp_path / "shared", "合成共有")
    restarted = GuiService(service.path)
    before = tree(tmp_path / "shared")
    restarted.progress_sync()
    assert tree(tmp_path / "shared") == before
    assert not restarted.progress_confirmed()
    restarted.confirm_progress()
    assert restarted.progress_confirmed()
    assert restarted.run(plan["plan"], "fake", None)["usage"]["analysis_runs"] == 2
    with Store(restarted.config().storage.database, readonly=True) as store:
        assert len(store.rows("SELECT * FROM analysis_runs")) == 3


def test_existing_wiki_scripts_get_same_compatibility_in_isolated_child(tmp_path):
    root = tmp_path / "crypto"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    script = scripts / "probe.py"
    script.write_text(
        """import json, os
from pathlib import Path
from paperflow import paths

def fail(*args, **kwargs):
    error = OSError('synthetic')
    error.winerror = 1005
    raise error
paths._NATIVE_RESOLVE = fail
paths._cryptomator_volume = lambda root: True
root = Path(os.environ['LLM_WIKI_ROOT'])
print(json.dumps({'root': str(root.resolve()), 'args': __import__('sys').argv[1:]}))
""",
        encoding="utf-8",
    )
    original = Path.resolve
    data = run_script(root, "probe.py", ["日本語", "--test"], expect_json=True)
    assert data == {"root": str(root), "args": ["日本語", "--test"]}
    assert Path.resolve is original
    with paths.script_paths():
        assert Path.resolve is not original
    assert Path.resolve is original
    assert json.loads(run_script(root, "probe.py"))["root"] == str(root)
