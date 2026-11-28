import shutil

import pytest
from conftest import tree
from test_progress_sync import two_pcs as two_fixture

from paperflow.folder_sync import FolderTransport, validate_folder
from paperflow.pipeline import Pipeline
from paperflow.progress_sync import ProgressSync, SyncError, directory, save_settings
from paperflow.store import Store


@pytest.fixture
def pcs(tmp_path):
    return two_fixture.__wrapped__(tmp_path)


def connected(cfg, folder):
    transport = FolderTransport(cfg, folder)
    cloud = ProgressSync(cfg, transport, confirmed=True)
    cloud.connect(transport.create_workspace("合成共有"), create=True)
    return cloud


def test_sync_never_shares_edited_analysis_settings_without_explicit_choice(pcs, tmp_path):
    a, _, _ = pcs
    folder = tmp_path / "shared"
    home = connected(a, folder)
    before = tree(folder)
    original = home.status()["shared_profile"]
    a.analysis.research_context = "このPCで編集した研究分野"
    with pytest.raises(SyncError, match="現在の解析設定を共有"):
        home.sync()
    assert tree(folder) == before
    assert home.status()["shared_profile"] == original
    home.share_profile()
    assert (
        home.status()["shared_profile"]["analysis"]["research_context"]
        == a.analysis.research_context
    )
    home.begin()
    home.end()


@pytest.mark.parametrize("edit_progress", [False, True])
def test_receive_with_local_setting_edits_keeps_settings_and_detects_real_conflict(
    pcs, tmp_path, edit_progress
):
    a, b, _ = pcs
    folder = tmp_path / "shared"
    home = connected(a, folder)
    lab = ProgressSync(b, FolderTransport(b, folder), confirmed=True)
    lab.connect(FolderTransport(b, folder).workspace())
    b.analysis.research_context = "ラボPCで選び直した設定"
    if edit_progress:
        with Store(b.storage.database) as store, store.transaction():
            store.db.execute("UPDATE papers SET error_message='このPCだけの進捗変更'")
    home.begin()
    home.end()
    before = tree(folder)
    if edit_progress:
        with pytest.raises(SyncError, match="未送信変更"):
            lab.sync(publish=False)
        with Store(b.storage.database, readonly=True) as store:
            assert (
                store.rows("SELECT error_message FROM papers")[0]["error_message"]
                == "このPCだけの進捗変更"
            )
    else:
        lab.sync(publish=False)
        assert lab.status()["head"] == home.status()["head"]
        assert lab.status()["shared_profile"] == home.status()["shared_profile"]
    assert b.analysis.research_context == "ラボPCで選び直した設定"
    assert tree(folder) == before


def test_folder_roundtrip_without_google_api_or_credential_setup(pcs, tmp_path, monkeypatch):
    from test_progress_sync import CountProvider

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: pytest.fail("API通信禁止"))
    a, b, _ = pcs
    folder = tmp_path / "drive-shared"
    home = connected(a, folder)
    home.begin()
    provider = CountProvider()
    with Store(a.storage.database, readonly=True) as store:
        paper_id = store.rows("SELECT paper_id FROM papers LIMIT 1")[0]["paper_id"]
    Pipeline(a, provider).run(paper_ids=[paper_id], export=False, checkpoint=home.checkpoint)
    home.end()
    lab = ProgressSync(b, FolderTransport(b, folder), confirmed=True)
    lab.connect(FolderTransport(b, folder).workspace())
    lab.begin()
    Pipeline(b, provider).run(paper_ids=[paper_id], export=True, checkpoint=lab.checkpoint)
    lab.end()
    assert provider.calls == 1
    assert lab.status()["cloud_delivery"] == "unverified"
    assert not (directory(a) / "google.dpapi").exists()
    assert not list(folder.rglob("*.db"))
    assert not list(folder.rglob("*.pending"))


