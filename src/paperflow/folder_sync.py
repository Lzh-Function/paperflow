"""Driveデスクトップ版が同期する専用フォルダ。クラウドへの到達は検証しない。"""

import hashlib
import json
import os
import re
import tempfile
import uuid
from pathlib import Path

from paperflow.progress_sync import SyncError
from paperflow.store import now
from paperflow.sync_snapshot import MAX_PACKED

MARKER = "paperflow-workspace.json"


def validate_folder(cfg, path):
    path = Path(path).expanduser().absolute()
    for part in [path, *path.parents]:
        if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
            raise SyncError("共有フォルダにシンボリックリンク/ジャンクションは使用できません")
    path = path.resolve()
    if any(p.casefold() == ".obsidian" for p in path.parts):
        raise SyncError(".obsidianは共有先にできません")
    for protected in (
        cfg.source.paperpile_dir,
        cfg.destination.vault_dir,
        cfg.storage.data_dir,
        cfg.storage.database.parent,
    ):
        protected = protected.resolve()
        if path.is_relative_to(protected) or protected.is_relative_to(path):
            raise SyncError(
                "共有先はPaperpile・Vault・C:の作業データと分離した専用フォルダを選択してください"
            )
    return path


class FolderTransport:
    requires_confirmation = True

    def __init__(self, cfg, path):
        self.root = validate_folder(cfg, path)
        self._cache = {}

    def _path(self, relative):
        path = self.root / relative
        for part in [path, *path.parents]:
            if part == self.root.parent:
                break
            if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
                raise SyncError("共有ファイルにシンボリックリンク/ジャンクションがあります")
        if not path.resolve().is_relative_to(self.root):
            raise SyncError("共有ファイルが専用フォルダの外を参照しています")
        return path

    def _read(self, relative, limit):
        try:
            with self._path(relative).open("rb") as stream:
                data = stream.read(limit + 1)
        except FileNotFoundError as exc:
            raise SyncError(
                "共有ファイルの同期待ちです。両PCのDrive同期完了を確認してください：" + relative
            ) from exc
        if len(data) > limit:
            raise SyncError("共有ファイルの容量が上限を超えています")
        return data

    def _write(self, relative, data):
        path = self._path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if self._read(relative, max(MAX_PACKED, len(data))) != data:
                raise SyncError("同じ共有ファイル名に異なる内容があります。上書きせず停止しました")
            return
        fd, name = tempfile.mkstemp(prefix=".paperflow-", suffix=".pending", dir=path.parent)
        temp = Path(name)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            self._path(relative)
            try:
                if os.name == "nt":
                    os.rename(temp, path)
                else:
                    os.link(temp, path)
            except FileExistsError:
                if self._read(relative, max(MAX_PACKED, len(data))) != data:
                    raise SyncError("別のWriterが異なる共有ファイルを保存しました") from None
        finally:
            temp.unlink(missing_ok=True)

    def workspace(self):
        try:
            data = json.loads(self._read(MARKER, 65536))
        except (ValueError, UnicodeError) as exc:
            raise SyncError(
                "共有フォルダの識別ファイルが不正です。Driveの同期完了を確認してください"
            ) from exc
        if (
            data.get("type") != "paperflow-folder"
            or data.get("version") != 1
            or not re.fullmatch(r"[a-f0-9]{32}", data.get("workspace_id", ""))
        ):
            raise SyncError("PaperFlowの共有フォルダではありません")
        # Driveが競合コピーを生成した場合は、異なるライブラリを結合しない。
        if any(p.name != MARKER for p in self.root.glob("paperflow-workspace*.json")):
            raise SyncError(
                "共有フォルダの識別ファイルが重複しています。両PCの処理を止めて確認してください"
            )
        return dict(
            id=data["workspace_id"],
            workspace_id=data["workspace_id"],
            name=data["name"],
            transport="folder",
            shared_folder=str(self.root),
        )

    def create_workspace(self, name):
        if not name.strip() or len(name) > 100:
            raise SyncError("共有名を1〜100文字で入力してください")
        if self.root.exists() and any(self.root.iterdir()):
            raise SyncError(
                "新規作成は空の専用フォルダで行ってください。2台目は「既存の共有を引き継ぐ」を使います"
            )
        self.root.mkdir(parents=True, exist_ok=True)
        self._write(
            MARKER,
            json.dumps(
                dict(
                    type="paperflow-folder",
                    version=1,
                    workspace_id=uuid.uuid4().hex,
                    name=name.strip(),
                    created_at=now(),
                ),
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8"),
        )
        return self.workspace()

    def commits(self, folder):
        if self.workspace()["workspace_id"] != folder:
            raise SyncError("共有フォルダの識別子が変更されています")
        result = []
        self._path("commits")
        for path in sorted((self.root / "commits").glob("*.json")):
            if len(result) >= 20000:
                raise SyncError("共有セーブが2万件を超えています。履歴の整理が必要です")
            if not re.fullmatch(r"[a-f0-9]{32}\.json", path.name):
                raise SyncError("共有セーブに競合コピーがあります。上書きせず停止しました")
            try:
                info = self._path("commits/" + path.name).stat()
                stamp = (info.st_size, info.st_mtime_ns)
                cached = self._cache.get(path.name)
                if cached and cached[0] == stamp:
                    manifest = cached[1]
                else:
                    manifest = json.loads(self._read("commits/" + path.name, 65536))
                    self._cache[path.name] = (stamp, manifest)
            except (ValueError, UnicodeError) as exc:
                raise SyncError(
                    "共有セーブの同期待ち、または破損です。再度Driveの完了を確認してください"
                ) from exc
            if manifest.get("commit_id") != path.stem or manifest.get("workspace_id") != folder:
                raise SyncError("共有セーブのファイル名/識別子が一致しません")
            result.append(dict(manifest))
        return result

    def download(self, blob_id):
        if not isinstance(blob_id, str) or not re.fullmatch(r"[a-f0-9]{64}", blob_id):
            raise SyncError("共有データの識別子が不正です")
        data = self._read("objects/" + blob_id + ".json.gz", MAX_PACKED)
        if hashlib.sha256(data).hexdigest() != blob_id:
            raise SyncError("共有データのハッシュが一致しません")
        return data

    def publish(self, folder, manifest, blob):
        if self.workspace()["workspace_id"] != folder or manifest.get("workspace_id") != folder:
            raise SyncError("共有先の識別子が一致しません")
        commit_id = manifest.get("commit_id", "")
        if not re.fullmatch(r"[a-f0-9]{32}", commit_id):
            raise SyncError("共有セーブIDが不正です")
        if len(blob) > MAX_PACKED:
            raise SyncError("共有データが32MiBを超えています")
        blob_id = hashlib.sha256(blob).hexdigest()
        if manifest.get("blob_sha256") != blob_id:
            raise SyncError("送信前の共有データのハッシュが一致しません")
        self._write("objects/" + blob_id + ".json.gz", blob)
        payload = json.dumps(
            dict(manifest, blob_id=blob_id), ensure_ascii=False, sort_keys=True
        ).encode("utf-8")
        # デスクトップ版Driveはこの順序で届けるとは限らない。受信側は欠落時に停止する。
        self._write("commits/" + commit_id + ".json", payload)
