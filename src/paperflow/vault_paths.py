"""Vaultの場所と保存済み文献の同一性を分ける。参照時の解決だけでDBは変更しない。"""

import json
import re
from pathlib import Path, PurePosixPath, PureWindowsPath

import yaml

from paperflow.drive_paths import drive_path, relative_path, same_binding
from paperflow.models import digest
from paperflow.store import Store
from paperflow.wiki.bundle import sha, target


def _relative(cfg, name):
    path = PurePosixPath(name)
    if (
        not name
        or not path.parts
        or path.is_absolute()
        or "\\" in name
        or ":" in name
        or any(p == ".." or p.casefold() == ".obsidian" for p in path.parts)
    ):
        raise ValueError("保存記録の相対パスが範囲外です")
    return path.as_posix()


def _receipt(cfg, row):
    identifier = row["paper_id"]
    if not re.fullmatch(r"[a-f0-9]{16,32}", identifier):
        raise ValueError("保存記録の文献IDが不正です")
    path = target(cfg.destination.vault_dir, f"00_System/PaperFlow/receipts/{identifier}.json")
    if not path.is_file():
        raise ValueError("Vaultの保存記録の同期待ちです：" + str(path))
    data = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(data, dict)
        or data.get("paperflow_id") != identifier
        or not isinstance(data.get("analysis_run_id"), str)
        or not re.fullmatch(r"[a-f0-9]{32}", data["analysis_run_id"])
        or not isinstance(row["generated_hash"], str)
        or not re.fullmatch(r"[a-f0-9]{64}", row["generated_hash"])
        or not isinstance(data.get("source_id"), str)
        or not re.fullmatch(r"paper:paperflow-[a-f0-9]{20}", data.get("source_id", ""))
        or not isinstance(data.get("pdf_sha256"), str)
        or not re.fullmatch(r"[a-f0-9]{64}", data.get("pdf_sha256", ""))
        or not isinstance(data.get("managed"), dict)
    ):
        raise ValueError("保存先のVaultとこのPCの保存履歴が一致しません")
    notes = [n for n in data["managed"] if n.startswith("10_Papers/20_Source_Notes/")]
    if len(notes) != 1:
        raise ValueError("保存先のSource Noteの管理記録が一致しません")
    name = _relative(cfg, notes[0])
    managed_hash = data["managed"][notes[0]]
    if (
        data["analysis_run_id"] != row["source_analysis_run_id"]
        or managed_hash != row["generated_hash"]
    ):
        # 他PCで正式版が切り替わっていても、場所の同一性と版の採用は別に扱う。
        # 旧ファイル名の出典IDと、現在の管理ハッシュ/Propertiesの一致をすべて確認する。
        slug = re.search(r"(paperflow-[a-f0-9]{20})\.md$", row["target_path"])
        if (
            slug is None
            or data["source_id"] != "paper:" + slug[1]
            or slug[1] != "paperflow-" + data["pdf_sha256"][:20]
        ):
            raise ValueError("保存先の凍結出典と元の保存記録が一致しません")
        note = target(cfg.destination.vault_dir, name)
        if not note.is_file() or sha(note) != managed_hash:
            raise ValueError("保存先のSource Noteの管理ハッシュが一致しません：" + str(note))
        parts = note.read_text(encoding="utf-8-sig").split("---", 2)
        try:
            fm = yaml.safe_load(parts[1]) if len(parts) == 3 and not parts[0].strip() else None
        except yaml.YAMLError as exc:
            raise ValueError("保存先のSource NoteのPropertiesが不正です：" + str(note)) from exc
        if not isinstance(fm, dict) or any(
            fm.get(key) != data[value]
            for key, value in (
                ("paperflow_id", "paperflow_id"),
                ("source_id", "source_id"),
                ("source_hash", "pdf_sha256"),
                ("analysis_run_id", "analysis_run_id"),
            )
        ):
            raise ValueError("保存先のSource Noteと保存記録が一致しません")
    return data, name


