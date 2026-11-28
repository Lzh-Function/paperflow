import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from conftest import tree
from test_gui import gui_config

from paperflow import batches, recovery
from paperflow.config import load_config
from paperflow.gui_config import save_changes
from paperflow.library import snapshot
from paperflow.obsidian.writer import WriteConflict
from paperflow.pipeline import Pipeline
from paperflow.reporting.reports import report
from paperflow.store import Store
from paperflow.sync_snapshot import capture, vault_ready
from paperflow.vault_paths import binding_matches, export_path, export_relative
from paperflow.wiki.bundle import apply, json_write
from paperflow.wiki.export import cached_items, prepare


@pytest.fixture
def saved(tmp_path):
    path = gui_config(tmp_path)
    cfg = load_config(path)
    for _ in range(2):
        Pipeline(cfg).run(scan_only=True)
    planned = batches.plan(cfg, count=2, seed=22)
    first, second = [i["paper_id"] for i in planned["items"]]
    Pipeline(cfg).run(paper_ids=[first])
    Pipeline(cfg).run(paper_ids=[second], export=False)
    job = recovery.prepare_job(cfg, [second], "save")
    json_write(recovery.job_path(cfg, job["job_id"]), job)
    bundle = cfg.storage.data_dir / "pending-vault-bundle"
    prepare(cfg, cached_items(cfg, [second]), bundle)
    return path, cfg, planned, job, bundle


def move(path, cfg, tmp_path):
    destination = tmp_path / "別ドライブ相当" / "新しい名前の Vault"
    destination.parent.mkdir()
    shutil.move(cfg.destination.vault_dir, destination)
    return save_changes(path, {"destination.vault_dir": str(destination)})


def forbid(*a, **k):
    pytest.fail("保存先の変更でAIを呼びました")


def test_move_keeps_legacy_exports_arrival_snapshot_report_and_library(
    saved, tmp_path, monkeypatch
):
    path, old, planned, job, bundle = saved
    cfg = move(path, old, tmp_path)
    with Store(cfg.storage.database, readonly=True) as store:
        exported = store.rows("SELECT * FROM exports")[0]
    before = tree(cfg.storage.data_dir)
    vault = tree(cfg.destination.vault_dir)
    source = tree(cfg.source.paperpile_dir)
    assert not old.destination.vault_dir.exists()
    assert str(old.destination.vault_dir) in exported["target_path"]
    vault_ready(cfg)
    assert export_path(cfg, exported).is_file()
    data = capture(cfg)
    assert len(data["batches"]) == 1
    assert not Path(data["tables"]["exports"][0]["target_path"]).is_absolute()
    assert str(old.destination.vault_dir) not in json.dumps(data, ensure_ascii=False)
    with Store(cfg.storage.database, readonly=True) as store:
        note = export_relative(cfg, exported)
        assert note[:-3] in report(store, min_priority=1, config=cfg)
    registry = batches.Registry(cfg)
    actual = Path.open

    def no_vault_reads(p, *a, **k):
        assert not p.is_relative_to(cfg.destination.vault_dir)
        return actual(p, *a, **k)

    with monkeypatch.context() as m:
        m.setattr(Path, "open", no_vault_reads)
        result = snapshot(cfg, registry)
    row = next(r for r in result["rows"] if r["paper_id"] == exported["paper_id"])
    assert row["imported"] and row["note_path"] == str(export_path(cfg, exported))
    assert tree(cfg.storage.data_dir) == before
    assert tree(cfg.destination.vault_dir) == vault and tree(cfg.source.paperpile_dir) == source


@pytest.mark.parametrize("legacy", [False, True])
def test_fixed_plan_and_recovery_resume_after_move_without_reanalysis(
    saved, tmp_path, monkeypatch, legacy
):
    path, old, planned, job, bundle = saved
    if legacy:
        original = json.loads(Path(planned["plan"]).read_text(encoding="utf-8"))
        original["binding"].pop("vault_identity", None)
        json_write(Path(planned["plan"]), original)
        job["binding"].pop("vault_identity", None)
        json_write(recovery.job_path(old, job["job_id"]), job)
    cfg = move(path, old, tmp_path)
    monkeypatch.setattr(Pipeline, "_provider", forbid)
    loaded = recovery.load_job(cfg, job["job_id"])
    result = recovery.execute(cfg, loaded)
    assert result["status"] == "complete", result
    assert result["items"][0]["external_calls"] == 0
    assert batches.load(cfg, Path(planned["plan"]))["selected_count"] == 2
    resumed = batches.execute(cfg, Path(planned["plan"]))
    assert resumed["registered"] == 2
    with Store(cfg.storage.database, readonly=True) as store:
        assert len(store.rows("SELECT * FROM analysis_runs")) == 2
        assert len(store.rows("SELECT * FROM exports")) == 2
    vault_ready(cfg)


