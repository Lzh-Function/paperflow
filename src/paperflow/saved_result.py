"""PaperFlow試験フォルダの成功結果を、明示選択で取り込む。外部通信はしない。"""

import json
import os
from datetime import datetime
from hashlib import sha256
from pathlib import Path

import pymupdf

from paperflow.agents.base import AnalysisRequest, PDFDocument
from paperflow.agents.schema import parse_response, validate
from paperflow.agents.usage import number
from paperflow.config import SettingsError
from paperflow.models import digest
from paperflow.paths import resolve_path
from paperflow.portable import bib_key
from paperflow.wiki.bundle import json_write

FILES = ("manifest.json", "analysis.json", "result.json", "input-audit.json", "verification.json")


def read(cfg, folder, hashes=None):
    folder = resolve_path(Path(folder))
    if os.name == "nt" and folder.drive.upper() != "C:":
        raise SettingsError("試験結果はC:内のフォルダを選んでください")
    if folder.is_relative_to(resolve_path(cfg.destination.vault_dir)) or folder.is_relative_to(
        resolve_path(cfg.source.paperpile_dir)
    ):
        raise SettingsError("原本・Vault内の結果は直接取り込みません")
    records, checksums = {}, {}
    for name in FILES:
        path = folder / name
        if (
            not path.is_file()
            or resolve_path(path).parent != folder
            or path.stat().st_size > cfg.analysis.max_output_bytes * 2
        ):
            raise SettingsError(f"有効なPaperFlow試験結果がありません：{name}")
        with path.open("rb") as stream:
            raw = stream.read(cfg.analysis.max_output_bytes * 2 + 1)
        if len(raw) > cfg.analysis.max_output_bytes * 2:
            raise SettingsError("試験結果の容量上限を超えています")
        checksums[name] = sha256(raw).hexdigest()
        records[name] = parse_response(raw.decode("utf-8"))
        if not isinstance(records[name], dict):
            raise SettingsError(f"試験結果のJSON形式が不正です：{name}")
    if hashes is not None and checksums != hashes:
        raise SettingsError("選択後に試験結果が変更されています。選び直してください")
    manifest, result, audit, verification = (
        records[n]
        for n in ("manifest.json", "result.json", "input-audit.json", "verification.json")
    )
    if (
        manifest.get("provider") not in {"claude", "codex", "agy"}
        or not isinstance(manifest.get("model"), str)
        or not manifest["model"]
        or result.get("status") != "success"
        or verification.get("status") != "success"
        or result.get("model") != manifest["model"]
        or audit.get("model") != manifest["model"]
        or audit.get("source_pdf_sha256") != manifest.get("pdf_sha256")
        or verification.get("source_unchanged") is not True
        or verification.get("original_pdf_pages") != manifest.get("pages")
    ):
        raise SettingsError("成功・モデル・元PDFの来歴を確認できない試験結果です")
    return records, checksums


def prepare_import(cfg, folder):
    from paperflow.recovery import prepare_job

    records, hashes = read(cfg, folder)
    manifest = records["manifest.json"]
    job = prepare_job(
        cfg, [manifest["paper_id"]], "save", provider=manifest["provider"], model=manifest["model"]
    )
    item = job["items"][0]
    item.pop("artifact", None)
    if item["title"] != manifest.get("title") or item["pdf_sha256"] != manifest.get("pdf_sha256"):
        raise SettingsError("試験結果の書誌・PDFが現在の論文と一致しません")
    job.update(mode="import", artifact=dict(folder=str(resolve_path(Path(folder))), hashes=hashes))
    item["import_run_id"] = job["job_id"]
    return job


