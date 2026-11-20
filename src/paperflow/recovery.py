"""失敗・未保存の棚卸しと明示選択した文献の一括後処理。"""

import json
import re
import uuid
from collections import Counter
from pathlib import Path

from paperflow.agents.base import ProviderError
from paperflow.batches import Registry, binding
from paperflow.config import SettingsError
from paperflow.drive_paths import drive_path
from paperflow.library import snapshot
from paperflow.obsidian.writer import SafeWriter, WriteConflict, local_lock
from paperflow.pipeline import Pipeline
from paperflow.portable import bib_key, profile, profiles_compatible
from paperflow.store import Store, now
from paperflow.wiki.bundle import apply, json_write, sha
from paperflow.wiki.export import cached_items, prepare, record_exports

MODES = {
    "validate": "保存済み回答を再検証",
    "save": "再送せず復旧・保存",
    "reanalyze": "選択分を再解析・保存",
    "import": "別保存の成功結果を取り込み・保存",
}
LABELS = {
    "ready": "保存するだけ",
    "revalidate": "保存済み回答の再検証候補",
    "retry": "再解析が必要",
    "artifact": "別保存の成功結果候補",
    "attention": "確認が必要",
    "resolved": "解決済み",
}
COMMON_ERRORS = {
    "authentication",
    "missing_cli",
    "cli_version",
    "cli_wrapper",
    "launch",
    "fulltext_permission",
}


def overview(cfg, registry=None):
    registry = registry or Registry(cfg)
    view = snapshot(cfg, registry)
    with Store(cfg.storage.database, readonly=True) as store:
        runs = store.rows(
            "SELECT run_id,paper_id,status,provider,model,error_type,error_message,started_at FROM analysis_runs ORDER BY started_at,rowid"
        )
    histories = {}
    for run in runs:
        histories.setdefault(run["paper_id"], []).append(run)
    artifacts = saved_artifacts(cfg)
    rows = []
    for row in view["rows"]:
        history = histories.get(row["paper_id"], [])
        failed = [r for r in history if r["status"] in {"failed", "interrupted"}]
        if not failed and not row["error"] and row["group"] != "ready":
            continue
        latest = history[-1] if history else {}
        diagnostic = (
            cfg.storage.data_dir / "diagnostics" / (latest.get("run_id", "") + ".rejected.json")
        )
        if row["imported"]:
            group = "resolved"
        elif (
            not row["in_source"]
            or row["pdf_status"] != "対応済み"
            or row["group"] in {"running", "attention"}
            or row["error_type"] in {"sync_result_unknown", "sync_legacy_review", "sync_vault_wait"}
        ):
            group = "attention"
        elif row["group"] == "ready" or (
            row["error_type"] == "saved_import_pending" and latest.get("status") == "success"
        ):
            group = "ready"
        elif (
            latest.get("error_type") in {"visual_reading", "schema", "invalid_json"}
            and diagnostic.is_file()
        ):
            group = "revalidate"
        elif row["paper_id"] in artifacts:
            group = "artifact"
        else:
            group = "retry"
        if group == "retry" and row.get("error_type") == "no_external":
            group = "attention"
        rows.append(
            dict(
                row,
                recovery_group=group,
                recovery_label=LABELS[group],
                failed_runs=len(failed),
                latest_run=latest.get("run_id"),
                diagnostic=str(diagnostic) if diagnostic.is_file() else None,
                artifact_folder=artifacts.get(row["paper_id"], {}).get("folder"),
            )
        )
    bundles = Counter()
    root = cfg.storage.data_dir / "wiki-bundles"
    for directory in root.iterdir() if root.exists() else []:
        if not directory.is_dir():
            continue
        journal = directory / "journal.json"
        if journal.is_file():
            try:
                bundles[json.loads(journal.read_text(encoding="utf-8"))["status"]] += 1
            except (OSError, ValueError, KeyError, TypeError):
                bundles["unknown"] += 1
        elif (directory / "preview").is_dir():
            bundles["preview"] += 1
    return dict(
        rows=rows,
        counts=dict(Counter(r["recovery_group"] for r in rows)),
        bundles=dict(bundles),
        updated_at=view["updated_at"],
        imported=view["counts"]["imported"],
    )


