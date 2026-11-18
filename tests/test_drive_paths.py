import os
import shutil
from pathlib import Path

import pytest
from test_gui import gui_config

from paperflow import drive_paths
from paperflow.agents.fake import FakeProvider
from paperflow.batches import load, plan
from paperflow.config import load_config
from paperflow.drive_paths import drive_path, same_binding
from paperflow.gui_config import save_changes
from paperflow.gui_service import GuiService
from paperflow.pipeline import Pipeline
from paperflow.store import Store
from paperflow.sync_snapshot import capture, materialize, packed, unpack


def test_drive_display_name_alias_is_limited_to_drive_root():
    assert drive_path(r"G:\マイドライブ\Paperpile\a.pdf") == r"G:\My Drive\Paperpile\a.pdf"
    assert drive_path(r"H:\My Drive\LLM-Wiki\a.md") == r"H:\My Drive\LLM-Wiki\a.md"
    assert drive_path(r"C:\research\マイドライブ\a.pdf") == r"C:\research\マイドライブ\a.pdf"
    assert drive_path(r"\\server\share\マイドライブ\a.pdf") == r"\\server\share\マイドライブ\a.pdf"
    assert same_binding(
        {"source": r"G:\マイドライブ\Paperpile", "bib": None},
        {"source": r"G:\My Drive\Paperpile", "bib": None},
    )
    assert not same_binding(
        {"source": r"G:\マイドライブ\Paperpile"}, {"source": r"H:\My Drive\Paperpile"}
    )
    if os.name != "nt":
        assert not same_binding({"source": "/tmp/Case"}, {"source": "/tmp/case"})


def test_renamed_roots_keep_cached_analysis_exports_and_fixed_batches(tmp_path, monkeypatch):
    path = gui_config(tmp_path)
    old = load_config(path)
    for _ in range(2):
        Pipeline(old).run(scan_only=True)
    planned = plan(old, count=2)
    plan_path = Path(planned["plan"])
    paper_id = planned["items"][0]["paper_id"]
    Pipeline(old).run(paper_ids=[paper_id], export=False)
    source, vault = tmp_path / "new source", tmp_path / "new vault"
    shutil.move(old.source.paperpile_dir, source)
    shutil.move(old.destination.vault_dir, vault)
    cfg = save_changes(
        path, {"source.paperpile_dir": str(source), "destination.vault_dir": str(vault)}
    )
    aliases = [(old.source.paperpile_dir, source), (old.destination.vault_dir, vault)]
    actual = drive_paths.drive_path

    def alias(value):
        value = Path(value)
        for before, after in aliases:
            if value.is_relative_to(before):
                return str(after / value.relative_to(before))
        return actual(value)

    monkeypatch.setattr(drive_paths, "drive_path", alias)
    monkeypatch.setattr("paperflow.batches.drive_path", alias)
    # 名前変更後にスキャンして、旧パスと新パスの添付行が両方あるケース。
    for _ in range(2):
        Pipeline(cfg).run(scan_only=True)
    data = capture(cfg)
    assert len(data["tables"]["attachments"]) == 3
    assert len(data["batches"]) == 1
    local_plan = load(cfg, plan_path)
    assert local_plan["items"][0]["paper_id"] == paper_id
    assert Path(local_plan["items"][0]["pdf"]).is_relative_to(source)
    payload = packed(data)
    import hashlib

    materialize(cfg, unpack(payload, hashlib.sha256(payload).hexdigest()))

    class NeverAnalyze(FakeProvider):
        def analyze(self, request):
            pytest.fail("ドライブ名変更で再解析しない")

    assert Pipeline(cfg, NeverAnalyze()).run(paper_ids=[paper_id], export=False)["analyzed"] == 0
    with Store(cfg.storage.database, readonly=True) as store:
        assert store.paper(paper_id)["source_hash"]
        assert len(store.rows("SELECT * FROM analysis_runs")) == 1


def test_failed_snapshot_does_not_leave_half_initialized_shared_folder(tmp_path, monkeypatch):
    from paperflow import sync_snapshot

    path = gui_config(tmp_path)
    service = GuiService(path)
    folder = tmp_path / "shared"

    def broken(cfg):
        raise ValueError("合成の書き出しエラー")

    monkeypatch.setattr(sync_snapshot, "capture", broken)
    with pytest.raises(ValueError, match="書き出し"):
        service.create_progress_folder(folder, "合成")
    assert not folder.exists()
    service.record_sync_error("合成の書き出しエラー")
    assert "書き出し" in service.sync_error()["message"]
