"""Paperpile同期済み文献の読み取り専用一覧。外部LLM・ディスク走査を呼ばない。"""

import json
import unicodedata
from collections import Counter
from pathlib import Path

from paperflow.drive_paths import drive_path
from paperflow.store import Store, now

FILTERS = {
    "すべて": "all",
    "取り込み済み": "processed",
    "未取り込み（失敗等も含む）": "not_imported",
    "未処理": "pending",
    "要約済み・未保存": "ready",
    "解析中": "running",
    "失敗・中断": "failed",
    "要確認・PDF未検出": "attention",
    "Paperpileにない": "removed",
}
LABELS = {
    "processed": "取り込み済み",
    "pending": "未処理",
    "ready": "要約済み・未保存",
    "running": "解析中",
    "failed": "失敗",
    "attention": "要確認",
}
PDF_LABELS = {
    "pdf_matched": "対応済み",
    "metadata_only": "未検出",
    "needs_review": "要確認",
    "retry_wait": "同期確認中",
}


def normalized(value):
    return unicodedata.normalize("NFKC", str(value)).casefold()


def decode(value, default):
    try:
        return json.loads(value) if value else default
    except (ValueError, TypeError):
        return default


def status(paper, match, latest, success, imported, metadata, *, alias_matches=False):
    if imported:
        return "processed", "取り込み済み・ファイル同期待ち" if paper[
            "error_type"
        ] == "sync_vault_wait" else LABELS["processed"]
    if latest and latest["status"] == "running":
        return "running", LABELS["running"]
    if paper["error_type"] or (latest and latest["status"] in {"failed", "interrupted"}):
        return "failed", "中断" if latest and latest["status"] == "interrupted" else LABELS[
            "failed"
        ]
    if success and (paper["source_hash"] == success["source_hash"] or alias_matches):
        return "ready", LABELS["ready"]
    if success:
        return "attention", "入力更新・再確認"
    if metadata.get("no_external"):
        return "attention", "外部送信禁止"
    if match.get("status") == "needs_review":
        return "attention", "PDF対応の要確認"
    if not match.get("path"):
        return "attention", "PDF未検出"
    if paper["state"] == "retry_wait":
        return "pending", "同期確認中"
    return "pending", LABELS["pending"]


def snapshot(config, registry=None):
    with Store(config.storage.database, readonly=True) as store:
        # 一つのSQLite読み取りトランザクションで文献・解析・出力の整合を保つ。
        store.db.execute("BEGIN")
        papers = store.rows("SELECT * FROM papers ORDER BY title COLLATE NOCASE,paper_id")
        runs = store.rows(
            "SELECT run_id,paper_id,provider,model,status,source_hash,output_json,error_type,error_message,started_at,ended_at FROM analysis_runs ORDER BY started_at, rowid"
        )
        exports = store.rows("SELECT * FROM exports")
        attachments = store.rows(
            "SELECT paper_id,source_path,sha256 FROM attachments WHERE is_primary=1"
        )
        aliases = {
            (r["run_id"], r["source_hash"])
            for r in store.rows("SELECT run_id,source_hash FROM analysis_aliases")
        }
    latest, success, imported_rows, hashes = {}, {}, {}, {}
    for run in runs:
        latest[run["paper_id"]] = run
        if run["status"] == "success":
            success[run["paper_id"]] = run
    kind = "WikiSource" if config.destination.layout == "llm_wiki" else "Analysis"
    runs_by_id = {run["run_id"]: run for run in runs}
    for exported in exports:
        exported["target_path"] = drive_path(exported["target_path"])
        if exported["export_kind"] == kind and Path(exported["target_path"]).is_relative_to(
            config.destination.vault_dir
        ):
            imported_rows[exported["paper_id"]] = exported
    for attachment in attachments:
        key = (attachment["paper_id"], drive_path(attachment["source_path"]))
        hashes[key] = attachment["sha256"] or hashes.get(key)
    rows = []
    for paper in papers:
        identifier = paper["paper_id"]
        metadata = decode(paper["metadata_json"], {})
        match = decode(paper["match_json"], {})
        if match.get("path"):
            match["path"] = drive_path(match["path"])
        last, good = latest.get(identifier), success.get(identifier)
        item = dict(
            metadata, paper_id=identifier, pdf_sha256=hashes.get((identifier, match.get("path")))
        )
        exported = imported_rows.get(identifier)
        active = runs_by_id.get(exported["source_analysis_run_id"]) if exported else None
        if active and active["status"] == "success":
            good = active
        displayed = active or last
        in_vault = bool(registry and registry.contains(item))
        imported = bool(exported or in_vault)
        group, label = status(
            paper,
            match,
            last,
            good,
            imported,
            metadata,
            alias_matches=bool(good and (good["run_id"], paper["source_hash"]) in aliases),
        )
        good_data = decode(good["output_json"], {}) if good else {}
        authors = decode(paper["authors_json"], [])
        error = paper["error_message"] or (last["error_message"] if last else "") or ""
        note = (
            exported["target_path"] if exported else registry.note_for(item) if in_vault else None
        )
        row = dict(
            paper_id=identifier,
            title=paper["title"],
            year=paper["year"] or "",
            authors=authors,
            doi=paper["doi_norm"] or "",
            arxiv_id=paper["arxiv_id"] or "",
            citekey=paper["citekey"],
            abstract=paper["abstract"] or "",
            in_source=bool(paper["source_present"]),
            imported=imported,
            registration="処理履歴で確認"
            if exported
            else "Vault照合で確認"
            if in_vault
            else "未登録"
            if registry
            else "Vault未照合",
            group=group,
            status=label,
            pdf_status=PDF_LABELS.get(match.get("status"), "未検出"),
            pdf_path=match.get("path"),
            note_path=note,
            error=error,
            error_type=paper["error_type"] or (last["error_type"] if last else ""),
            provider=displayed["provider"] if displayed else "",
            model=(displayed["model"] or "") if displayed else "",
            last_attempt=(last["ended_at"] or last["started_at"]) if last else "",
            last_success=good["ended_at"] if good else "",
            last_sync=paper["bib_last_seen"] or "",
            summary=good_data.get("tldr_ja", ""),
            topics=good_data.get("tags", []),
        )
        row["search"] = normalized(
            " ".join(
                [
                    row["title"],
                    " ".join(authors),
                    row["year"],
                    row["doi"],
                    row["arxiv_id"],
                    row["citekey"],
                    " ".join(row["topics"]),
                ]
            )
        )
        rows.append(row)
    current = [r for r in rows if r["in_source"]]
    counts = dict(Counter(r["group"] for r in current))
    counts.update(
        total=len(current),
        imported=sum(r["imported"] for r in current),
        not_imported=sum(not r["imported"] for r in current),
        removed=len(rows) - len(current),
    )
    return dict(
        rows=rows,
        counts=counts,
        updated_at=now(),
        last_sync=max((r["last_sync"] for r in rows), default=""),
    )


def filter_rows(rows, query="", group="all"):
    terms = normalized(query).split()
    return [
        r
        for r in rows
        if (
            (not r["in_source"] if group == "removed" else r["in_source"])
            and (
                group in {"all", "removed"}
                or (not r["imported"] if group == "not_imported" else r["group"] == group)
            )
            and all(term in r["search"] for term in terms)
        )
    ]