def register(cfg, store, paper, item, job, pdf):
    records, hashes = read(cfg, job["artifact"]["folder"], job["artifact"]["hashes"])
    manifest, result = records["manifest.json"], records["result.json"]
    with pymupdf.open(pdf) as doc:
        pages = len(doc)
    if (
        manifest["paper_id"] != paper["paper_id"]
        or manifest["pdf_sha256"] != item["pdf_sha256"]
        or manifest["pages"] != pages
    ):
        raise SettingsError("保存結果と現在のPDFが一致しません")
    request = AnalysisRequest(
        paper["paper_id"],
        {},
        "",
        records["analysis.json"]["coverage"],
        list(range(1, pages + 1)),
        cfg.taxonomy(),
        document=PDFDocument(pdf, item["pdf_sha256"], pages, cfg.destination.visual_max_items),
    )
    data = validate(records["analysis.json"], request)
    generated_at = manifest.get("generated_at")
    if generated_at:
        date = datetime.fromisoformat(generated_at)
        if date.tzinfo is None:
            raise SettingsError("試験結果の生成日時にタイムゾーンがありません")
    data["uncertainties_ja"].append(
        "PaperFlowの別保存結果取り込み：試験時の成功回答を再送せず利用した。"
        + (
            "元の生成日時を保持する。"
            if generated_at
            else "生成日時の明示記録がないため、ノートの日付は取り込み日時である。"
        )
        + "試験時のモデル・取得済み使用量を保持する。"
    )
    identity = dict(
        kind="imported-result",
        bib=bib_key(json.loads(paper["metadata_json"])),
        pdf=item["pdf_sha256"],
        files=hashes,
    )
    source_hash = digest(identity)
    run_id = item["import_run_id"]
    existing = store.rows("SELECT * FROM analysis_runs WHERE run_id=?", (run_id,))
    if existing:
        if existing[0]["source_hash"] != source_hash or existing[0]["status"] != "success":
            raise SettingsError("試験結果の取り込み記録が一致しません")
        return
    output = cfg.storage.data_dir / "analysis" / (run_id + ".json")
    if output.exists() and parse_response(output.read_text(encoding="utf-8")) != data:
        raise SettingsError("既存の解析JSONを試験結果で上書きしません")
    json_write(output, data)
    original_usage = result.get("usage") or {}
    if not isinstance(original_usage, dict):
        raise SettingsError("試験結果の使用量記録が不正です")
    usage = {
        k: number(original_usage.get(k))
        for k in (
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "total_tokens",
            "cost_usd",
        )
    }
    usage.update(source="cli_reported", imported_result=True)
    with store.transaction():
        store.db.execute(
            "UPDATE attachments SET sha256=? WHERE paper_id=? AND source_path=?",
            (item["pdf_sha256"], paper["paper_id"], json.loads(paper["match_json"])["path"]),
        )
        store.db.execute(
            """INSERT INTO analysis_runs
            (run_id,paper_id,provider,model,status,coverage,source_hash,schema_version,prompt_version,parser_version,output_json_path,output_json,started_at,ended_at,elapsed_seconds)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                run_id,
                paper["paper_id"],
                manifest["provider"],
                manifest["model"],
                "success",
                data["coverage"],
                source_hash,
                "v2-visual",
                "imported-result",
                "imported-result",
                str(output),
                json.dumps(data, ensure_ascii=False),
                generated_at or job["created_at"],
                job["created_at"],
                number(result.get("elapsed_seconds")),
            ),
        )
        store.db.execute("INSERT INTO analysis_usage VALUES (?,?)", (run_id, json.dumps(usage)))
        store.db.execute(
            "INSERT INTO portable_analyses VALUES (?,?,?)",
            (run_id, source_hash, json.dumps(identity)),
        )
        store.update(
            paper["paper_id"],
            state="analyzed",
            source_hash=source_hash,
            error_type="saved_import_pending",
            error_message="別保存の成功回答を取り込み済みです。復旧・後処理から保存してください",
            next_retry_at=None,
        )
    json_write(
        output.with_suffix(".import.json"),
        dict(
            type="paperflow-imported-result",
            source_files_sha256=hashes,
            run_id=run_id,
            external_calls=0,
        ),
    )