def export_relative(cfg, row, *, prove=True):
    """新旧の保存記録を現在のVault内の相対パスに解決する。旧絶対パスは推測しない。"""
    if not isinstance(row.get("target_path"), str) or not row["target_path"]:
        raise ValueError("保存記録に有効なパスがありません")
    value = drive_path(row["target_path"])
    parts = PureWindowsPath(value).parts if PureWindowsPath(value).drive else Path(value).parts
    if any(p == ".." or p.casefold() == ".obsidian" for p in parts):
        raise ValueError("保存記録のパスが範囲外です")
    if not Path(value).is_absolute() and not PureWindowsPath(value).is_absolute():
        return _relative(cfg, value)
    try:
        return _relative(cfg, Path(value).relative_to(cfg.destination.vault_dir).as_posix())
    except ValueError:
        pass
    if not prove:
        raise ValueError("保存先の照合が必要です")
    if row["export_kind"] == "WikiSource":
        return _receipt(cfg, row)[1]
    if row["export_kind"] not in {"Papers", "Analysis", "Reports", "Inbox"} or not re.fullmatch(
        r"[a-f0-9]{16,32}", row["paper_id"]
    ):
        raise ValueError("保存先の相対パスを特定できません")
    name = f"PaperFlow/{row['export_kind']}/{row['paper_id']}.md"
    path = target(cfg.destination.vault_dir, name)
    if not path.is_file() or sha(path) != row["generated_hash"]:
        raise ValueError("保存先のファイルと管理ハッシュが一致しません：" + str(path))
    return name


def export_path(cfg, row):
    return target(cfg.destination.vault_dir, export_relative(cfg, row))


def _exports(cfg):
    if not cfg.storage.database.is_file():
        return []
    with Store(cfg.storage.database, readonly=True) as store:
        return store.rows("SELECT * FROM exports ORDER BY rowid")


def _anchor(cfg, row):
    record, _ = _receipt(cfg, row)
    return (
        "receipt:"
        + row["paper_id"]
        + ":"
        + digest(dict(source_id=record["source_id"], pdf_sha256=record["pdf_sha256"]))
    )


def vault_identity(cfg):
    """固定計画には、移動しても変わらない既存出典の識別情報を添える。"""
    rows = _exports(cfg)
    try:
        first = next((r for r in rows if r["export_kind"] == "WikiSource"), None)
        if first:
            return _anchor(cfg, first)
        marker = target(cfg.destination.vault_dir, ".llm-wiki-root")
        return "marker:" + sha(marker) if marker.is_file() else None
    except (OSError, ValueError):
        return None


def check_vault(cfg):
    """新しい場所に同じ保存記録と実ファイルが揃っているか確認する。書き込みはしない。"""
    from paperflow.sync_snapshot import export_ready

    rows = _exports(cfg)
    for row in rows:
        if row["export_kind"] == "WikiSource":
            _receipt(cfg, row)
        export_ready(cfg, row)
    return rows


def binding_matches(cfg, previous, current):
    """原本/DBは別物を混ぜず、Vaultだけの場所変更を証跡付きで許可する。"""
    if not isinstance(previous, dict):
        return False
    fields = {"vault", "database", "paperpile", "bibtex"}
    if not fields <= previous.keys() or set(previous) - fields - {"vault_identity"}:
        return False
    if any(not isinstance(previous[k], str) for k in fields - {"bibtex"}):
        return False
    identity = previous.get("vault_identity")
    if identity is not None and (
        not isinstance(identity, str)
        or not re.fullmatch(r"(?:receipt:[a-f0-9]{16,32}:|marker:)[a-f0-9]{64}", identity)
    ):
        return False
    before = {k: previous[k] for k in fields}
    after = {k: current[k] for k in fields}
    if same_binding(before, after):
        return True
    if not same_binding(
        {k: v for k, v in before.items() if k != "vault"},
        {k: v for k, v in after.items() if k != "vault"},
    ):
        return False
    try:
        rows = check_vault(cfg)
        if identity and identity.startswith("receipt:"):
            return any(
                _anchor(cfg, r) == identity for r in rows if r["export_kind"] == "WikiSource"
            )
        if identity and identity.startswith("marker:"):
            marker = target(cfg.destination.vault_dir, ".llm-wiki-root")
            if marker.is_file() and "marker:" + sha(marker) == identity:
                return True
            # 最初の登録・フレームワーク更新でmarkerが変わった場合も、保存済み出典で照合する。
        # 旧計画には識別情報がない。旧Vault配下にあった保存記録を新しい場所で照合する。
        for row in rows:
            try:
                relative_path(row["target_path"], Path(previous["vault"]))
                return True
            except ValueError:
                continue
    except (OSError, ValueError):
        return False
    return False