def saved_artifacts(cfg):
    from paperflow.saved_result import read

    result = {}
    for manifest_path in sorted(
        (cfg.storage.data_dir / "pdf-delivery-retrial").glob("*/manifest.json")
    ):
        try:
            records, hashes = read(cfg, manifest_path.parent)
            manifest = records["manifest.json"]
            result[manifest["paper_id"]] = dict(
                folder=str(manifest_path.parent), hashes=hashes, manifest=manifest
            )
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return result


def prepare_job(cfg, paper_ids, mode, *, provider=None, model=None):
    if mode not in MODES:
        raise SettingsError("復旧の操作が不正です")
    ids = list(dict.fromkeys(paper_ids))
    if not ids:
        raise SettingsError("復旧する論文を選んでください")
    job_cfg = cfg.model_copy(deep=True)
    if provider is not None:
        job_cfg.analysis.default_provider = provider
        job_cfg.analysis.model = model
    registry = Registry(cfg)
    artifacts = saved_artifacts(cfg) if mode == "save" else {}
    with Store(cfg.storage.database, readonly=True) as store:
        items = []
        for identifier in ids:
            paper = store.paper(identifier)
            meta = json.loads(paper["metadata_json"])
            match = json.loads(paper["match_json"] or "{}")
            if (
                not paper["source_present"]
                or not match.get("path")
                or match.get("status") != "pdf_matched"
            ):
                raise SettingsError(f"PDF対応かPaperpileの存在を確認してください：{paper['title']}")
            pdf = Path(drive_path(match["path"])).resolve()
            if not pdf.is_relative_to(cfg.source.paperpile_dir.resolve()):
                raise SettingsError("Paperpileの外のPDFは処理しません")
            digest_pdf = sha(pdf)
            if registry.contains(
                dict(meta, paper_id=identifier, pdf_sha256=digest_pdf)
            ) or store.rows("SELECT 1 FROM exports WHERE paper_id=?", (identifier,)):
                raise SettingsError(f"取り込み済み論文は対象にできません：{paper['title']}")
            if paper["error_type"] in {
                "sync_result_unknown",
                "sync_legacy_review",
                "sync_vault_wait",
            } or store.rows(
                "SELECT 1 FROM analysis_runs WHERE paper_id=? AND status='running'", (identifier,)
            ):
                raise SettingsError(f"前回の実行結果を確認してください：{paper['title']}")
            if mode == "reanalyze" and meta.get("no_external"):
                raise SettingsError(f"外部送信禁止です：{paper['title']}")
            items.append(
                dict(
                    paper_id=identifier,
                    title=paper["title"],
                    bib=bib_key(meta),
                    pdf_sha256=digest_pdf,
                    status="pending",
                    message="",
                )
            )
            artifact = artifacts.get(identifier)
            if artifact and store.latest_analysis(identifier) is None:
                manifest = artifact["manifest"]
                if manifest["pdf_sha256"] != digest_pdf or manifest["title"] != paper["title"]:
                    raise SettingsError("別保存の成功結果と現在のPDF・書誌が一致しません")
                items[-1].update(
                    artifact=dict(folder=artifact["folder"], hashes=artifact["hashes"]),
                    import_run_id=uuid.uuid4().hex,
                )
    return dict(
        type="paperflow-recovery-job",
        version=1,
        job_id=uuid.uuid4().hex,
        created_at=now(),
        binding=binding(cfg),
        profile=profile(job_cfg),
        mode=mode,
        provider=job_cfg.analysis.default_provider,
        model=job_cfg.analysis.model,
        status="prepared",
        items=items,
    )


def job_path(cfg, identifier):
    if not re.fullmatch(r"[a-f0-9]{32}", identifier):
        raise SettingsError("復旧ジョブIDが不正です")
    return cfg.storage.data_dir / "recovery-jobs" / (identifier + ".json")


def history(cfg):
    jobs = []
    for path in (cfg.storage.data_dir / "recovery-jobs").glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if (
                isinstance(data, dict)
                and data.get("type") == "paperflow-recovery-job"
                and data.get("version") == 1
            ):
                jobs.append(data)
        except (OSError, ValueError):
            continue
    return sorted(jobs, key=lambda j: j["created_at"], reverse=True)


def load_job(cfg, identifier):
    data = json.loads(job_path(cfg, identifier).read_text(encoding="utf-8"))
    if (
        data.get("type") != "paperflow-recovery-job"
        or data.get("version") != 1
        or data.get("job_id") != identifier
        or data.get("mode") not in MODES
        or data.get("binding") != binding(cfg)
    ):
        raise SettingsError("このPC・設定では復旧ジョブを再開できません")
    return data