def test_prepared_bundle_can_apply_at_identical_new_location(saved, tmp_path):
    path, old, planned, job, bundle = saved
    cfg = move(path, old, tmp_path)
    result = apply(cfg, bundle)
    assert result["written"] > 0
    assert len(list((cfg.destination.vault_dir / "10_Papers/20_Source_Notes").glob("*.md"))) == 2


@pytest.mark.parametrize("wrong_vault", [False, True])
def test_partial_bundle_is_restored_only_after_identifying_relocated_vault(
    saved, tmp_path, wrong_vault
):
    path, old, planned, job, bundle = saved
    plan = json.loads((bundle / "plan.json").read_text(encoding="utf-8"))
    entry = next(e for e in plan["changes"] if e["path"].startswith("10_Papers/20_Source_Notes/"))
    partial = old.destination.vault_dir / entry["path"]
    shutil.copyfile(bundle / "preview" / entry["path"], partial)
    json_write(bundle / "journal.json", dict(status="applying", touched=[entry]))
    cfg = move(path, old, tmp_path)
    if wrong_vault:
        human = cfg.destination.vault_dir / "50_Wiki/Concepts/human.md"
        human.write_text("# 人間の追加ノート\n保持する\n", encoding="utf-8")
        before = tree(cfg.destination.vault_dir)
        with pytest.raises(WriteConflict, match="一致しません"):
            apply(cfg, bundle)
        assert tree(cfg.destination.vault_dir) == before
    else:
        assert apply(cfg, bundle)["written"] > 0


def test_new_plan_without_exports_can_follow_empty_vault_copy(tmp_path):
    path = gui_config(tmp_path)
    cfg = load_config(path)
    for _ in range(2):
        Pipeline(cfg).run(scan_only=True)
    plan = batches.plan(cfg, count=1)
    assert plan["binding"]["vault_identity"].startswith("marker:")
    moved = move(path, cfg, tmp_path)
    assert batches.load(moved, Path(plan["plan"]))["items"] == plan["items"]


def test_pre_registration_marker_plan_can_use_saved_receipts_after_framework_update(
    saved, tmp_path
):
    path, cfg, plan, job, bundle = saved
    assert plan["binding"]["vault_identity"].startswith("marker:")
    marker = cfg.destination.vault_dir / ".llm-wiki-root"
    marker.write_text(
        marker.read_text(encoding="utf-8") + "\nframework_version: '1.13'\n", encoding="utf-8"
    )
    moved = move(path, cfg, tmp_path)
    assert batches.load(moved, Path(plan["plan"]))["selected_count"] == 2


@pytest.mark.parametrize(
    "damage", ["missing-receipt", "wrong-run", "missing-pdf", "changed-record"]
)
def test_gui_rejects_wrong_or_incomplete_vault_without_changing_config_or_db(
    saved, tmp_path, damage
):
    path, cfg, plan, job, bundle = saved
    other = tmp_path / "別の保存先"
    shutil.copytree(cfg.destination.vault_dir, other)
    with Store(cfg.storage.database, readonly=True) as store:
        row = store.rows("SELECT * FROM exports")[0]
    receipt_path = other / "00_System/PaperFlow/receipts" / (row["paper_id"] + ".json")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if damage == "missing-receipt":
        receipt_path.unlink()
    elif damage == "missing-pdf":
        (
            other / "10_Papers/10_Sources" / receipt["source_id"].split(":")[1] / "source.pdf"
        ).unlink()
    else:
        if damage == "wrong-run":
            receipt["analysis_run_id"] = "0" * 32
        else:
            note = next(n for n in receipt["managed"] if n.startswith("10_Papers/20_Source_Notes/"))
            receipt["managed"][note] = "0" * 64
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    config_before = path.read_bytes()
    db_before = cfg.storage.database.read_bytes()
    before = tree(other)
    with pytest.raises(ValueError):
        save_changes(path, {"destination.vault_dir": str(other)})
    assert path.read_bytes() == config_before and cfg.storage.database.read_bytes() == db_before
    assert tree(other) == before


def test_manual_edit_and_changed_bundle_are_never_overwritten_after_move(saved, tmp_path):
    path, cfg, plan, job, bundle = saved
    note = next((cfg.destination.vault_dir / "10_Papers/20_Source_Notes").glob("*.md"))
    note.write_text(note.read_text(encoding="utf-8") + "\n人間の追記\n", encoding="utf-8")
    moved = move(path, cfg, tmp_path)
    before = tree(moved.destination.vault_dir)
    with pytest.raises(WriteConflict, match="一致しません"):
        apply(moved, bundle)
    assert tree(moved.destination.vault_dir) == before


