"""C:での準備、差分固定、排他とハッシュ確認付きの本番反映。"""

import hashlib
import json
import os
import re
import shutil
import socket
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import yaml

from paperflow.config import SettingsError
from paperflow.ingestion.pdf_discovery import is_redirect
from paperflow.obsidian.writer import WriteConflict
from paperflow.wiki.render import stamp

EXCLUDE = {
    ".obsidian",
    ".git",
    ".claude",
    ".agents",
    ".codex",
    "__pycache__",
    ".venv",
    "node_modules",
}
ZONES = [
    "00_System",
    "agent-skills",
    "schemas",
    "scripts",
    "10_Papers",
    "20_Web",
    "30_Agent_Research",
    "35_Remote_Captures",
    "40_My_Notes",
    "50_Wiki",
    "60_Outputs",
    "70_Review",
]
ROOT_FILES = ["AGENTS.md", "CLAUDE.md", ".llm-wiki-root"]


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def target(root, relative):
    rel = Path(relative)
    if (
        rel.is_absolute()
        or ".." in rel.parts
        or "\\" in relative
        or ":" in relative
        or not rel.parts
    ):
        raise SettingsError("Vault出力先の相対パスが不正です")
    if any(p == ".obsidian" for p in rel.parts):
        raise SettingsError(".obsidianは操作しません")
    result = root / rel
    for p in [result, *result.parents]:
        if (p.exists() or p.is_symlink()) and is_redirect(p):
            raise SettingsError("リパースポイント経由のVault操作を拒否しました")
    if not result.resolve().is_relative_to(root.resolve()):
        raise SettingsError("Vault外への操作を拒否しました")
    return result


def inventory(root):
    """管理ノート・契約・manifestだけ。凍結原本とアプリ状態は巡回しない。"""
    result = {}
    for name in ROOT_FILES:
        p = target(root, name)
        if p.is_file():
            result[name] = sha(p)
    skills = root / "agent-skills"
    if skills.is_dir():
        for p in skills.glob("*/SKILL.md"):
            for prefix in (".claude/skills", ".agents/skills"):
                name = prefix + "/" + p.relative_to(skills).as_posix()
                generated = target(root, name)
                if generated.is_file():
                    result[name] = sha(generated)
        for prefix in (".claude/skills", ".agents/skills"):
            name = prefix + "/.generated-from-agent-skills"
            if target(root, name).is_file():
                result[name] = sha(target(root, name))
    for zone in ZONES:
        start = target(root, zone)
        if not start.is_dir():
            continue
        for current, dirs, files in os.walk(start, followlinks=False):
            dirs[:] = [d for d in dirs if d not in EXCLUDE and not is_redirect(Path(current) / d)]
            relative = Path(current).relative_to(root)
            frozen = any(p in {"10_Sources", "10_Registered", "90_Archive"} for p in relative.parts)
            for name in files:
                p = Path(current) / name
                if is_redirect(p):
                    raise SettingsError("管理対象ファイルのリパースポイントを拒否しました")
                if frozen and name != "manifest.yml":
                    continue
                if p.suffix.lower() not in {".md", ".py", ".json", ".yaml", ".yml"}:
                    continue
                if "Locks" in relative.parts or "Audit" in relative.parts:
                    continue
                result[p.relative_to(root).as_posix()] = sha(p)
    return result


def json_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".paperflow-journal-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        replace_file(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def replace_file(source, destination):
    # Windowsでは別スレッドの短いread_text中も置換が拒否される。送信や解析は再試行しない。
    for attempt in range(6):
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if os.name != "nt" or attempt == 5:
                raise
            time.sleep(min(0.02 * 2**attempt, 0.2))


@contextmanager
def wiki_lock(root):
    if not target(root, ".llm-wiki-root").is_file():
        raise SettingsError("Vaultマーカーがありません")
    path = target(root, "00_System/Locks/agent-write-lock.yml")
    path.parent.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    data = dict(
        lock_type="agent-write",
        host=socket.gethostname(),
        agent="paperflow",
        started_at=stamp(),
        operation="ingest-paper",
        token=token,
    )
    try:
        with path.open("x", encoding="utf-8") as stream:
            yaml.safe_dump(data, stream)
    except FileExistsError as exc:
        raise WriteConflict("WikiのWriterロックがあります。同一ホストでも上書きしません") from exc
    try:
        yield
    finally:
        if path.is_file():
            current = yaml.safe_load(path.read_text(encoding="utf-8"))
            if isinstance(current, dict) and current.get("token") == token:
                path.unlink()