def _save(cfg, store, identifier):
    items = cached_items(cfg, [identifier])
    bundle = cfg.storage.data_dir / "wiki-bundles" / uuid.uuid4().hex[:12]
    plan = prepare(cfg, items, bundle)
    apply(cfg, bundle)
    record_exports(cfg, store, plan)


def _one(cfg, store, item, mode, *, injected_provider=None, checkpoint=None, job=None):
    paper = store.paper(item["paper_id"])
    meta = json.loads(paper["metadata_json"])
    match = json.loads(paper["match_json"] or "{}")
    pdf = Path(drive_path(match.get("path") or "")).resolve()
    if (
        not paper["source_present"]
        or bib_key(meta) != item["bib"]
        or not pdf.is_relative_to(cfg.source.paperpile_dir.resolve())
        or sha(pdf) != item["pdf_sha256"]
    ):
        raise WriteConflict("固定した書誌・PDFが変わっています。AIへは送りません")
    registry = Registry(cfg)
    if registry.contains(dict(meta, paper_id=paper["paper_id"], pdf_sha256=item["pdf_sha256"])):
        return dict(status="already_saved", message="取り込み済みのためスキップ", external_calls=0)
    if paper["error_type"] in {
        "sync_result_unknown",
        "sync_legacy_review",
        "sync_vault_wait",
    } or store.rows(
        "SELECT 1 FROM analysis_runs WHERE paper_id=? AND status='running'", (paper["paper_id"],)
    ):
        raise SettingsError("前回の実行結果を確認するまで処理できません")
    match["path"] = str(pdf)
    expected = dict(portable_bib=item["bib"], pdf=str(pdf), pdf_sha256=item["pdf_sha256"])
    work = cfg.model_copy(deep=True)
    processor = Pipeline(work, injected_provider)
    processor.checkpoint = checkpoint or (lambda: None)
    if mode == "import" or (mode == "save" and item.get("artifact")):
        from paperflow.saved_result import register

        register(
            work,
            store,
            paper,
            item,
            dict(job, artifact=item["artifact"])
            if mode != "import" and item.get("artifact")
            else job,
            pdf,
        )
        _save(work, store, paper["paper_id"])
        with store.transaction():
            store.update(paper["paper_id"], error_type=None, error_message=None)
        return dict(
            status="saved", message="別保存の成功結果を再送せず保存しました", external_calls=0
        )
    if mode == "reanalyze":
        try:
            processor._process(
                store, paper, meta, match, work.analysis.default_provider, False, False, expected
            )
        except (ProviderError, WriteConflict, OSError, ValueError) as exc:
            processor._record_error(store, paper, exc)
            raise
        calls = processor.cli_calls
    else:
        good = store.latest_analysis(paper["paper_id"])
        last = store.rows(
            "SELECT * FROM analysis_runs WHERE paper_id=? ORDER BY started_at DESC,rowid DESC LIMIT 1",
            (paper["paper_id"],),
        )
        run = good or (last[0] if last else None)
        if not run:
            return dict(
                status="needs_analysis",
                message="保存済み回答がないため再解析待ちです。AIへは送りません",
                external_calls=0,
            )
        if good is None and (
            run["error_type"] not in {"visual_reading", "schema", "invalid_json"}
            or not (
                work.storage.data_dir / "diagnostics" / (run["run_id"] + ".rejected.json")
            ).is_file()
        ):
            return dict(
                status="needs_analysis",
                message="再検証できる保存済み回答がありません。再解析を選んでください",
                external_calls=0,
            )
        work.analysis.default_provider, work.analysis.model = run["provider"], run["model"]
        imported = (
            store.rows("SELECT input_json FROM portable_analyses WHERE run_id=?", (run["run_id"],))
            if good
            else []
        )
        provenance = json.loads(imported[0]["input_json"]) if imported else {}
        saved_profile = provenance.get("profile") or provenance.get("legacy_fixed", {}).get(
            "profile"
        )
        if saved_profile and saved_profile.get("provider") == run["provider"]:
            work.analysis.model = saved_profile.get("model")
        elif run["provider"] == "fake":
            work.analysis.model = None
        if provenance.get("kind") == "imported-result":
            if provenance.get("bib") != item["bib"] or provenance.get("pdf") != item["pdf_sha256"]:
                raise SettingsError("別保存結果を取り込んだ後に入力が変更されています")
            if mode == "save":
                _save(work, store, paper["paper_id"])
                with store.transaction():
                    store.update(paper["paper_id"], error_type=None, error_message=None)
                return dict(
                    status="saved",
                    message="取り込み済みの別保存結果を保存しました",
                    external_calls=0,
                )
            return dict(
                status="ready", message="別保存結果は取り込み済み・未保存です", external_calls=0
            )
        if good is None:
            processor._process(
                store,
                paper,
                meta,
                match,
                run["provider"],
                False,
                False,
                expected,
                revalidate_only=True,
            )
            paper = store.paper(paper["paper_id"])
        if good is not None or mode == "save":
            processor._process(
                store,
                paper,
                meta,
                match,
                work.analysis.default_provider,
                False,
                False,
                expected,
                cached_only=True,
            )
        calls = 0
    if checkpoint:
        checkpoint()
    if mode != "validate":
        _save(work, store, paper["paper_id"])
        return dict(status="saved", message="Obsidianへ保存しました", external_calls=calls)
    return dict(
        status="ready", message="再検証が通過しました。要約済み・未保存です", external_calls=0
    )


