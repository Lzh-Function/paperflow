import hashlib
import json
import shutil
from pathlib import Path

import pytest
from create_demo import make_demo
from test_batches import batch_config as batch_fixture

from paperflow.agents.fake import FakeProvider
from paperflow.batches import execute, load, plan
from paperflow.config import load_config
from paperflow.pipeline import Pipeline
from paperflow.progress_sync import MemoryDrive, ProgressSync, SyncError, directory, head
from paperflow.store import Store
from paperflow.sync_snapshot import capture, packed, unpack

WORKSPACE = dict(id="synthetic-drive-folder", workspace_id="a" * 32)


@pytest.fixture
def wiki_config(tmp_path):
    return batch_fixture.__wrapped__(tmp_path)


class CountProvider(FakeProvider):
    def __init__(self):
        self.calls = 0

    def analyze(self, request):
        self.calls += 1
        return super().analyze(request)


@pytest.fixture
def two_pcs(tmp_path):
    a = load_config(make_demo(tmp_path / "home", count=3))
    b = load_config(make_demo(tmp_path / "lab", count=3))
    shutil.rmtree(b.source.paperpile_dir)
    shutil.copytree(a.source.paperpile_dir, b.source.paperpile_dir)
    for _ in range(2):
        Pipeline(a).run(scan_only=True)
    return a, b, MemoryDrive()


def test_saved_analysis_moves_between_paths_without_second_ai_call(two_pcs):
    a, b, drive = two_pcs
    provider = CountProvider()
    home = ProgressSync(a, drive)
    home.connect(WORKSPACE, create=True)
    home.begin()
    with Store(a.storage.database, readonly=True) as store:
        paper_id = store.rows("SELECT paper_id FROM papers ORDER BY paper_id")[0]["paper_id"]
    Pipeline(a, provider).run(paper_ids=[paper_id], export=False, checkpoint=home.checkpoint)
    home.end()
    assert provider.calls == 1
    lab = ProgressSync(b, drive)
    lab.connect(WORKSPACE)
    lab.begin()
    result = Pipeline(b, provider).run(paper_ids=[paper_id], export=True, checkpoint=lab.checkpoint)
    lab.end()
    assert result["analyzed"] == 0 and result["exported"] > 0
    assert provider.calls == 1
    with Store(b.storage.database, readonly=True) as store:
        assert len(store.rows("SELECT * FROM analysis_runs")) == 1
    raw = json.dumps(capture(b), ensure_ascii=False)
    assert str(a.source.paperpile_dir) not in raw and str(b.source.paperpile_dir) not in raw
    assert "refresh_token" not in raw


def test_offline_preserves_paid_result_and_retry_only_uploads(two_pcs):
    a, b, drive = two_pcs
    home = ProgressSync(a, drive)
    home.connect(WORKSPACE, create=True)
    home.begin()

    class Disconnect(CountProvider):
        def analyze(self, request):
            result = super().analyze(request)
            drive.fail = True
            return result

    provider = Disconnect()
    with pytest.raises(SyncError):
        Pipeline(a, provider).run(limit=1, export=False, checkpoint=home.checkpoint)
    with Store(a.storage.database, readonly=True) as store:
        assert store.rows("SELECT status FROM analysis_runs")[0]["status"] == "success"
    drive.fail = False
    home.sync()
    home.end()
    lab = ProgressSync(b, drive)
    lab.connect(WORKSPACE)
    assert provider.calls == 1


def test_remote_ack_lost_is_idempotent(two_pcs):
    a, _, drive = two_pcs
    original = drive.publish
    fail_once = [True]

    def publish(*args):
        original(*args)
        if fail_once.pop() if fail_once else False:
            raise SyncError("合成ACK消失")

    drive.publish = publish
    cloud = ProgressSync(a, drive)
    with pytest.raises(SyncError):
        cloud.connect(WORKSPACE, create=True)
    assert (directory(a) / "pending.json").exists()
    cloud.sync()
    assert len(drive.records[WORKSPACE["id"]]) == 1
    assert not (directory(a) / "pending.json").exists()


def test_prior_owner_blocks_and_explicit_takeover_keeps_unknown_blocked(two_pcs):
    a, b, drive = two_pcs
    home = ProgressSync(a, drive)
    home.connect(WORKSPACE, create=True)
    home.begin()
    with Store(a.storage.database) as store, store.transaction():
        paper = store.rows("SELECT paper_id FROM papers LIMIT 1")[0]
        store.db.execute(
            "INSERT INTO analysis_runs(run_id,paper_id,status) VALUES (?,?,?)",
            ("b" * 32, paper["paper_id"], "running"),
        )
    home.checkpoint()
    lab = ProgressSync(b, drive)
    lab.connect(WORKSPACE)
    with pytest.raises(SyncError, match="前PC"):
        lab.begin()
    lab.takeover()
    lab.begin()
    with pytest.raises(SyncError, match="未確認"):
        Pipeline(b).run(paper_ids=[paper["paper_id"]], checkpoint=lab.checkpoint)
    lab.end()
    with Store(b.storage.database, readonly=True) as store:
        assert store.paper(paper["paper_id"])["error_type"] == "sync_result_unknown"


