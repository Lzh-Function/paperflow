import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

import yaml

from paperflow.config import SettingsError
from paperflow.ingestion.pdf_discovery import is_redirect
from paperflow.models import digest


class WriteConflict(RuntimeError):
    pass


class SafeWriter:
    def __init__(self, config):
        self.config = config
        self.vault = config.destination.vault_dir
        self.root = self.vault / "PaperFlow"

    def preflight(self):
        if not self.config.destination.allow_vault_write:
            raise SettingsError(
                "Vaultへの初回書き込みは未承認です。承認後allow_vault_writeを設定してください"
            )
        if not self.vault.is_dir():
            raise FileNotFoundError(f"Vaultが接続されていません: {self.vault}")
        if (
            self.config.destination.require_wiki_marker
            and not (self.vault / ".llm-wiki-root").is_file()
        ):
            raise SettingsError(".llm-wiki-rootがありません。書き込みを中止します")
        lock = self.vault / "00_System/Locks/agent-write-lock.yml"
        if lock.exists():
            raise WriteConflict("LLM-Wikiのagent-write-lockがあります。書き込みを中止します")
        # Check all ancestors before resolve; resolving first hides junctions.
        for parent in [self.root, *self.root.parents]:
            if parent.exists() or parent.is_symlink():
                if is_redirect(parent):
                    raise SettingsError(f"リパースポイント経由の出力を拒否: {parent}")

    def target(self, relative: str) -> Path:
        path = Path(relative)
        if (
            path.is_absolute()
            or ".." in path.parts
            or "\\" in relative
            or ":" in relative
            or len(path.parts) != 2
            or path.parts[0] not in {"Papers", "Analysis", "Reports", "Inbox"}
            or path.suffix != ".md"
        ):
            raise SettingsError("PaperFlow専用出力先以外への書き込みを拒否しました")
        target = self.root / path
        for parent in [target, *target.parents]:
            if (parent.exists() or parent.is_symlink()) and is_redirect(parent):
                raise SettingsError(f"リパースポイントを拒否: {parent}")
        if not target.resolve().is_relative_to(self.root.resolve()):
            raise SettingsError("出力先がPaperFlowの外です")
        return target

    def write(self, relative: str, text: str, previous_hash: str | None = None):
        self.preflight()
        target = self.target(relative)
        content = text.encode("utf-8")
        generated_hash = digest(content)
        current = target.read_bytes() if target.exists() else None
        if current is not None and relative.startswith("Papers/"):
            return "preserved", digest(current)
        if current is not None:
            front = current.decode("utf-8").split("---", 2)
            try:
                managed = (
                    len(front) == 3
                    and front[0] == ""
                    and yaml.safe_load(front[1]).get("paperflow_managed") is True
                )
            except (yaml.YAMLError, AttributeError):
                managed = False
            if not managed:
                raise WriteConflict(f"管理対象外の既存ファイル: {relative}")
            # Recover a completed rename if the process stopped before committing its DB record.
            if digest(current) == generated_hash:
                return "unchanged", generated_hash
            if previous_hash is None or digest(current) != previous_hash:
                raise WriteConflict(f"手動編集との衝突: {relative}")
        target.parent.mkdir(parents=True, exist_ok=True)
        self.target(relative)
        fd, name = tempfile.mkstemp(prefix=".paperflow-", suffix=".tmp", dir=target.parent)
        temp = Path(name)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            for attempt in range(3):
                self.preflight()
                self.target(relative)
                observed = target.read_bytes() if target.exists() else None
                if observed != current:
                    raise WriteConflict(f"書き込み直前にファイルが変わりました: {relative}")
                try:
                    if current is None:
                        if os.name == "nt":
                            os.rename(
                                temp, target
                            )  # Windows rename never replaces an existing file.
                        else:
                            os.link(
                                temp, target
                            )  # Atomic exclusive creation for fixture platforms.
                            temp.unlink()
                    else:
                        os.replace(temp, target)
                    return "written", generated_hash
                except FileExistsError as exc:
                    raise WriteConflict(f"他のWriterが作成しました: {relative}") from exc
                except OSError:
                    if attempt == 2:
                        raise
                    time.sleep(0.1 * 2**attempt)
        finally:
            temp.unlink(missing_ok=True)


@contextmanager
def local_lock(data_dir: Path):
    """OS releases the lock even after a crash; the lock file itself is harmless."""
    data_dir.mkdir(parents=True, exist_ok=True)
    with (data_dir / "worker.lock").open("a+b") as handle:
        handle.seek(0)
        if handle.read(1) == b"":
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise WriteConflict("別のPaperFlowが実行中です") from exc
        else:
            import fcntl

            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise WriteConflict("別のPaperFlowが実行中です") from exc
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)