def _call_count(store, identifier):
    return sum(
        not json.loads(r["usage_json"] or "{}").get("imported_result")
        for r in store.rows(
            "SELECT u.usage_json FROM analysis_runs a LEFT JOIN analysis_usage u USING(run_id) WHERE a.paper_id=? AND a.provider!='fake'",
            (identifier,),
        )
    )


def execute(cfg, job, *, checkpoint=None, should_stop=None, on_progress=None, provider=None):
    if job.get("binding") != binding(cfg) or job.get("mode") not in MODES:
        raise SettingsError("復旧ジョブの設定が一致しません")
    work = cfg.model_copy(deep=True)
    work.analysis.default_provider, work.analysis.model = job["provider"], job["model"]
    if not profiles_compatible(job["profile"], profile(work)):
        raise SettingsError("解析設定が変わっています。復旧対象を選び直してください")
    if job["mode"] != "validate":
        SafeWriter(work).preflight()
    if (
        job["mode"] == "reanalyze"
        and work.analysis.default_provider != "fake"
        and not (
            work.analysis.allow_external_fulltext
            and work.analysis.policy == "fulltext_external"
            and work.analysis.require_visual_reading
        )
    ):
        raise SettingsError("再解析には全文送信の許可と視覚読解が必要です")
    path = job_path(cfg, job["job_id"])
    with local_lock(cfg.storage.data_dir), Store(cfg.storage.database) as store:
        job["status"] = "running"
        job.pop("halted", None)
        json_write(path, job)
        try:
            # 認証を呼ばず、書誌・PDFの削除や変更を開始時に一度だけ照合する。
            Pipeline(cfg).scan(store)
            for item in job["items"]:
                if item["status"] in {"saved", "ready", "already_saved"}:
                    continue
                if should_stop and should_stop():
                    job["status"] = "stopped"
                    break
                item.update(status="running", message="処理中")
                json_write(path, job)
                if on_progress:
                    on_progress(job)
                before_calls = _call_count(store, item["paper_id"])
                previous_calls = item.get("external_calls", 0)
                try:
                    result = _one(
                        work,
                        store,
                        item,
                        job["mode"],
                        injected_provider=provider,
                        checkpoint=checkpoint,
                        job=job,
                    )
                    item.update(result)
                except (ProviderError, SettingsError, WriteConflict, OSError, ValueError) as exc:
                    item.update(
                        status="failed",
                        message=str(exc),
                        category=getattr(exc, "category", type(exc).__name__),
                    )
                    if getattr(exc, "category", None) in COMMON_ERRORS:
                        job["halted"] = str(exc)
                        job["status"] = "blocked"
                finally:
                    after_calls = _call_count(store, item["paper_id"])
                    item["external_calls"] = previous_calls + (
                        after_calls - before_calls if job["mode"] != "import" else 0
                    )
                json_write(path, job)
                if checkpoint:
                    checkpoint()
                if on_progress:
                    on_progress(job)
                if job.get("halted"):
                    break
            if job["status"] == "running":
                job["status"] = (
                    "partial"
                    if any(i["status"] in {"failed", "needs_analysis"} for i in job["items"])
                    else "complete"
                )
        except BaseException:
            job["status"] = "interrupted"
            raise
        finally:
            job["updated_at"] = now()
            json_write(path, job)
    return job
