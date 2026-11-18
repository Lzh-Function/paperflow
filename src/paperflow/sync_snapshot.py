"""同期用の論理スナップショット。PDF・認証・端末固有パスは含めない。"""

import copy
import gzip
import hashlib
import io
import json
import re
import shutil
import sqlite3
import uuid
from pathlib import Path, PurePosixPath

from paperflow.models import digest
from paperflow.portable import bib_key, profile
from paperflow.store import Store
from paperflow.wiki.bundle import json_write, sha, target

TABLES = (
    "papers",
    "identities",
    "attachments",
    "analysis_runs",
    "exports",
    "managed_files",
    "analysis_usage",
    "portable_analyses",
    "analysis_aliases",
)
MAX_PACKED = 32 * 1024 * 1024
MAX_RAW = 128 * 1024 * 1024


def relative(path, root):
    p = Path(path).resolve()
    if not p.is_relative_to(root.resolve()):
        raise ValueError("共有データに原本/Vault外のパスがあります")
    return p.relative_to(root.resolve()).as_posix()


def local(root, name):
    if not isinstance(name, str) or not name or "\\" in name or ":" in name:
        raise ValueError("共有パスが不正です")
    rel = PurePosixPath(name)
    if (
        rel.is_absolute()
        or ".." in rel.parts
        or any(p.casefold() == ".obsidian" for p in rel.parts)
    ):
        raise ValueError("共有パスが範囲外です")
    result = root.joinpath(*rel.parts).resolve()
    if not result.is_relative_to(root.resolve()) or result == root.resolve():
        raise ValueError("共有パスが範囲外です")
    return result


