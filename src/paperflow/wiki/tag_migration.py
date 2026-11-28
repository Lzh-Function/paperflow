"""既存のPaperFlow生成ノートの主題タグを、本文を保持して追補する。"""

import hashlib
import json
import re
import shutil
import sqlite3

from paperflow.config import SettingsError
from paperflow.paths import resolve_path
from paperflow.subject_tags import patch_note, register_topics
from paperflow.wiki.bundle import apply, finalize_bundle, inventory, json_write, target
from paperflow.wiki.export import run_script, validate_changed

POLICY = """
## PaperFlowの主題タグ

PaperFlowの論文要約では、管理主題語を `topics` に保持し、同じ主題を `tags` の
`topic/<slug>` にも記録する。例: `topics: [graph-neural-network]` と
`tags: [note/paper, area/academic, source/paper, topic/graph-neural-network]`。
Obsidianのタグ一覧では `topic` の下で論文を絞り込める。

`topic/*` は `Dictionaries/tag-taxonomy.yml` の登録値だけを使う。取り込み時に
PaperFlowの管理taxonomyを辞書へ登録し、LLMが提案した未登録語は自動採用しない。
`topics` はBases・検索・関連候補照合にも使用するため保持する。分類タグは
Wikiの意味関係や原典検証の状態を表さない。
"""


def inspect(cfg):
    result = []
    for path in sorted((cfg.destination.vault_dir / "10_Papers/20_Source_Notes").glob("*.md")):
        raw = path.read_bytes()
        updated = patch_note(raw)
        if updated != raw:
            result.append(
                dict(
                    path=path.relative_to(cfg.destination.vault_dir).as_posix(),
                    before=hashlib.sha256(raw).hexdigest(),
                    after=hashlib.sha256(updated).hexdigest(),
                )
            )
    return result


def prepare(cfg, bundle):
    root = resolve_path(cfg.destination.vault_dir)
    bundle = resolve_path(bundle)
    if (
        bundle.exists()
        or bundle.is_relative_to(root)
        or bundle.is_relative_to(cfg.source.paperpile_dir)
    ):
        raise SettingsError("タグ移行先は原本/Vault外の新規作業フォルダにしてください")
    before = inventory(root)
    stage = bundle / "preview"
    stage.mkdir(parents=True)
    for name in before:
        path = target(stage, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(target(root, name), path)
    changed = inspect(cfg)
    topics = list(cfg.taxonomy())
    for record in changed:
        path = target(stage, record["path"])
        raw = path.read_bytes()
        path.write_bytes(patch_note(raw))
        # inventoryは凍結抽出本文を巡回しない。既存リンクの照合用にだけC:へ配置する。
        for name in re.findall(
            r"10_Papers/10_Sources/[^\]\s|]+/extracted\.md", raw.decode("utf-8-sig")
        ):
            source = target(root, name)
            destination = target(stage, name)
            if source.is_file() and not destination.exists():
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
        import yaml

        fm = yaml.safe_load(raw.decode("utf-8-sig").split("---", 2)[1])
        topics.extend(fm.get("topics", []))
        paper_id = fm.get("paperflow_id")
        if not paper_id:
            continue
        receipt_path = target(stage, f"00_System/PaperFlow/receipts/{paper_id}.json")
        if receipt_path.exists():
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            # 人間編集が含まれるファイルを「生成したまま」として登録し直さない。
            if receipt.get("managed", {}).get(record["path"]) == record["before"]:
                receipt["managed"][record["path"]] = record["after"]
                json_write(receipt_path, receipt)
    register_topics(stage, list(dict.fromkeys(topics)))
    properties = stage / "00_System/Properties-Schema.md"
    if properties.exists() and "## PaperFlowの主題タグ" not in properties.read_text(
        encoding="utf-8-sig"
    ):
        with properties.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write("\n" + POLICY)
    if changed:
        run_script(
            stage,
            "append_log.py",
            [
                "--operation",
                "schema",
                "--subject",
                "paperflow-subject-tags-v1",
                "--detail",
                f"PaperFlow生成Source Note {len(changed)}件のtopicsをtopic/*タグへ併記。本文・原本・根拠状態は保持。タグ辞書へ管理主題語を登録。",
            ],
        )
    reports = (
        validate_changed(stage, [r["path"] for r in changed], attachment_root=root)
        if changed
        else {}
    )
    plan = finalize_bundle(
        bundle,
        root,
        before,
        dict(
            operation="ingest",
            migration="paperflow-subject-tags-v1",
            notes=changed,
            validation=reports,
        ),
    )
    return plan


def apply_migration(cfg, bundle):
    plan = json.loads((bundle / "plan.json").read_text(encoding="utf-8"))
    if plan.get("migration") != "paperflow-subject-tags-v1":
        raise SettingsError("この差分は主題タグ移行用ではありません")
    for entry in plan["notes"]:
        original = target(bundle / "originals", entry["path"]).read_bytes()
        prepared = target(bundle / "preview", entry["path"]).read_bytes()
        if patch_note(original) != prepared:
            raise SettingsError("タグ以外のノート変更が含まれるため停止しました")
    result = apply(cfg, bundle)
    if cfg.storage.database.is_file():
        # 既存DBのスキーマ/解析結果/処理状態は変更せず、管理ハッシュだけ追従。
        backup = bundle / "database-before.db"
        with sqlite3.connect(cfg.storage.database) as db:
            if not backup.exists():
                with sqlite3.connect(backup) as saved:
                    db.backup(saved)
            for entry in plan["notes"]:
                path = str(target(cfg.destination.vault_dir, entry["path"]))
                db.execute(
                    "UPDATE exports SET generated_hash=? WHERE target_path=? AND generated_hash=?",
                    (entry["after"], path, entry["before"]),
                )
                db.execute(
                    "UPDATE managed_files SET generated_hash=? WHERE path=? AND generated_hash=?",
                    (entry["after"], entry["path"], entry["before"]),
                )
    return dict(result, notes=len(plan["notes"]))
