import os

import pytest

from paperflow.config import SettingsError
from paperflow.obsidian.writer import SafeWriter, WriteConflict

TEXT = "---\npaperflow_managed: true\n---\n\n# 合成ノート\n"


@pytest.mark.parametrize(
    "path",
    [
        "../outside.md",
        "Analysis/../../outside.md",
        "/outside.md",
        "Analysis/nested/a.md",
        "..\\outside.md",
        "C:/outside.md",
    ],
)
def test_path_escape_refused(small_config, path):
    with pytest.raises(SettingsError):
        SafeWriter(small_config).write(path, TEXT)


def test_unmanaged_files_never_adopted(small_config):
    writer = SafeWriter(small_config)
    target = writer.target("Analysis/test.md")
    target.parent.mkdir(parents=True)
    target.write_text("# 人間のノート", encoding="utf-8")
    with pytest.raises(WriteConflict):
        writer.write("Analysis/test.md", TEXT)
    assert target.read_text(encoding="utf-8") == "# 人間のノート"


def test_replace_failure_preserves_old_file(small_config, monkeypatch):
    writer = SafeWriter(small_config)
    _, hash_ = writer.write("Analysis/test.md", TEXT)

    def fail(*args):
        raise PermissionError("Drive unavailable")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(PermissionError):
        writer.write("Analysis/test.md", TEXT + "changed", hash_)
    assert writer.target("Analysis/test.md").read_text(encoding="utf-8") == TEXT
    assert not list(writer.root.rglob("*.tmp"))


def test_symlink_escape(small_config, tmp_path):
    writer = SafeWriter(small_config)
    writer.root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (writer.root / "Analysis").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Windowsでシンボリックリンク作成権限なし。ジャンクション検証は別テスト。")
    with pytest.raises(SettingsError):
        writer.write("Analysis/escape.md", TEXT)
    assert not list(outside.iterdir())


@pytest.mark.skipif(os.name != "nt", reason="Windows専用ジャンクション検証")
def test_windows_junction_escape(small_config, tmp_path):
    import subprocess

    writer = SafeWriter(small_config)
    writer.root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    junction = writer.root / "Analysis"
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(outside)],
        check=False,
        capture_output=True,
    )
    assert result.returncode == 0
    with pytest.raises(SettingsError):
        writer.write("Analysis/escape.md", TEXT)
    assert not list(outside.iterdir())


def test_vault_marker_permission_and_wiki_lock(small_config):
    config = small_config
    writer = SafeWriter(config)
    config.destination.allow_vault_write = False
    with pytest.raises(SettingsError):
        writer.write("Analysis/a.md", TEXT)
    config.destination.allow_vault_write = True
    lock = config.destination.vault_dir / "00_System/Locks/agent-write-lock.yml"
    lock.parent.mkdir(parents=True)
    lock.write_text("host: another-device", encoding="utf-8")
    with pytest.raises(WriteConflict):
        writer.write("Analysis/a.md", TEXT)