def capture(cfg):
    with Store(cfg.storage.database, snapshot=True) as store:
        tables = {name: store.rows(f"SELECT * FROM {name} ORDER BY rowid") for name in TABLES}
    original_hashes = {r["paper_id"]: r["bib_hash"] for r in tables["papers"]}
    for row in tables["papers"]:
        meta = json.loads(row["metadata_json"])
        meta["file"] = ""
        row["metadata_json"] = json.dumps(meta, ensure_ascii=False, sort_keys=True)
        row["bib_hash"] = digest(meta)
        if row["match_json"]:
            match = json.loads(row["match_json"])
            if match.get("path"):
                match["path"] = relative(match["path"], cfg.source.paperpile_dir)
            # 観測値と候補リストは各PCで再取得する。
            match.pop("mtime_ns", None)
            match["candidates"] = []
            row["match_json"] = json.dumps(match, ensure_ascii=False, sort_keys=True)
        row["next_retry_at"] = None
    for row in tables["attachments"]:
        row["source_path"] = relative(row["source_path"], cfg.source.paperpile_dir)
        row["mtime_ns"] = None
    for row in tables["analysis_runs"]:
        row["output_json_path"] = None
    for row in tables["exports"]:
        row["target_path"] = relative(row["target_path"], cfg.destination.vault_dir)
    for row in tables["managed_files"]:
        local(cfg.destination.vault_dir, row["path"])
    # エラーに含まれるパスも端末から持ち出さない。
    for rows in tables.values():
        for row in rows:
            for key in ("error_message",):
                if row.get(key):
                    row[key] = re.sub(r"[A-Za-z]:[\\/][^\s\"<>]+", "（端末内パス）", row[key])
    plans = []
    paper_meta = {r["paper_id"]: json.loads(r["metadata_json"]) for r in tables["papers"]}
    for path in sorted((cfg.storage.data_dir / "batches").glob("*.plan.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("binding") != __import__("paperflow.batches", fromlist=["binding"]).binding(
            cfg
        ):
            continue
        data.pop("binding")
        for item in data["items"]:
            item["pdf"] = relative(item["pdf"], cfg.source.paperpile_dir)
            if "portable_bib" not in item and item["bib_hash"] == original_hashes.get(
                item["paper_id"]
            ):
                item["portable_bib"] = bib_key(paper_meta[item["paper_id"]])
        progress_path = path.with_name(path.stem + ".progress.json")
        progress = (
            json.loads(progress_path.read_text(encoding="utf-8"))
            if progress_path.exists()
            else None
        )
        if progress:
            progress.pop("plan_sha256", None)
            if not data.get("portable_runtime"):
                from paperflow.batches import runtime_key

                previous = cfg.model_copy(deep=True)
                previous.analysis.default_provider = progress["provider"]
                previous.analysis.model = progress["model"]
                if progress.get("runtime_key") == runtime_key(previous):
                    previous_profile = profile(previous)
                    data["portable_runtime"] = True
                    progress["runtime_key"] = digest(previous_profile)
                    # 設定と固定入力の両方が証明できる旧成功結果だけを移行。
                    fixed = {p["paper_id"]: p for p in data["items"] if "portable_bib" in p}
                    known = {p["run_id"] for p in tables["portable_analyses"]}
                    baseline = set(progress.get("baseline_runs", []))
                    for run in tables["analysis_runs"]:
                        paper = next(
                            (p for p in tables["papers"] if p["paper_id"] == run["paper_id"]), None
                        )
                        item = fixed.get(run["paper_id"])
                        if (
                            run["status"] == "success"
                            and run["run_id"] not in known | baseline
                            and item
                            and paper
                            and run["source_hash"] == paper["source_hash"]
                            and run["parser_version"] == previous_profile["parser"]
                            and any(
                                a["paper_id"] == run["paper_id"]
                                and a["sha256"] == item["pdf_sha256"]
                                for a in tables["attachments"]
                            )
                        ):
                            legacy = dict(
                                legacy_fixed=dict(
                                    bib=item["portable_bib"],
                                    pdf=item["pdf_sha256"],
                                    profile=previous_profile,
                                    coverage=run["coverage"],
                                    schema=run["schema_version"],
                                )
                            )
                            tables["portable_analyses"].append(
                                dict(
                                    run_id=run["run_id"],
                                    cache_key=digest(legacy),
                                    input_json=json.dumps(legacy, ensure_ascii=False),
                                )
                            )
                            known.add(run["run_id"])
        plans.append(dict(plan=data, progress=progress))
    return dict(version=1, profile=profile(cfg), tables=tables, batches=plans)


def packed(data):
    raw = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    if len(raw) > MAX_RAW:
        raise ValueError("共有進捗が128MiBを超えています")
    result = gzip.compress(raw, mtime=0)
    if len(result) > MAX_PACKED:
        raise ValueError("圧縮した共有進捗が32MiBを超えています")
    return result


def unpack(blob, checksum):
    if len(blob) > MAX_PACKED or hashlib.sha256(blob).hexdigest() != checksum:
        raise ValueError("共有進捗のサイズ/ハッシュが一致しません")
    with gzip.GzipFile(fileobj=io.BytesIO(blob)) as stream:
        raw = stream.read(MAX_RAW + 1)
    if len(raw) > MAX_RAW:
        raise ValueError("展開した共有進捗が大きすぎます")
    data = json.loads(raw)
    if (
        not isinstance(data, dict)
        or set(data) != {"version", "profile", "tables", "batches"}
        or data["version"] != 1
    ):
        raise ValueError("未対応の共有進捗です")
    if not isinstance(data["tables"], dict) or set(data["tables"]) != set(TABLES):
        raise ValueError("共有DBの形式が不正です")
    with Store(Path("unused"), snapshot=True) as schema:
        for name in TABLES:
            columns = {r[1] for r in schema.db.execute(f"PRAGMA table_info({name})")}
            if not isinstance(data["tables"][name], list) or any(
                not isinstance(r, dict) or set(r) != columns for r in data["tables"][name]
            ):
                raise ValueError("共有DBの列が不正です")
    for row in data["tables"]["analysis_runs"]:
        if not re.fullmatch(r"[a-f0-9]{32}", row["run_id"]):
            raise ValueError("解析IDが不正です")
        if row["status"] == "success":
            import jsonschema

            from paperflow.agents.schema import SCHEMA, VISUAL_SCHEMA

            output = json.loads(row["output_json"])
            jsonschema.validate(output, VISUAL_SCHEMA if "visual_review" in output else SCHEMA)
    return data


def materialize(cfg, data):
    """本番反映前にメモリDBとパスを検証し、ローカル用のデータへ変換。"""
    data = copy.deepcopy(data)
    tables = data["tables"]
    for row in tables["papers"]:
        if row["match_json"]:
            match = json.loads(row["match_json"])
            if match.get("path"):
                p = local(cfg.source.paperpile_dir, match["path"])
                match["path"] = str(p)
                match["mtime_ns"] = p.stat().st_mtime_ns if p.is_file() else -1
            row["match_json"] = json.dumps(match, ensure_ascii=False, sort_keys=True)
        if any(
            r["paper_id"] == row["paper_id"] and r["status"] == "running"
            for r in tables["analysis_runs"]
        ):
            row.update(
                state="blocked",
                error_type="sync_result_unknown",
                error_message="前PCの解析結果が未確認です。再送前に確認してください",
            )
        elif row["state"] != "exported" and any(
            r["paper_id"] == row["paper_id"]
            and r["status"] == "success"
            and r
            is next(
                (
                    candidate
                    for candidate in reversed(tables["analysis_runs"])
                    if candidate["paper_id"] == row["paper_id"] and candidate["status"] == "success"
                ),
                None,
            )
            and not any(p["run_id"] == r["run_id"] for p in tables["portable_analyses"])
            for r in tables["analysis_runs"]
        ):
            row.update(
                state="blocked",
                error_type="sync_legacy_review",
                error_message="旧版の保存済み解析があります。解析設定の一致を確認できないため自動再送を停止しています",
            )
    for row in tables["attachments"]:
        p = local(cfg.source.paperpile_dir, row["source_path"])
        row["source_path"] = str(p)
        row["mtime_ns"] = p.stat().st_mtime_ns if p.is_file() else -1
    for row in tables["analysis_runs"]:
        if row["status"] == "running":
            row.update(
                status="interrupted",
                error_type="sync_result_unknown",
                error_message="前PCで回答の保存を確認できませんでした",
            )
        row["output_json_path"] = (
            str(cfg.storage.data_dir / "analysis" / (row["run_id"] + ".json"))
            if row["output_json"]
            else None
        )
    for row in tables["exports"]:
        row["target_path"] = str(local(cfg.destination.vault_dir, row["target_path"]))
        try:
            export_ready(cfg, row)
        except ValueError as exc:
            for paper in tables["papers"]:
                if paper["paper_id"] == row["paper_id"] and not paper["error_type"]:
                    paper.update(error_type="sync_vault_wait", error_message=str(exc))
    for row in tables["managed_files"]:
        local(cfg.destination.vault_dir, row["path"])
    with Store(Path("unused"), snapshot=True) as test:
        insert(test.db, tables)
        if test.rows("PRAGMA foreign_key_check"):
            raise ValueError("共有DBの参照が不正です")
    batch_files = {}
    existing_paths = {}
    for path in (cfg.storage.data_dir / "batches").glob("*.plan.json"):
        old = json.loads(path.read_text(encoding="utf-8"))
        existing_paths[old.get("batch_id")] = path
    from paperflow.batches import binding

    for entry in data["batches"]:
        plan = entry["plan"]
        if not re.fullmatch(r"[a-f0-9]{32}", plan["batch_id"]):
            raise ValueError("バッチIDが不正です")
        plan["binding"] = binding(cfg)
        for item in plan["items"]:
            item["pdf"] = str(local(cfg.source.paperpile_dir, item["pdf"]))
        name = existing_paths.get(plan["batch_id"]) or cfg.storage.data_dir / "batches" / (
            plan["batch_id"] + ".plan.json"
        )
        if name in batch_files:
            raise ValueError("共有バッチIDが重複しています")
        batch_files[name] = plan
        if entry["progress"]:
            progress = entry["progress"]
            # ハッシュは書き出す実際のバイト列に対して後で計算。
            batch_files[name.with_name(name.stem + ".progress.json")] = progress
    return tables, batch_files


def insert(db, tables):
    for name in TABLES:
        for row in tables[name]:
            columns = list(row)
            db.execute(
                f"INSERT INTO {name} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                tuple(row[c] for c in columns),
            )


def backup_local(cfg):
    backup = cfg.storage.data_dir / "progress-sync" / "backups" / uuid.uuid4().hex
    backup.mkdir(parents=True)
    if cfg.storage.database.exists():
        with (
            sqlite3.connect(
                cfg.storage.database.resolve().as_uri() + "?mode=ro", uri=True
            ) as source,
            sqlite3.connect(backup / "state.db") as saved,
        ):
            source.backup(saved)
    batch_dir = cfg.storage.data_dir / "batches"
    if batch_dir.exists():
        shutil.copytree(batch_dir, backup / "batches")
    return backup


def rollback_local(cfg, backup):
    if not backup.resolve().is_relative_to(
        (cfg.storage.data_dir / "progress-sync/backups").resolve()
    ):
        raise ValueError("同期バックアップの範囲が不正です")
    if (backup / "state.db").exists():
        with (
            sqlite3.connect(backup / "state.db") as saved,
            sqlite3.connect(cfg.storage.database) as db,
        ):
            saved.backup(db)
    else:
        cfg.storage.database.unlink(missing_ok=True)
    batch_dir = cfg.storage.data_dir / "batches"
    if batch_dir.exists():
        shutil.rmtree(batch_dir)
    if (backup / "batches").exists():
        shutil.copytree(backup / "batches", batch_dir)


def restore(cfg, data, *, commit_id=None):
    tables, files = materialize(cfg, data)
    backup = backup_local(cfg)
    journal = cfg.storage.data_dir / "progress-sync/receiving.json"
    if commit_id:
        json_write(journal, dict(backup=backup.name, commit_id=commit_id))
    try:
        with Store(cfg.storage.database) as store, store.transaction():
            for name in reversed(TABLES):
                store.db.execute(f"DELETE FROM {name}")
            store.db.execute("DELETE FROM observations")
            insert(store.db, tables)
        for path, value in files.items():
            if path.name.endswith(".plan.json"):
                path.parent.mkdir(parents=True, exist_ok=True)
                json_write(path, value)
        for path, value in files.items():
            if path.name.endswith(".progress.json"):
                plan = path.with_name(path.name.replace(".plan.progress.json", ".plan.json"))
                value["plan_sha256"] = sha(plan)
                json_write(path, value)
        for row in tables["analysis_runs"]:
            if row["output_json_path"]:
                path = Path(row["output_json_path"])
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(row["output_json"], encoding="utf-8")
    except BaseException:
        rollback_local(cfg, backup)
        journal.unlink(missing_ok=True)
        raise
    return str(backup)


def vault_ready(cfg):
    """共有の保存記録だけ先着した場合は、再解析せずファイル到着を待つ。"""
    with Store(cfg.storage.database, readonly=True) as store:
        exports = store.rows("SELECT * FROM exports")
    for row in exports:
        export_ready(cfg, row)


def export_ready(cfg, row):
    path = Path(row["target_path"])
    if not path.is_file():
        raise ValueError("Vaultのファイル同期待ちです：" + path.name)
    if row["export_kind"] == "WikiSource":
        receipt = target(
            cfg.destination.vault_dir, f"00_System/PaperFlow/receipts/{row['paper_id']}.json"
        )
        if not receipt.is_file():
            raise ValueError("Vaultの保存記録の同期待ちです")
        data = json.loads(receipt.read_text(encoding="utf-8"))
        if data.get("paperflow_id") != row["paper_id"]:
            raise ValueError("Vaultの保存記録が一致しません")
        source_id = data.get("source_id", "")
        if not source_id.startswith("paper:"):
            raise ValueError("Vaultの保存記録に出典IDがありません")
        source_dir = "10_Papers/10_Sources/" + source_id.split(":", 1)[1]
        for name in ("source.pdf", "manifest.yml", "extracted.md", "extracted.json"):
            if not target(cfg.destination.vault_dir, source_dir + "/" + name).is_file():
                raise ValueError("Vaultの凍結出典の同期待ちです")
        # 原本PDFは全件再ハッシュせず、凍結ファイルの到着を確認する。
        for name in data.get("managed", {}):
            if not target(cfg.destination.vault_dir, name).is_file():
                raise ValueError("Vaultの図表/出典の同期待ちです：" + name)
