"""選択した不採用回答をローカルで再検証する。AI・Vaultは呼ばない。"""

import json

from paperflow.agents.schema import validate
from paperflow.config import SettingsError
from paperflow.drive_paths import drive_path
from paperflow.models import digest
from paperflow.obsidian.writer import local_lock
from paperflow.portable import analysis_key, compatible_profiles, profile
from paperflow.store import Store, now
from paperflow.wiki.bundle import json_write


def revalidate_run(cfg, store, paper, request, identity):
    current = digest(identity)
    identities = {current}
    if request.document:
        for compatible in compatible_profiles(profile(cfg, identity["provider"])):
            identities.add(
                digest(dict(identity, prompt=compatible["prompt"], schema=compatible["schema"]))
            )
    runs = store.rows(
        "SELECT * FROM analysis_runs WHERE paper_id=? AND status='failed' "
        "ORDER BY started_at DESC,rowid DESC",
        (paper["paper_id"],),
    )
    run = next((r for r in runs if r["source_hash"] in identities), None)
    if run is None or run["error_type"] not in {"schema", "visual_reading"}:
        raise SettingsError(
            "現在のPDF・書誌・モデル・解析設定と一致する保存済み不採用回答がありません"
        )
    diagnostic = cfg.storage.data_dir / "diagnostics" / (run["run_id"] + ".rejected.json")
    if not diagnostic.is_file() or diagnostic.stat().st_size > cfg.analysis.max_output_bytes * 2:
        raise SettingsError("再検証できる診断JSONがありません、または容量上限を超えています")
    record = json.loads(diagnostic.read_text(encoding="utf-8"))
    if (
        not isinstance(record, dict)
        or record.get("type") != "paperflow-rejected-response"
        or record.get("accepted") is not False
        or record.get("run_id") != run["run_id"]
        or record.get("category") != run["error_type"]
        or "response" not in record
    ):
        raise SettingsError("診断JSONの来歴が解析記録と一致しません")
    receipt = record.get("input_receipt") or {}
    if request.document and (
        not isinstance(receipt, dict)
        or receipt.get("pdf_sha256") != request.document.sha256
        or receipt.get("page_count") != request.document.page_count
        or receipt.get("provider") != run["provider"]
        or receipt.get("model") != run["model"]
        or (run["provider"] == "agy" and receipt.get("pdf_read_completed") is not True)
    ):
        raise SettingsError("保存済み回答に対応するPDF入力・読み取り完了を照合できません")
    if cfg.destination.layout == "llm_wiki":
        from paperflow.batches import Registry

        metadata = json.loads(paper["metadata_json"])
        if Registry(cfg).contains(
            dict(metadata, paper_id=paper["paper_id"], pdf_sha256=identity["pdf"])
        ):
            raise SettingsError(
                "Vaultに登録済みの論文は再検証で切り替えません。要約の版管理を使ってください"
            )
    data = validate(record["response"], request)
    output = cfg.storage.data_dir / "analysis" / (run["run_id"] + ".json")
    if output.exists() or run["output_json"]:
        raise SettingsError("既存の正式解析データは再検証で上書きしません")
    key, portable = analysis_key(
        cfg, json.loads(paper["metadata_json"]), identity["pdf"], request, run["provider"]
    )
    # 元の不採用回答とエラーは残し、同じCLI実行の使用量を二重計上しない。
    json_write(
        diagnostic.with_name(run["run_id"] + ".revalidated.json"),
        dict(
            type="paperflow-local-revalidation",
            run_id=run["run_id"],
            at=now(),
            original_error=run["error_message"],
            original_category=run["error_type"],
            original_source_hash=run["source_hash"],
            source_hash=current,
            external_calls=0,
            response_sha256=digest(record["response"]),
            normalized_sha256=digest(data),
        ),
    )
    json_write(output, data)
    try:
        with store.transaction():
            store.db.execute(
                "UPDATE analysis_runs SET status='success',output_json_path=?,output_json=?,"
                "error_type=NULL,error_message=NULL,retryable=0 WHERE run_id=? AND status='failed'",
                (str(output), json.dumps(data, ensure_ascii=False), run["run_id"]),
            )
            store.db.execute(
                "INSERT OR IGNORE INTO analysis_aliases VALUES (?,?)", (run["run_id"], current)
            )
            store.db.execute(
                "INSERT OR REPLACE INTO portable_analyses VALUES (?,?,?)",
                (run["run_id"], key, json.dumps(portable, ensure_ascii=False)),
            )
            store.update(
                paper["paper_id"],
                state="analyzed",
                source_hash=current,
                error_type=None,
                error_message=None,
                next_retry_at=None,
            )
    except BaseException:
        output.unlink(missing_ok=True)
        raise
    return dict(
        paper_id=paper["paper_id"], run_id=run["run_id"], status="analyzed", external_calls=0
    )


def revalidate_selected(cfg, paper_ids):
    from paperflow.pipeline import Pipeline

    ids = list(dict.fromkeys(paper_ids))
    if not ids:
        raise SettingsError("再検証する論文を選んでください")
    results = []
    with local_lock(cfg.storage.data_dir), Store(cfg.storage.database) as store:
        for paper_id in ids:
            paper = store.paper(paper_id)
            if not paper or not paper["source_present"]:
                raise SettingsError("現在のPaperpileにある論文だけを再検証できます")
            if store.rows("SELECT 1 FROM exports WHERE paper_id=?", (paper_id,)):
                raise SettingsError(
                    "取り込み済み論文は再検証で切り替えません。要約の版管理を使ってください"
                )
            match = json.loads(paper["match_json"] or "{}")
            if not match.get("path") or match.get("status") != "pdf_matched":
                raise SettingsError("確定したPDF対応が必要です")
            match["path"] = drive_path(match["path"])
            results.append(
                Pipeline(cfg)._process(
                    store,
                    paper,
                    json.loads(paper["metadata_json"]),
                    match,
                    cfg.analysis.default_provider,
                    False,
                    False,
                    revalidate_only=True,
                )
            )
    return results