def test_same_shared_folder_can_move_without_resetting_db_or_known_history(pcs, tmp_path):
    a, _, _ = pcs
    old = tmp_path / "old-shared"
    cloud = connected(a, old)
    initial = cloud.status()
    moved = tmp_path / "new-shared"
    shutil.move(old, moved)
    transport = FolderTransport(a, moved)
    db_before = a.storage.database.read_bytes()
    with pytest.raises(SyncError, match="未確認"):
        ProgressSync(a, transport).connect(transport.workspace())
    renewed = ProgressSync(a, transport, confirmed=True)
    renewed.connect(transport.workspace())
    assert renewed.status()["head"] == initial["head"]
    assert renewed.status()["workspace_id"] == initial["workspace_id"]
    assert renewed.status()["shared_folder"] == str(moved)
    assert a.storage.database.read_bytes() == db_before


def test_startup_receives_but_does_not_publish_without_fresh_confirmation(pcs, tmp_path):
    a, b, _ = pcs
    folder = tmp_path / "shared"
    home = connected(a, folder)
    lab = ProgressSync(b, FolderTransport(b, folder), confirmed=True)
    lab.connect(FolderTransport(b, folder).workspace())
    with Store(a.storage.database) as store, store.transaction():
        store.db.execute("UPDATE papers SET error_message='共有の新しい記録'")
    home.sync()
    restarted = ProgressSync(b)
    before = tree(folder)
    restarted.sync(publish=False)
    assert tree(folder) == before
    with Store(b.storage.database, readonly=True) as store:
        assert (
            store.rows("SELECT error_message FROM papers")[0]["error_message"] == "共有の新しい記録"
        )
    with pytest.raises(SyncError, match="未確認"):
        restarted.begin()
    with pytest.raises(SyncError, match="未確認"):
        restarted.sync()
    assert tree(folder) == before


def test_unconfirmed_startup_keeps_unsaved_local_results_local(pcs, tmp_path):
    a, _, _ = pcs
    folder = tmp_path / "shared"
    connected(a, folder)
    with Store(a.storage.database) as store, store.transaction():
        store.db.execute("UPDATE papers SET error_message='C:だけの未保存結果'")
    before = tree(folder)
    ProgressSync(a).sync(publish=False)
    assert tree(folder) == before


def test_cached_old_folder_never_rolls_back_known_local_head(pcs, tmp_path):
    a, b, _ = pcs
    folder, stale = tmp_path / "shared", tmp_path / "stale-cache"
    home = connected(a, folder)
    shutil.copytree(folder, stale)
    lab = ProgressSync(b, FolderTransport(b, folder), confirmed=True)
    lab.connect(FolderTransport(b, folder).workspace())
    home.begin()
    home.end()
    lab.sync()
    before = b.storage.database.read_bytes()
    lab.transport = FolderTransport(b, stale)
    with pytest.raises(SyncError, match="古い世代"):
        lab.sync(publish=False)
    assert b.storage.database.read_bytes() == before


def test_manifest_arriving_before_object_waits_without_import(pcs, tmp_path):
    a, b, _ = pcs
    folder = tmp_path / "shared"
    connected(a, folder)
    blob = next((folder / "objects").glob("*.gz"))
    payload = blob.read_bytes()
    blob.unlink()
    transport = FolderTransport(b, folder)
    lab = ProgressSync(b, transport, confirmed=True)
    with pytest.raises(SyncError, match="同期待ち"):
        lab.connect(transport.workspace())
    assert not b.storage.database.exists()
    blob.write_bytes(payload)
    lab.connect(transport.workspace())
    assert b.storage.database.exists()


def test_duplicate_conflict_filename_is_not_silently_ignored(pcs, tmp_path):
    a, _, _ = pcs
    folder = tmp_path / "shared"
    home = connected(a, folder)
    commit = next((folder / "commits").glob("*.json"))
    shutil.copy2(commit, commit.with_stem(commit.stem + " (conflicted copy)"))
    with pytest.raises(SyncError, match="競合コピー"):
        home.sync()


def test_data_and_manifest_publication_is_idempotent_and_content_checked(pcs, tmp_path):
    a, _, _ = pcs
    folder = tmp_path / "shared"
    home = connected(a, folder)
    transport = home.backend()
    manifest = transport.commits(home.local["folder_id"])[0]
    blob = transport.download(manifest["blob_id"])
    before = tree(folder)
    transport.publish(
        home.local["folder_id"], {k: v for k, v in manifest.items() if k != "blob_id"}, blob
    )
    assert tree(folder) == before
    with pytest.raises(SyncError, match="ハッシュ"):
        transport.publish(home.local["folder_id"], manifest, blob + b"changed")
    assert tree(folder) == before