def test_dirty_and_remote_changes_conflict_without_overwrite(two_pcs):
    a, b, drive = two_pcs
    home = ProgressSync(a, drive)
    home.connect(WORKSPACE, create=True)
    lab = ProgressSync(b, drive)
    lab.connect(WORKSPACE)
    with Store(b.storage.database) as store, store.transaction():
        store.db.execute("UPDATE papers SET error_message='合成のローカル変更'")
    home.begin()
    home.end()
    before = b.storage.database.read_bytes()
    with pytest.raises(SyncError, match="未送信変更"):
        lab.sync()
    assert b.storage.database.read_bytes() == before


def test_corrupt_snapshot_never_replaces_local_database(two_pcs):
    a, b, drive = two_pcs
    home = ProgressSync(a, drive)
    home.connect(WORKSPACE, create=True)
    latest = head(drive.commits(WORKSPACE["id"]))
    drive.blobs[latest["blob_id"]] = b"broken"
    with pytest.raises(ValueError, match="ハッシュ"):
        ProgressSync(b, drive).connect(WORKSPACE)
    assert not b.storage.database.exists()


def test_fork_is_not_resolved_by_timestamp():
    common = dict(version=1, parent=None)
    with pytest.raises(SyncError, match="分岐"):
        head([dict(common, commit_id="a"), dict(common, commit_id="b")])


def test_paths_and_expansion_are_checked(two_pcs):
    a, b, _ = two_pcs
    from paperflow.sync_snapshot import materialize

    data = capture(a)
    data["tables"]["attachments"][0]["source_path"] = "../outside.pdf"
    blob = packed(data)
    valid = unpack(blob, hashlib.sha256(blob).hexdigest())
    with pytest.raises(ValueError, match="範囲外"):
        materialize(b, valid)


@pytest.mark.parametrize("folder_mode", [False, True])
def test_fixed_batch_resumes_on_second_pc_and_back_without_duplicate_plan(
    wiki_config, tmp_path, folder_mode
):
    a = wiki_config
    b = a.model_copy(deep=True)
    b.source.paperpile_dir = tmp_path / "lab-source"
    shutil.copytree(a.source.paperpile_dir, b.source.paperpile_dir)
    b.source.bibtex_file = tmp_path / "lab.bib"
    shutil.copy2(a.source.bibtex_file, b.source.bibtex_file)
    b.destination.vault_dir = tmp_path / "lab-vault"
    shutil.copytree(a.destination.vault_dir, b.destination.vault_dir)
    b.storage.data_dir = tmp_path / "lab-cache"
    b.storage.database = b.storage.data_dir / "state.db"
    b.analysis.claude_command = "different-local-cli.exe"
    Pipeline(a).run(scan_only=True)
    path = Path(plan(a, count=3, seed=123)["plan"])
    if folder_mode:
        from paperflow.folder_sync import FolderTransport

        drive = FolderTransport(a, tmp_path / "batch-shared")
        workspace = drive.create_workspace("合成バッチ")
    else:
        drive, workspace = MemoryDrive(), WORKSPACE
    home = ProgressSync(a, drive, confirmed=True)
    home.connect(workspace, create=True)
    home.begin()
    stopped = [False]
    provider = CountProvider()
    first = execute(
        a,
        path,
        provider=provider,
        checkpoint=home.checkpoint,
        save_each_paper=True,
        should_stop=lambda: stopped[0],
        on_progress=lambda _: stopped.__setitem__(0, True),
    )
    home.end()
    assert first["registered"] == 1
    shutil.copytree(a.destination.vault_dir, b.destination.vault_dir, dirs_exist_ok=True)
    lab = ProgressSync(b, FolderTransport(b, drive.root) if folder_mode else drive, confirmed=True)
    lab.connect(workspace)
    lab.begin()
    imported = next((b.storage.data_dir / "batches").glob("*.plan.json"))
    assert [p["paper_id"] for p in load(a, path)["items"]] == [
        p["paper_id"] for p in load(b, imported)["items"]
    ]
    result = execute(
        b, imported, provider=provider, checkpoint=lab.checkpoint, save_each_paper=True
    )
    lab.end()
    assert result["registered"] == 3 and provider.calls == 3
    home.sync()
    assert len(capture(a)["batches"]) == 1
    with Store(a.storage.database, readonly=True) as store:
        assert len(store.rows("SELECT * FROM analysis_runs")) == 3


def test_empty_join_required_for_preexisting_library(two_pcs):
    a, b, drive = two_pcs
    home = ProgressSync(a, drive)
    home.connect(WORKSPACE, create=True)
    Pipeline(b).run(scan_only=True)
    before = b.storage.database.read_bytes()
    with pytest.raises(SyncError, match="既存の文献DB"):
        ProgressSync(b, drive).connect(WORKSPACE)
    assert b.storage.database.read_bytes() == before