def test_vault_identity_does_not_accept_another_database_or_source(saved, tmp_path):
    path, old, plan, job, bundle = saved
    cfg = move(path, old, tmp_path)
    before = job["binding"]
    for name in ["database", "paperpile", "bibtex"]:
        wrong = dict(batches.binding(cfg), **{name: str(tmp_path / "other")})
        assert not binding_matches(cfg, before, wrong)


def test_same_source_with_a_different_adopted_version_can_move_without_adopting_it_in_db(
    saved, tmp_path
):
    path, cfg, plan, job, bundle = saved
    with Store(cfg.storage.database, readonly=True) as store:
        row = store.rows("SELECT * FROM exports")[0]
    receipt_path = (
        cfg.destination.vault_dir / "00_System/PaperFlow/receipts" / (row["paper_id"] + ".json")
    )
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    note_path = next(n for n in receipt["managed"] if n.startswith("10_Papers/20_Source_Notes/"))
    note = cfg.destination.vault_dir / note_path
    parts = note.read_text(encoding="utf-8").split("---", 2)
    fm = yaml.safe_load(parts[1])
    fm["analysis_run_id"] = "a" * 32
    receipt["analysis_run_id"] = fm["analysis_run_id"]
    note.write_text(
        "---\n" + yaml.safe_dump(fm, allow_unicode=True) + "---" + parts[2], encoding="utf-8"
    )
    from paperflow.wiki.bundle import sha

    receipt["managed"][note_path] = sha(note)
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    moved = move(path, cfg, tmp_path)
    assert export_relative(moved, row) == note_path
    with Store(moved.storage.database, readonly=True) as store:
        assert store.rows("SELECT * FROM exports")[0] == row
        assert not store.rows("SELECT * FROM analysis_runs WHERE run_id=?", ("a" * 32,))


def test_manual_config_change_and_dry_run_do_not_migrate_or_mutate_data(
    saved, tmp_path, monkeypatch
):
    path, old, plan, job, bundle = saved
    moved = tmp_path / "移動後"
    shutil.move(old.destination.vault_dir, moved)
    cfg = old.model_copy(deep=True)
    cfg.destination.vault_dir = moved
    before = tree(cfg.storage.data_dir)
    vault = tree(moved)
    monkeypatch.setattr(Pipeline, "_provider", forbid)
    vault_ready(cfg)
    Pipeline(cfg).run(dry_run=True)
    assert capture(cfg)["tables"]["exports"]
    assert tree(cfg.storage.data_dir) == before and tree(moved) == vault


@pytest.mark.parametrize("direction", ["parent", "child", "parent-with-old-copy"])
def test_vault_contents_can_move_to_parent_or_child_without_stale_prefix(
    tmp_path, monkeypatch, direction
):
    path = gui_config(tmp_path)
    cfg = load_config(path)
    original = tmp_path / "保存先" / "外側" / "元Vault"
    original.parent.mkdir(parents=True)
    shutil.move(cfg.destination.vault_dir, original)
    cfg = save_changes(path, {"destination.vault_dir": str(original)})
    for _ in range(2):
        Pipeline(cfg).run(scan_only=True)
    plan = batches.plan(cfg, count=1)
    Pipeline(cfg).run(paper_ids=[plan["items"][0]["paper_id"]])
    destination = original / "内側" / "Vault" if direction == "child" else original.parent
    holding = tmp_path / "一時退避"
    shutil.move(original, holding)
    destination.mkdir(parents=True, exist_ok=True)
    for child in holding.iterdir():
        shutil.move(child, destination / child.name)
    holding.rmdir()
    if direction == "parent-with-old-copy":
        with Store(cfg.storage.database, readonly=True) as store:
            old_record = store.rows("SELECT * FROM exports")[0]
        old_note = Path(old_record["target_path"])
        old_note.parent.mkdir(parents=True)
        old_note.write_text("古い場所に残った別の本文\n", encoding="utf-8")
    monkeypatch.setattr(Pipeline, "_provider", forbid)
    moved = save_changes(path, {"destination.vault_dir": str(destination)})
    vault_ready(moved)
    assert batches.execute(moved, Path(plan["plan"]))["registered"] == 1
    data = capture(moved)
    assert data["tables"]["exports"][0]["target_path"].startswith("10_Papers/20_Source_Notes/")
    with Store(moved.storage.database, readonly=True) as store:
        assert len(store.rows("SELECT * FROM analysis_runs")) == 1
        row = store.rows("SELECT * FROM exports")[0]
    assert (
        export_path(moved, row).parent == moved.destination.vault_dir / "10_Papers/20_Source_Notes"
    )
    if direction == "parent-with-old-copy":
        assert old_note.read_text(encoding="utf-8") == "古い場所に残った別の本文\n"