def atomic_copy(source, dest, *, replace):
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".paperflow-", dir=dest.parent)
    try:
        with os.fdopen(fd, "wb") as out, source.open("rb") as inp:
            shutil.copyfileobj(inp, out)
            out.flush()
            os.fsync(out.fileno())
        if replace:
            replace_file(temporary, dest)
        elif os.name == "nt":
            os.rename(temporary, dest)
        else:
            os.link(temporary, dest)
    finally:
        Path(temporary).unlink(missing_ok=True)


def finalize_bundle(bundle, root, before, details):
    stage = bundle / "preview"
    after = inventory(stage)
    # PDF/抽出物はinventoryの巡回対象外。新規の凍結ディレクトリだけ明示追加。
    for directory in details.get("new_source_dirs", []):
        for name in ["source.pdf", "extracted.md", "extracted.json"]:
            p = target(stage, directory + "/" + name)
            after[directory + "/" + name] = sha(p)
    for name in details.get("derived_assets", []):
        after[name] = sha(target(stage, name))
    changes = []
    originals = bundle / "originals"
    for path, digest in sorted(after.items()):
        if before.get(path) == digest:
            continue
        live = target(root, path)
        expected = sha(live) if live.is_file() else None
        if expected == digest:
            continue
        if expected is not None:
            copy = originals / path
            copy.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(live, copy)
        changes.append(dict(path=path, before=expected, after=digest))
    plan = dict(
        version=1,
        vault=str(root.resolve()),
        created_at=stamp(),
        inventory=before,
        changes=changes,
        **details,
    )
    json_write(bundle / "plan.json", plan)
    return plan


def allowed_write(relative):
    parts = Path(relative).parts
    fixed = {
        "AGENTS.md",
        ".llm-wiki-root",
        "00_System/PaperFlow-Integration.md",
        "schemas/paper-note.schema.json",
        "scripts/_vault.py",
        "scripts/check_graph_integrity.py",
        "scripts/validate_obsidian_markup.py",
        "scripts/check_changed.py",
        "scripts/extract_paper_visuals.py",
        "scripts/update_index.py",
        "scripts/paper_database.py",
        "00_System/Processing-Policy.md",
        "00_System/Templates/Paper-Source-Note.md",
        "00_System/Dictionaries/tag-taxonomy.yml",
        "00_System/Properties-Schema.md",
    }
    if relative in fixed:
        return True
    if relative.startswith("00_System/Index/"):
        return relative.rsplit("/", 1)[-1] in {
            "index.md",
            "source-index.md",
            "concept-index.md",
            "area-index.md",
            "log.md",
            "論文データベース.md",
        }
    if relative.startswith("50_Wiki/Areas/"):
        return len(parts) == 3 and parts[-1] in {
            "Academic.md",
            "Shared.md",
            "Work.md",
            "Daily.md",
            "Hobby.md",
        }
    if relative.startswith("10_Papers/10_Sources/"):
        if (
            len(parts) == 6
            and parts[3] == "figures"
            and re.fullmatch(r"pfv-[a-f0-9]{16}", parts[4])
        ):
            return parts[-1] == "visuals.json" or bool(
                re.fullmatch(r"(?:figure|table|page)-\d+-p\d+\.png", parts[-1])
            )
        if len(parts) == 6 and parts[3:5] == ("figures", "paperflow-visuals-v1"):
            return parts[-1] == "visuals.json" or bool(
                re.fullmatch(r"(?:figure|table|page)-\d+-p\d+\.png", parts[-1])
            )
        return len(parts) == 4 and parts[-1] in {
            "source.pdf",
            "manifest.yml",
            "extracted.md",
            "extracted.json",
        }
    if relative.startswith(("10_Papers/20_Source_Notes/", "70_Review/Merge_Proposals/")):
        return len(parts) == 3 and parts[-1].endswith(".md")
    if relative.startswith("00_System/PaperFlow/receipts/"):
        return len(parts) == 4 and parts[-1].endswith(".json")
    if relative in {"60_Outputs/PaperFlow/読書候補.md", "60_Outputs/PaperFlow/要確認.md"}:
        return True
    for prefix in ("agent-skills", ".claude/skills", ".agents/skills"):
        if relative in {
            f"{prefix}/{name}/SKILL.md"
            for name in (
                "wiki-ingest-paper",
                "wiki-ingest-web",
                "wiki-triage-inbox",
                "wiki-research",
                "wiki-integrate-user-note",
                "wiki-promote",
                "wiki-lint",
                "wiki-synthesize",
                "wiki-normalize-note",
            )
        }:
            return True
    return False