def test_pending_upload_then_restart_never_imports_older_parent(two_pcs):
    a, _, drive = two_pcs
    cloud = ProgressSync(a, drive)
    cloud.connect(WORKSPACE, create=True)
    original = drive.publish

    def unavailable(*args):
        raise SyncError("合成アップロード失敗")

    drive.publish = unavailable
    with Store(a.storage.database) as store, store.transaction():
        store.db.execute("UPDATE papers SET error_message='保存後の新しい情報'")
    with pytest.raises(SyncError):
        cloud.sync()
    drive.publish = original
    restarted = ProgressSync(a, drive)
    restarted.sync()
    with Store(a.storage.database, readonly=True) as store:
        assert {r["error_message"] for r in store.rows("SELECT error_message FROM papers")} == {
            "保存後の新しい情報"
        }


def test_receive_write_failure_rolls_back_local_db(two_pcs, monkeypatch):
    a, b, drive = two_pcs
    home = ProgressSync(a, drive)
    home.connect(WORKSPACE, create=True)
    lab = ProgressSync(b, drive)
    lab.connect(WORKSPACE)
    with Store(b.storage.database, readonly=True) as store:
        before = list(store.db.iterdump())
    with Store(a.storage.database) as store, store.transaction():
        store.db.execute("UPDATE papers SET error_message='新しい共有状態'")
    home.sync()
    from paperflow import sync_snapshot

    real = sync_snapshot.insert

    def fail(db, tables):
        real(db, tables)
        if str(db.execute("PRAGMA database_list").fetchone()[2]):
            raise OSError("合成書き込み障害")

    monkeypatch.setattr(sync_snapshot, "insert", fail)
    with pytest.raises(OSError, match="合成"):
        lab.sync()
    with Store(b.storage.database, readonly=True) as store:
        assert list(store.db.iterdump()) == before


def test_legacy_fixed_batch_cache_is_migrated_only_with_verified_settings(wiki_config, tmp_path):
    from paperflow.batches import runtime_key
    from paperflow.wiki.bundle import json_write, sha

    a = wiki_config
    Pipeline(a).run(scan_only=True)
    path = Path(plan(a, count=1)["plan"])
    data = load(a, path)
    paper_id = data["items"][0]["paper_id"]
    provider = CountProvider()
    Pipeline(a, provider).run(paper_ids=[paper_id], export=False)
    with Store(a.storage.database) as store, store.transaction():
        store.db.execute("DELETE FROM portable_analyses")
    data.pop("portable_runtime")
    data["items"][0].pop("portable_bib")
    json_write(path, data)
    json_write(
        path.with_name(path.stem + ".progress.json"),
        dict(
            plan_sha256=sha(path),
            runtime_key=runtime_key(a),
            provider="fake",
            model=None,
            baseline_runs=[],
            paper_ids=[paper_id],
            status="stopped",
        ),
    )
    b = a.model_copy(deep=True)
    b.storage.data_dir = tmp_path / "legacy-lab"
    b.storage.database = b.storage.data_dir / "state.db"
    b.source.paperpile_dir = tmp_path / "legacy-lab-source"
    shutil.copytree(a.source.paperpile_dir, b.source.paperpile_dir)
    drive = MemoryDrive()
    home = ProgressSync(a, drive)
    home.connect(WORKSPACE, create=True)
    lab = ProgressSync(b, drive)
    lab.connect(WORKSPACE)
    lab.begin()
    Pipeline(b, provider).run(paper_ids=[paper_id], export=False, checkpoint=lab.checkpoint)
    lab.end()
    assert provider.calls == 1


def test_vault_arrival_wait_keeps_processed_state_without_resending(wiki_config, tmp_path):
    from paperflow.library import snapshot

    a = wiki_config
    b = a.model_copy(deep=True)
    b.storage.data_dir = tmp_path / "waiting-lab"
    b.storage.database = b.storage.data_dir / "state.db"
    b.destination.vault_dir = tmp_path / "waiting-vault"
    shutil.copytree(a.destination.vault_dir, b.destination.vault_dir)
    Pipeline(a).run(scan_only=True)
    provider = CountProvider()
    execute(a, Path(plan(a, count=1)["plan"]), provider=provider)
    drive = MemoryDrive()
    ProgressSync(a, drive).connect(WORKSPACE, create=True)
    lab = ProgressSync(b, drive)
    lab.connect(WORKSPACE)
    with pytest.raises(SyncError, match="同期待ち"):
        lab.begin()
    imported = [r for r in snapshot(b)["rows"] if r["imported"]]
    assert imported[0]["status"] == "取り込み済み・ファイル同期待ち"
    shutil.copytree(a.destination.vault_dir, b.destination.vault_dir, dirs_exist_ok=True)
    lab.begin()
    lab.end()
    assert provider.calls == 1
