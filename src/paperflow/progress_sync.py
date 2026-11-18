"""単一利用者のPC切り替え同期。分岐や通信失敗は解析失敗と区別して停止する。"""

import hashlib
import json
import socket
import uuid
from contextlib import nullcontext

from paperflow.models import digest
from paperflow.obsidian.writer import local_lock
from paperflow.portable import profile
from paperflow.store import Store, now
from paperflow.sync_snapshot import (
    backup_local,
    capture,
    packed,
    restore,
    rollback_local,
    unpack,
    vault_ready,
)
from paperflow.wiki.bundle import json_write


class SyncError(RuntimeError):
    pass


def directory(cfg):
    return cfg.storage.data_dir / "progress-sync"


def settings(cfg):
    path = directory(cfg) / "settings.json"
    if not path.exists():
        return dict(version=1, enabled=False, device_id="", device_name=socket.gethostname())
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("version") != 1:
        raise SyncError("この同期設定は新しいPaperFlowで作られています")
    return data


def save_settings(cfg, data):
    root = directory(cfg)
    root.mkdir(parents=True, exist_ok=True)
    json_write(root / "settings.json", data)


def head(commits):
    ids = {c["commit_id"] for c in commits}
    if len(ids) != len(commits):
        # Driveに同名ファイルがあっても同一内容の冪等再送だけを許す。
        unique = {}
        for c in commits:
            if c["commit_id"] in unique and unique[c["commit_id"]] != c:
                raise SyncError("共有セーブのIDが衝突しています")
            unique[c["commit_id"]] = c
        commits = list(unique.values())
    for c in commits:
        if (
            c.get("version") != 1
            or not isinstance(c.get("commit_id"), str)
            or c.get("parent") == c["commit_id"]
        ):
            raise SyncError("共有セーブの形式が不正です")
        if c.get("parent") and c["parent"] not in ids:
            raise SyncError("共有セーブの親がまだ取得できません。再度同期してください")
    heads = ids - {c.get("parent") for c in commits}
    if len(heads) > 1 or (commits and not heads):
        raise SyncError(
            "共有進捗が分岐しています。上書きせず停止しました。両PCのバックアップを確認してください"
        )
    if heads:
        by_id = {c["commit_id"]: c for c in commits}
        visited = set()
        current = next(iter(heads))
        while current:
            if current in visited:
                raise SyncError("共有セーブの親参照が循環しています")
            visited.add(current)
            current = by_id[current].get("parent")
        if visited != ids:
            raise SyncError("共有セーブの履歴が接続していません")
    return next((c for c in commits if c["commit_id"] in heads), None)