def restore(root, bundle, journal):
    for entry in reversed(journal["touched"]):
        dest = target(root, entry["path"])
        actual = sha(dest) if dest.is_file() else None
        if actual == entry["before"]:
            continue
        if actual != entry["after"]:
            raise WriteConflict(
                "復旧対象が他のWriterに変更されています。自動上書きせずjournalを確認してください"
            )
        if entry["before"] is None:
            dest.unlink()
        else:
            atomic_copy(bundle / "originals" / entry["path"], dest, replace=True)
    journal["status"] = "rolled-back"
    json_write(bundle / "journal.json", journal)


def allowed_promotion_write(relative):
    parts = Path(relative).parts
    if relative.startswith("50_Wiki/") and len(parts) == 3:
        if parts[1] == "Areas":
            return allowed_write(relative)
        return parts[1] in {
            "Concepts",
            "Claims",
            "Notes",
            "Guides",
            "Methods",
            "Syntheses",
        } and parts[-1].endswith(".md")
    return allowed_write(relative) and relative.startswith(
        (
            "10_Papers/20_Source_Notes/",
            "70_Review/Merge_Proposals/",
            "00_System/PaperFlow/receipts/",
            "00_System/Index/",
        )
    )


def apply(config, bundle, *, approved_promotion_sha256=None):
    if not config.destination.allow_vault_write:
        raise SettingsError("初回本番書き込みは未承認です。差分を確認してから許可してください")
    bundle = bundle.resolve()
    plan = json.loads((bundle / "plan.json").read_text(encoding="utf-8"))
    if plan.get("version") != 1:
        raise SettingsError("未対応の反映計画です")
    operation = plan.get("operation", "ingest")
    if operation not in {"ingest", "promote"}:
        raise SettingsError("未対応の反映操作です")
    permitted = allowed_write
    if operation == "promote":
        if approved_promotion_sha256 is None or approved_promotion_sha256 != sha(
            bundle / "plan.json"
        ):
            raise SettingsError(
                "Wiki統合は具体的差分の承認後、確認済みplanのSHA-256を指定してください"
            )
        permitted = allowed_promotion_write
    root = config.destination.vault_dir.resolve()
    if str(root) != plan["vault"]:
        raise SettingsError("この差分は別のVault用です")
    if any(not permitted(e["path"]) for e in plan["changes"]):
        raise SettingsError("許可範囲外のファイルが反映計画に含まれています")
    if bundle.is_relative_to(root):
        raise SettingsError("反映用bundleはVaultの外に置いてください")
    journal_path = bundle / "journal.json"
    journal = (
        json.loads(journal_path.read_text(encoding="utf-8")) if journal_path.exists() else None
    )
    with wiki_lock(root):
        if journal and journal["status"] == "applying":
            restore(root, bundle, journal)
        for name, digest in plan.get("frozen_guards", {}).items():
            if not target(root, name).is_file() or sha(target(root, name)) != digest:
                raise WriteConflict("再利用する凍結出典が準備後に変更されました")
        if journal and journal["status"] == "complete":
            if all(
                target(root, e["path"]).is_file() and sha(target(root, e["path"])) == e["after"]
                for e in plan["changes"]
            ):
                return dict(written=0, unchanged=len(plan["changes"]))
            raise WriteConflict("反映後のファイルが変わっています。新たに差分を準備してください")
        if inventory(root) != plan["inventory"]:
            raise WriteConflict(
                "準備後にVaultの管理ノート・契約・manifestが変わりました。差分を再生成してください"
            )
        for entry in plan["changes"]:
            dest = target(root, entry["path"])
            payload = target(bundle / "preview", entry["path"])
            if not payload.is_file() or sha(payload) != entry["after"]:
                raise WriteConflict("反映用ファイルが準備時のハッシュと一致しません")
            actual = sha(dest) if dest.is_file() else None
            if actual != entry["before"]:
                raise WriteConflict("書き込み対象が差分生成後に変わりました")
            if (
                entry["before"]
                and sha(target(bundle / "originals", entry["path"])) != entry["before"]
            ):
                raise WriteConflict("復旧用バックアップが一致しません")
        journal = dict(status="applying", touched=[])
        json_write(journal_path, journal)
        try:
            for entry in plan["changes"]:
                dest = target(root, entry["path"])
                if (sha(dest) if dest.is_file() else None) != entry["before"]:
                    raise WriteConflict("書き込み直前に対象が変わりました")
                journal["touched"].append(entry)
                json_write(journal_path, journal)
                atomic_copy(
                    bundle / "preview" / entry["path"], dest, replace=entry["before"] is not None
                )
            journal["status"] = "complete"
            json_write(journal_path, journal)
        except BaseException:
            restore(root, bundle, journal)
            raise
    return dict(written=len(plan["changes"]), unchanged=0)