def test_different_folders_with_delayed_delivery_detect_fork(pcs, tmp_path):
    a, b, _ = pcs
    first, second = tmp_path / "view-a", tmp_path / "view-b"
    home = connected(a, first)
    shutil.copytree(first, second)
    lab = ProgressSync(b, FolderTransport(b, second), confirmed=True)
    lab.connect(FolderTransport(b, second).workspace())
    home.begin()
    lab.begin()
    # 同期前に両端末が開始した事故を再現。Driveの配送後に分岐を検出する。
    shutil.copytree(second, first, dirs_exist_ok=True)
    with pytest.raises(SyncError, match="分岐"):
        home.sync()


def test_shared_folder_cannot_overlap_original_vault_or_cache(pcs, tmp_path):
    a, _, _ = pcs
    for path in (
        a.source.paperpile_dir,
        a.source.paperpile_dir / "shared",
        a.destination.vault_dir / "shared",
        a.storage.data_dir / "shared",
        tmp_path,
    ):
        with pytest.raises(SyncError, match="分離"):
            validate_folder(a, path)
    folder = tmp_path / "valid-shared"
    assert validate_folder(a, folder) == folder.resolve()
    folder.mkdir()
    (folder / "existing.txt").write_text("保持", encoding="utf-8")
    with pytest.raises(SyncError, match="空の"):
        FolderTransport(a, folder).create_workspace("合成")
    assert (folder / "existing.txt").read_text(encoding="utf-8") == "保持"


def test_join_missing_marker_does_not_create_a_new_library(pcs, tmp_path):
    a, _, _ = pcs
    folder = tmp_path / "not-arrived"
    with pytest.raises(SyncError, match="同期待ち"):
        FolderTransport(a, folder).workspace()
    assert not folder.exists()


def test_join_marker_only_does_not_publish_an_empty_library(pcs, tmp_path):
    a, b, _ = pcs
    folder = tmp_path / "marker-only"
    transport = FolderTransport(a, folder)
    workspace = transport.create_workspace("配送待ち")
    with pytest.raises(SyncError, match="共有セーブがまだ"):
        ProgressSync(b, FolderTransport(b, folder), confirmed=True).connect(workspace)
    assert not (folder / "commits").exists()
    assert not b.storage.database.exists()


def test_old_api_settings_cannot_trigger_network_requests(pcs, monkeypatch):
    a, _, _ = pcs
    save_settings(
        a,
        dict(
            version=1,
            enabled=True,
            folder_id="old-drive-id",
            workspace_id="a" * 32,
            device_id="b" * 32,
            device_name="old-pc",
        ),
    )
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: pytest.fail("API通信禁止"))
    with pytest.raises(SyncError, match="旧API方式"):
        ProgressSync(a).sync()


def test_dry_run_with_folder_enabled_has_no_disk_or_shared_writes(pcs, tmp_path):
    a, _, _ = pcs
    folder = tmp_path / "shared"
    connected(a, folder)
    before, shared_before = tree(a.storage.data_dir), tree(folder)
    Pipeline(a).run(dry_run=True)
    assert tree(a.storage.data_dir) == before
    assert tree(folder) == shared_before


def test_same_pc_crash_keeps_unknown_result_blocked(pcs, tmp_path):
    a, _, _ = pcs
    folder = tmp_path / "shared"
    home = connected(a, folder)
    home.begin()
    with Store(a.storage.database) as store, store.transaction():
        paper_id = store.rows("SELECT paper_id FROM papers LIMIT 1")[0]["paper_id"]
        store.db.execute(
            "INSERT INTO analysis_runs(run_id,paper_id,status) VALUES (?,?,?)",
            ("a" * 32, paper_id, "running"),
        )
    home.checkpoint()
    restarted = ProgressSync(a, confirmed=True)
    restarted.begin()
    with pytest.raises(SyncError, match="未確認"):
        Pipeline(a).run(paper_ids=[paper_id], checkpoint=restarted.checkpoint)
    restarted.end()
    with Store(a.storage.database, readonly=True) as store:
        assert store.paper(paper_id)["error_type"] == "sync_result_unknown"