class ProgressSync:
    def __init__(self, cfg, transport=None, *, confirmed=False):
        self.cfg = cfg
        self.local = settings(cfg)
        self.transport = transport
        self.confirmed = confirmed

    @property
    def enabled(self):
        return self.local.get("enabled", False)

    def backend(self):
        if self.transport is None:
            if self.local.get("transport") != "folder" or not self.local.get("shared_folder"):
                raise SyncError(
                    "旧API方式は利用できません。「PC間同期」で共有フォルダを設定してください。Googleへの接続は行いません"
                )
            from paperflow.folder_sync import FolderTransport

            self.transport = FolderTransport(self.cfg, self.local["shared_folder"])
        return self.transport

    def _require_confirmation(self):
        if getattr(self.backend(), "requires_confirmation", False) and not self.confirmed:
            raise SyncError(
                "この起動ではDriveの同期完了が未確認です。「PC間同期」で両PCの同期完了を確認してから取り込みを許可してください"
            )

    def status(self):
        return dict(
            self.local,
            pending=(directory(self.cfg) / "pending.json").exists(),
            confirmed=self.confirmed,
            cloud_delivery="unverified",
        )

    def _remote(self):
        commits = self.backend().commits(self.local["folder_id"])
        result = head(commits)
        if self.local.get("head") and self.local["head"] not in {c["commit_id"] for c in commits}:
            raise SyncError(
                "前回このPCで確認した世代が共有フォルダにありません。古い世代へ戻さず停止しました。Driveの同期完了と共有先を確認してください"
            )
        if result and result.get("workspace_id") != self.local["workspace_id"]:
            raise SyncError("共有先の識別子が一致しません")
        if (
            result
            and result["commit_id"] == self.local.get("head")
            and self.local.get("head_manifest_hash")
            and self.local["head_manifest_hash"] != digest(result)
        ):
            raise SyncError("公開済みの共有セーブが書き換えられています。上書きせず停止しました")
        return result

    def _save(self):
        self.local["updated_at"] = now()
        save_settings(self.cfg, self.local)

    def connect(self, workspace, *, create=False):
        """GUIから選択/作成した専用領域だけを有効化する。"""
        with local_lock(self.cfg.storage.data_dir):
            if self.enabled and self.local.get("workspace_id") != workspace["workspace_id"]:
                raise SyncError("共有先の変更は現在の同期を解除してから行ってください")
            self.local.update(
                enabled=True,
                folder_id=workspace["id"],
                workspace_id=workspace["workspace_id"],
                device_id=self.local.get("device_id") or uuid.uuid4().hex,
            )
            if workspace.get("transport") == "folder":
                self.local.update(
                    transport="folder",
                    shared_folder=workspace["shared_folder"],
                    workspace_name=workspace["name"],
                )
            self._require_confirmation()
            if create:
                if self.backend().commits(workspace["id"]):
                    raise SyncError("新規共有先が空ではありません")
                self.local.update(head=None, base=None)
                self.local["backup"] = str(backup_local(self.cfg))
                self._save()
                self._publish(owner=None)
            else:
                if self._remote() is None:
                    raise SyncError(
                        "最初のPCの共有セーブがまだありません。Driveの同期完了を待ってから引き継いでください"
                    )
                if not self.local.get("head"):
                    with Store(self.cfg.storage.database, readonly=True) as store:
                        if store.rows("SELECT paper_id FROM papers LIMIT 1"):
                            raise SyncError(
                                "このPCには既存の文献DBがあります。空の作業フォルダとDBを設定してから参加してください。既存DBは変更していません"
                            )
                self.sync(_lock_held=True)
            self._save()
        return self.status()

    def sync(self, *, _lock_held=False, publish=True):
        if not self.enabled:
            return self.status()
        if publish:
            self._require_confirmation()
        lock = nullcontext() if _lock_held else local_lock(self.cfg.storage.data_dir)
        with lock:
            journal = directory(self.cfg) / "receiving.json"
            if journal.exists():
                receiving = json.loads(journal.read_text(encoding="utf-8"))
                if self.local.get("head") != receiving["commit_id"]:
                    rollback_local(self.cfg, directory(self.cfg) / "backups" / receiving["backup"])
                journal.unlink()
            remote = self._remote()
            if publish:
                self._recover_pending(remote)
            remote = self._remote()
            local_data = capture(self.cfg)
            fingerprint = digest(local_data)
            if remote and remote["commit_id"] != self.local.get("head"):
                if self.local.get("base") and fingerprint != self.local["base"]:
                    raise SyncError(
                        "このPCの未送信変更と別PCの進捗が両方あります。上書きせず停止しました"
                    )
                data = unpack(self.backend().download(remote["blob_id"]), remote["blob_sha256"])
                if digest(data) != remote["state_hash"]:
                    raise SyncError("共有データの内容ハッシュが一致しません")
                backup = restore(self.cfg, data, commit_id=remote["commit_id"])
                self.local.update(
                    head=remote["commit_id"],
                    base=digest(capture(self.cfg)),
                    shared_profile=data["profile"],
                    backup=backup,
                    head_manifest_hash=digest(remote),
                )
                self._save()
                journal.unlink(missing_ok=True)
            elif remote is None and self.local.get("head"):
                raise SyncError("共有進捗が見つかりません。別の共有先へ保存し直さず停止しました")
            elif publish and fingerprint != self.local.get("base"):
                if remote and remote.get("owner") not in (None, self.local["device_id"]):
                    raise SyncError(
                        "前PCの処理終了待ちです。未送信変更を保存する前に終了を確認してください"
                    )
                self._publish(owner=remote.get("owner") if remote else None)
            self.local["owner"] = remote.get("owner") if remote else None
            self.local["checked_at"] = now()
            self._save()
            return self.status()

    def _recover_pending(self, remote):
        path = directory(self.cfg) / "pending.json"
        if not path.exists():
            return
        pending = json.loads(path.read_text(encoding="utf-8"))
        commits = self.backend().commits(self.local["folder_id"])
        known = next((c for c in commits if c["commit_id"] == pending["commit_id"]), None)
        if known:
            if any(known.get(k) != v for k, v in pending.items()):
                raise SyncError("未送信セーブと同じIDの共有内容が一致しません")
            self.local.update(head=known["commit_id"], base=pending["state_hash"])
            self.local["head_manifest_hash"] = digest(known)
            self._save()
            path.unlink()
            (directory(self.cfg) / "pending.gz").unlink(missing_ok=True)
            return
        if (remote["commit_id"] if remote else None) != pending["parent"]:
            raise SyncError("未送信セーブと別PCの進捗が競合しています")
        blob = (directory(self.cfg) / "pending.gz").read_bytes()
        if hashlib.sha256(blob).hexdigest() != pending["blob_sha256"]:
            raise SyncError("未送信セーブが破損しています。ローカルDBは保持しています")
        self.backend().publish(self.local["folder_id"], pending, blob)
        verified = self._remote()
        if not verified or verified["commit_id"] != pending["commit_id"]:
            raise SyncError("保存した共有進捗を確認できませんでした")
        self.local.update(head=pending["commit_id"], base=pending["state_hash"])
        self.local["head_manifest_hash"] = digest(verified)
        self._save()
        path.unlink()
        (directory(self.cfg) / "pending.gz").unlink(missing_ok=True)

    def _publish(self, *, owner, takeover=False):
        self._require_confirmation()
        remote = self._remote()
        self._recover_pending(remote)
        remote = self._remote()
        if (remote["commit_id"] if remote else None) != self.local.get("head"):
            raise SyncError("別PCで進捗が更新されています。処理を停止して同期してください")
        if not takeover and remote and remote.get("owner") not in (None, self.local["device_id"]):
            raise SyncError("前PCの処理終了待ちです")
        data = capture(self.cfg)
        blob = packed(data)
        manifest = dict(
            version=1,
            workspace_id=self.local["workspace_id"],
            commit_id=uuid.uuid4().hex,
            parent=self.local.get("head"),
            device_id=self.local["device_id"],
            device_name=self.local["device_name"],
            owner=owner,
            created_at=now(),
            state_hash=digest(data),
            blob_sha256=hashlib.sha256(blob).hexdigest(),
        )
        root = directory(self.cfg)
        root.mkdir(parents=True, exist_ok=True)
        # DBを先に保存済み。通信の再試行ではこの同じセーブを再送する。
        (root / "pending.gz").write_bytes(blob)
        json_write(root / "pending.json", manifest)
        self._recover_pending(remote)
        self.local.update(owner=owner, shared_profile=data["profile"], checked_at=now())
        self._save()

    def begin(self):
        self._require_confirmation()
        if self.local.get("shared_profile") and self.local["shared_profile"] != profile(self.cfg):
            raise SyncError(
                "解析設定が共有設定と違います。「共有解析設定を適用」するか「現在の解析設定を共有」してください"
            )
        self.sync()
        with local_lock(self.cfg.storage.data_dir):
            remote = self._remote()
            if remote and remote.get("owner") not in (None, self.local["device_id"]):
                raise SyncError(
                    "前PCが処理中です。終了済みなら「前PCの終了を確認して引き継ぐ」を使ってください"
                )
            if self.cfg.analysis.default_provider != "fake" and not self.cfg.analysis.model:
                raise SyncError(
                    "PC間同期ではモデルIDを選択してください。端末ごとの自動選択は使いません"
                )
            shared = self.local.get("shared_profile")
            if shared and shared != profile(self.cfg):
                raise SyncError(
                    "解析設定が共有設定と違います。「共有解析設定を適用」してProvider・モデルを確認してください"
                )
            try:
                vault_ready(self.cfg)
            except ValueError as exc:
                raise SyncError(str(exc)) from exc
            with Store(self.cfg.storage.database) as store, store.transaction():
                store.db.execute(
                    "UPDATE papers SET error_type=NULL,error_message=NULL WHERE error_type='sync_vault_wait'"
                )
                # writer lock取得後に残るrunningは、同じPCの強制終了でも結果未確認。
                store.db.execute(
                    "UPDATE papers SET state='blocked',error_type='sync_result_unknown',error_message='前回の解析結果が未確認です。再送前に保存済み回答を確認してください' WHERE paper_id IN (SELECT paper_id FROM analysis_runs WHERE status='running')"
                )
                store.db.execute(
                    "UPDATE analysis_runs SET status='interrupted',error_type='sync_result_unknown',error_message='回答の保存を確認できませんでした' WHERE status='running'"
                )
            self._publish(owner=self.local["device_id"])

    def checkpoint(self):
        # Pipeline/バッチが既にWindowsのwriter lockを保持する。
        try:
            self._publish(owner=self.local["device_id"])
        except Exception as exc:
            raise SyncError(
                "進捗を共有できず停止しました。保存済みの要約はC:に残っています。再同期してください："
                + str(exc)
            ) from exc

    def end(self):
        with local_lock(self.cfg.storage.data_dir):
            self._publish(owner=None)
        return self.status()

    def takeover(self):
        """GUIの明示的な前PC終了確認後だけ呼ぶ。"""
        self.sync()
        with local_lock(self.cfg.storage.data_dir):
            remote = self._remote()
            # 前ownerを引き継ぐことを専用コミットに記録。
            previous = remote.get("owner") if remote else None
            self.local["device_id_before_takeover"] = previous
            self._publish(owner=None, takeover=True)
        return self.status()

    def share_profile(self):
        if not self.enabled:
            raise SyncError("先に共有フォルダを作成または選択してください")
        with local_lock(self.cfg.storage.data_dir):
            self._publish(owner=None)
        return self.status()

    def acknowledge(self):
        self._require_confirmation()
        with (
            local_lock(self.cfg.storage.data_dir),
            Store(self.cfg.storage.database) as store,
            store.transaction(),
        ):
            store.db.execute(
                "UPDATE papers SET state='metadata_ready',error_type=NULL,error_message=NULL WHERE error_type='sync_result_unknown'"
            )
        return self.sync()


class MemoryDrive:
    """合成2端末試験用。実ネットワークや実Google設定を参照しない。"""

    def __init__(self):
        self.records = {}
        self.blobs = {}
        self.fail = False

    def commits(self, folder):
        if self.fail:
            raise SyncError("合成通信障害")
        return [dict(c) for c in self.records.get(folder, [])]

    def download(self, blob_id):
        return self.blobs[blob_id]

    def publish(self, folder, manifest, blob):
        if self.fail:
            raise SyncError("合成通信障害")
        blob_id = manifest["commit_id"]
        self.blobs[blob_id] = blob
        entry = dict(manifest, blob_id=blob_id)
        if not any(c["commit_id"] == entry["commit_id"] for c in self.records.get(folder, [])):
            self.records.setdefault(folder, []).append(entry)