@pytest.mark.parametrize("old_prefix", [r"X:\old-library", r"\\old-server\share\library"])
def test_old_drive_or_unc_reference_resolves_without_accessing_the_old_location(saved, old_prefix):
    _, cfg, _, _, _ = saved
    with Store(cfg.storage.database, readonly=True) as store:
        row = store.rows("SELECT * FROM exports")[0]
    relative = export_relative(cfg, row)
    row["target_path"] = old_prefix + "\\" + relative.replace("/", "\\")
    assert export_relative(cfg, row) == relative
    assert export_path(cfg, row).is_file()


def test_multiple_destination_moves_preserve_original_saved_and_pending_work(
    saved, tmp_path, monkeypatch
):
    path, cfg, plan, job, _ = saved
    for name in ["1回目の移動", "2回目の移動"]:
        new_root = tmp_path / name
        shutil.move(cfg.destination.vault_dir, new_root)
        cfg = save_changes(path, {"destination.vault_dir": str(new_root)})
        vault_ready(cfg)
        assert len(capture(cfg)["batches"]) == 1
    monkeypatch.setattr(Pipeline, "_provider", forbid)
    assert recovery.execute(cfg, recovery.load_job(cfg, job["job_id"]))["status"] == "complete"
    assert batches.execute(cfg, Path(plan["plan"]))["registered"] == 2
    with Store(cfg.storage.database, readonly=True) as store:
        assert len(store.rows("SELECT * FROM analysis_runs")) == 2


def test_legacy_layout_can_move_contents_to_parent_without_reanalysis(
    small_config, tmp_path, monkeypatch
):
    cfg = small_config
    original = tmp_path / "旧形式の保存先" / "元Vault"
    original.parent.mkdir()
    shutil.move(cfg.destination.vault_dir, original)
    cfg.destination.vault_dir = original
    for _ in range(2):
        Pipeline(cfg).run(scan_only=True)
    assert Pipeline(cfg).run()["analyzed"] == 1
    destination = original.parent
    for child in original.iterdir():
        shutil.move(child, destination / child.name)
    original.rmdir()
    cfg.destination.vault_dir = destination
    before = cfg.storage.database.read_bytes()
    vault_ready(cfg)
    data = capture(cfg)
    assert len(data["tables"]["exports"]) == 2
    assert all(r["target_path"].startswith("PaperFlow/") for r in data["tables"]["exports"])
    assert cfg.storage.database.read_bytes() == before
    monkeypatch.setattr(Pipeline, "_provider", forbid)
    assert Pipeline(cfg).run()["analyzed"] == 0


@pytest.mark.skipif(os.name != "nt", reason="Windowsの実ドライブ文字の割り当てを検証")
def test_real_windows_drive_letter_change_preserves_resume_and_cleans_mapping(
    saved, tmp_path, monkeypatch
):
    import ctypes

    path, cfg, planned, job, _ = saved
    kernel = ctypes.windll.kernel32

    def device(letter):
        buffer = ctypes.create_unicode_buffer(32768)
        return buffer.value if kernel.QueryDosDeviceW(letter, buffer, len(buffer)) else None

    mask = kernel.GetLogicalDrives()
    letter = next(
        (
            c + ":"
            for c in "ZYXWVUTSRQPONMLKJ"
            if not mask & (1 << (ord(c) - ord("A"))) and device(c + ":") is None
        ),
        None,
    )
    assert letter is not None, "検証用の未使用ドライブ文字がありません"
    disk = tmp_path / "合成ドライブ"
    disk.mkdir()
    destination = disk / "移動後Vault"
    shutil.move(cfg.destination.vault_dir, destination)
    subprocess.run(["subst", letter, str(disk)], check=True, capture_output=True)
    created = device(letter)
    assert created is not None
    try:
        selected = letter + "\\移動後Vault"
        monkeypatch.setattr(Pipeline, "_provider", forbid)
        moved = save_changes(path, {"destination.vault_dir": selected})
        vault_ready(moved)
        result = recovery.execute(moved, recovery.load_job(moved, job["job_id"]))
        assert result["status"] == "complete", result
        assert result["items"][0]["external_calls"] == 0
        assert batches.execute(moved, Path(planned["plan"]))["registered"] == 2
        assert len(capture(moved)["tables"]["exports"]) == 2
    finally:
        assert device(letter) == created, "ドライブ割り当てが外部で変更されたため解除しません"
        subprocess.run(["subst", letter, "/D"], check=True, capture_output=True)
        assert device(letter) is None
