"""未登録論文の乱択、固定対象の分割実行、消費量の記録。"""

import json
import os
import random
import re
import time
import uuid
from collections import Counter
from pathlib import Path

import pymupdf
import yaml

from paperflow.agents.schema import PROMPT, VISUAL_PROMPT, VISUAL_SCHEMA
from paperflow.config import SettingsError
from paperflow.drive_paths import drive_path, same_binding
from paperflow.ingestion.bibtex_reader import identity_key, normalize_doi
from paperflow.models import Bibliography, digest
from paperflow.obsidian.writer import SafeWriter, WriteConflict, local_lock
from paperflow.pipeline import Pipeline
from paperflow.portable import bib_key, profile
from paperflow.store import Store, now
from paperflow.wiki.bundle import json_write, sha, target


def binding(config):
    paths = dict(
        vault=config.destination.vault_dir,
        database=config.storage.database,
        paperpile=config.source.paperpile_dir,
        bibtex=config.source.bibtex_file,
    )
    return {
        key: os.path.normcase(str(value.resolve())) if value is not None else None
        for key, value in paths.items()
    }


def local_output(config, path):
    path = path.resolve()
    if any(p.casefold() == ".obsidian" for p in path.parts):
        raise SettingsError(".obsidianはバッチ保存先にできません")
    for protected in [config.source.paperpile_dir, config.destination.vault_dir]:
        if path.is_relative_to(protected.resolve()):
            raise SettingsError("バッチの保存先はPaperpile・Vaultの外にしてください")
    if os.name == "nt" and path.drive.upper() != "C:":
        raise SettingsError("バッチの保存先はC:内にしてください")
    return path


def metadata_keys(meta):
    result = set()
    if meta.get("doi"):
        result.add("doi:" + normalize_doi(str(meta["doi"])))
    if meta.get("arxiv_id"):
        result.add("arxiv:" + str(meta["arxiv_id"]).lower())
    if meta.get("title"):
        authors = meta.get("authors") or []
        if isinstance(authors, list) and all(isinstance(a, str) for a in authors):
            result.add(
                "meta:"
                + identity_key(
                    Bibliography(
                        citekey="",
                        title=meta["title"],
                        authors=authors,
                        year=str(meta.get("year") or ""),
                    )
                )
            )
    return result


class Registry:
    def __init__(self, config):
        self.hashes = set()
        self.keys = set()
        self.ids = set()
        self.records = []
        root = config.destination.vault_dir
        if not target(root, ".llm-wiki-root").is_file():
            raise SettingsError("Vaultマーカーがありません")
        notes = {}
        note_paths = {}
        for path in target(root, "10_Papers/20_Source_Notes").glob("*.md"):
            path = target(root, path.relative_to(root).as_posix())
            parts = path.read_text(encoding="utf-8-sig").split("---", 2)
            try:
                fm = yaml.safe_load(parts[1]) if len(parts) == 3 else {}
            except yaml.YAMLError as exc:
                raise SettingsError("既存Source NoteのPropertiesを読めません") from exc
            if not isinstance(fm, dict):
                raise SettingsError("既存Source NoteのPropertiesが不正です")
            sid = fm.get("source_id") or fm.get("id")
            if isinstance(sid, str):
                notes[sid] = fm
                note_paths[sid] = str(path)
        for path in target(root, "10_Papers/10_Sources").glob("*/manifest.yml"):
            path = target(root, path.relative_to(root).as_posix())
            try:
                manifest = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
            except yaml.YAMLError as exc:
                raise SettingsError("既存出典のmanifestを読めません") from exc
            if not isinstance(manifest, dict):
                raise SettingsError("既存出典のmanifestが不正です")
            sid = manifest.get("source_id")
            h = manifest.get("sha256")
            if sid not in notes or manifest.get("source_status") != "frozen":
                continue
            if not isinstance(h, str) or not re.fullmatch("[0-9a-f]{64}", h):
                raise SettingsError("既存出典のPDFハッシュが不正です")
            self.hashes.add(h)
            self.keys.update(metadata_keys(manifest))
            self.keys.update(metadata_keys(notes[sid]))
            for obj in [manifest, notes[sid]]:
                if obj.get("paperflow_id"):
                    self.ids.add(obj["paperflow_id"])
            self.records.append(
                dict(
                    sha256=h,
                    keys=metadata_keys(manifest) | metadata_keys(notes[sid]),
                    ids={
                        obj["paperflow_id"]
                        for obj in [manifest, notes[sid]]
                        if obj.get("paperflow_id")
                    },
                    note=note_paths[sid],
                )
            )

    def note_for(self, item):
        keys = metadata_keys(item)
        matches = {
            r["note"]
            for r in self.records
            if r["sha256"] == item["pdf_sha256"]
            or item["paper_id"] in r["ids"]
            or bool(keys & r["keys"])
        }
        return next(iter(matches)) if len(matches) == 1 else None

    def contains(self, item):
        return (
            item["pdf_sha256"] in self.hashes
            or item["paper_id"] in self.ids
            or bool(metadata_keys(item) & self.keys)
        )


def plan(
    config, *, count=None, total=None, all_papers=False, seed=None, output=None, dry_run=False
):
    if config.destination.layout != "llm_wiki":
        raise SettingsError("バッチ取り込みは正式Wikiモード専用です")
    if (
        sum([count is not None, total is not None, bool(all_papers)]) != 1
        or (count is not None and count < 1)
        or (total is not None and total < 1)
    ):
        raise SettingsError(
            "--all、--count、--totalのどれか1つを指定してください。本数は正の整数です"
        )
    output = local_output(
        config, output or config.storage.data_dir / "batches" / f"{uuid.uuid4().hex[:12]}.plan.json"
    )
    if not dry_run and output.exists():
        raise SettingsError("既存のバッチ計画は上書きしません")
    from contextlib import nullcontext

    with nullcontext() if dry_run else local_lock(config.storage.data_dir):
        with Store(config.storage.database, snapshot=dry_run) as store:
            Pipeline(config).scan(store)
            registry = Registry(config)
            pool = store.rows("SELECT * FROM papers WHERE source_present=1 ORDER BY paper_id")
            wanted = (
                len(pool)
                if all_papers
                else count
                if count is not None
                else max(0, total - len(registry.hashes))
            )
            seed = seed if seed is not None else random.SystemRandom().randrange(2**63)
            random.Random(seed).shuffle(pool)
            selected = []
            selected_hashes = set()
            selected_keys = set()
            excluded = Counter()
            for paper in pool:
                if len(selected) >= wanted:
                    break
                match = json.loads(paper["match_json"] or "{}")
                meta = json.loads(paper["metadata_json"])
                if paper["paper_id"] in registry.ids or metadata_keys(meta) & registry.keys:
                    excluded["registered"] += 1
                    continue
                if (
                    paper["error_type"]
                    or paper["state"]
                    in {"failed", "blocked", "conflict", "needs_review", "retry_wait"}
                    or match.get("status") != "pdf_matched"
                    or not match.get("path")
                ):
                    excluded["not_ready"] += 1
                    continue
                if meta.get("no_external"):
                    excluded["no_external"] += 1
                    continue
                path = Path(match["path"]).resolve()
                if not path.is_relative_to(config.source.paperpile_dir.resolve()):
                    raise SettingsError("原本範囲外のPDF対応を拒否しました")
                before = path.stat()
                h = sha(path)
                if h in registry.hashes:
                    excluded["registered"] += 1
                    continue
                if h in selected_hashes or metadata_keys(meta) & selected_keys:
                    excluded["duplicate"] += 1
                    continue
                try:
                    with pymupdf.open(path) as doc:
                        if doc.needs_pass or not len(doc):
                            raise ValueError("PDFを読めません")
                        pages = len(doc)
                except (RuntimeError, ValueError):
                    excluded["unreadable_pdf"] += 1
                    continue
                after = path.stat()
                if (
                    (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns)
                    or before.st_size != match["size"]
                    or before.st_mtime_ns != match["mtime_ns"]
                ):
                    excluded["changing"] += 1
                    continue
                if config.analysis.require_visual_reading and (
                    pages > config.analysis.visual_max_pages
                    or before.st_size > config.analysis.visual_max_pdf_mb * 1024 * 1024
                ):
                    excluded["visual_input_limit"] += 1
                    continue
                selected.append(
                    dict(
                        paper_id=paper["paper_id"],
                        title=paper["title"],
                        authors=meta["authors"],
                        year=meta["year"],
                        doi=meta.get("doi"),
                        arxiv_id=meta.get("arxiv_id"),
                        bib_hash=paper["bib_hash"],
                        portable_bib=bib_key(meta),
                        pdf=str(path),
                        pdf_sha256=h,
                        pages=pages,
                        size_bytes=before.st_size,
                    )
                )
                selected_hashes.add(h)
                selected_keys.update(metadata_keys(meta))
            data = dict(
                type="paperflow-batch",
                version=1,
                portable_runtime=True,
                batch_id=uuid.uuid4().hex,
                created_at=now(),
                binding=binding(config),
                seed=seed,
                mode="all" if all_papers else "additional" if count is not None else "total",
                requested=count if count is not None else total,
                registered_before=len(registry.hashes),
                desired_additions=None if all_papers else wanted,
                selected_count=len(selected),
                shortage=0 if all_papers else max(0, wanted - len(selected)),
                total_pages=sum(p["pages"] for p in selected),
                excluded=dict(excluded),
                items=selected,
            )
            if not dry_run:
                output.parent.mkdir(parents=True, exist_ok=True)
                with output.open("x", encoding="utf-8") as handle:
                    json.dump(data, handle, ensure_ascii=False, indent=2)
                store.db.commit()
    return dict(plan=str(output) if not dry_run else None, dry_run=dry_run, **data)


def load(config, path):
    path = local_output(config, path)
    if path.stat().st_size > 16 * 1024 * 1024:
        raise SettingsError("バッチ計画が大きすぎます")
    data = json.loads(path.read_text(encoding="utf-8"))
    if (
        data.get("type") != "paperflow-batch"
        or data.get("version") != 1
        or not same_binding(data.get("binding"), binding(config))
    ):
        raise SettingsError("別のVault・DB・原本用、または未対応のバッチ計画です")
    items = data.get("items")
    if (
        data.get("mode") not in {"additional", "total", "all"}
        or (data.get("mode") == "all" and data.get("requested") is not None)
        or (
            data.get("mode") != "all"
            and (type(data.get("requested")) is not int or data["requested"] < 1)
        )
    ):
        raise SettingsError("バッチの本数指定が不正です")
    if not isinstance(items, list) or data.get("selected_count") != len(items):
        raise SettingsError("バッチの本数が不正です")
    if len({p["paper_id"] for p in items}) != len(items) or len(
        {p["pdf_sha256"] for p in items}
    ) != len(items):
        raise SettingsError("バッチ対象が重複しています")
    for p in items:
        p["pdf"] = str(Path(drive_path(p["pdf"])).resolve())
        if (
            not isinstance(p.get("paper_id"), str)
            or not isinstance(p.get("bib_hash"), str)
            or not re.fullmatch("[0-9a-f]{64}", p.get("pdf_sha256", ""))
            or not Path(p["pdf"]).resolve().is_relative_to(config.source.paperpile_dir.resolve())
        ):
            raise SettingsError("バッチ対象の同一性・PDFパスが不正です")
    return data


def runtime_key(config):
    return digest(
        dict(
            analysis=config.analysis.model_dump(
                mode="json",
                exclude={"agy_command"} if config.analysis.default_provider != "agy" else set(),
            ),
            taxonomy=config.taxonomy(),
            visuals=config.destination.visual_max_items,
            prompt=PROMPT + VISUAL_PROMPT,
            schema=VISUAL_SCHEMA,
        )
    )


def usage_report(config, progress):
    baseline = set(progress["baseline_runs"])
    ids = set(progress["paper_ids"])
    with Store(config.storage.database, readonly=True) as store:
        rows = [
            r
            for r in store.rows(
                "SELECT a.run_id,a.paper_id,p.title,a.provider,a.model,a.status,a.elapsed_seconds,a.started_at,a.error_type,a.error_message FROM analysis_runs a JOIN papers p ON p.paper_id=a.paper_id"
            )
            if r["paper_id"] in ids and r["run_id"] not in baseline
        ]
        exists = store.rows(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='analysis_usage'"
        )
        usage = (
            {
                r["run_id"]: json.loads(r["usage_json"])
                for r in store.rows("SELECT * FROM analysis_usage")
            }
            if exists
            else {}
        )
    external = [
        r
        for r in rows
        if r["provider"] != "fake" and not usage.get(r["run_id"], {}).get("imported_result")
    ]
    totals = {}
    missing = {}
    for key in ["input_tokens", "cached_input_tokens", "output_tokens", "total_tokens", "cost_usd"]:
        values = [usage.get(r["run_id"], {}).get(key) for r in external]
        known = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
        totals[key] = sum(known) if known or not external else None
        missing[key] = len(values) - len(known)
    return dict(
        analysis_runs=len(rows),
        external_calls=len(external),
        cached_or_previous_runs_excluded=True,
        reported=totals,
        unreported_calls=missing,
        elapsed_analysis_seconds=sum(
            r["elapsed_seconds"] or 0
            for r in rows
            if not usage.get(r["run_id"], {}).get("imported_result")
        ),
        runs=[dict(**r, usage=usage.get(r["run_id"])) for r in rows],
        notice="CLI申告値の集計。未取得は不明。CLIの費用表示は利用枠の消費率・請求確定額ではありません。中断・失敗した呼出しの消費量が未取得の可能性があります。",
    )


def summarize(config, plan, progress):
    registry = Registry(config)
    done = [p["paper_id"] for p in plan["items"] if registry.contains(p)]
    return dict(
        batch_id=plan["batch_id"],
        planned=len(plan["items"]),
        registered=len(done),
        registered_ids=done,
        remaining=len(plan["items"]) - len(done),
        usage=usage_report(config, progress),
        wall_seconds=progress.get("wall_seconds", 0),
        status=progress["status"],
        halted=progress.get("halted"),
    )


def report_path(path):
    return path.with_name(path.stem + ".report.md")


def write_report(path, summary):
    from paperflow.obsidian.renderer import escape

    usage = summary["usage"]

    def shown(value):
        return "不明" if value is None else str(value)

    lines = [
        "# 論文バッチの結果",
        "",
        f"- 対象: {summary['planned']}本 / 登録済み: {summary['registered']}本 / 残り: {summary['remaining']}本",
        f"- 外部AI呼出し: {usage['external_calls']}回",
        f"- バッチの経過時間（再開分を含む）: {summary['wall_seconds']:.1f}秒",
        f"- 解析所要時間（取得分）: {usage['elapsed_analysis_seconds']:.1f}秒",
        f"- 入力トークン（取得分）: {shown(usage['reported']['input_tokens'])}",
        f"- 出力トークン（取得分）: {shown(usage['reported']['output_tokens'])}",
        f"- CLI表示費用の合計（取得分・USD）: {shown(usage['reported']['cost_usd'])}",
        "",
        usage["notice"],
        "",
        "| 論文 | Provider | モデル | 状態 | トークン合計 | CLI表示費用USD |",
        "|---|---|---|---|---|---|",
    ]
    for r in usage["runs"]:
        u = r["usage"] or {}
        lines.append(
            f"| {escape(r['title'])} (`{r['paper_id']}`) | {escape(r['provider'])} | {escape(r['model'] or '不明')} | {escape(r['status'])} | {shown(u.get('total_tokens'))} | {shown(u.get('cost_usd'))} |"
        )
    failures = [r for r in usage["runs"] if r.get("error_message")]
    if failures:
        lines += ["", "## 失敗理由", ""]
        lines.extend(f"- {escape(r['title'])}：{escape(r['error_message'])}" for r in failures)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def execute(
    config,
    path,
    *,
    dry_run=False,
    provider=None,
    on_progress=None,
    should_stop=None,
    on_paper=None,
    save_each_paper=False,
    checkpoint=None,
):
    path = local_output(config, path)
    data = load(config, path)
    registry = Registry(config)
    pending = [p for p in data["items"] if not registry.contains(p)]
    if data["mode"] == "total":
        pending = pending[: max(0, data["requested"] - len(registry.hashes))]
    if dry_run:
        return dict(
            dry_run=True,
            planned=len(data["items"]),
            remaining=len(pending),
            papers=[dict(paper_id=p["paper_id"], title=p["title"]) for p in pending],
        )
    SafeWriter(config).preflight()
    if config.analysis.default_provider != "fake" and not (
        config.analysis.policy == "fulltext_external"
        and config.analysis.allow_external_fulltext
        and config.analysis.require_visual_reading
    ):
        raise SettingsError("実論文バッチには全文送信の許可と視覚読解必須の設定が必要です")
    progress_path = path.with_name(path.stem + ".progress.json")
    report = report_path(path)
    with local_lock(config.storage.data_dir):
        started = time.monotonic()
        plan_hash = sha(path)
        key = digest(profile(config)) if data.get("portable_runtime") else runtime_key(config)
        if progress_path.exists():
            progress = json.loads(progress_path.read_text(encoding="utf-8"))
            from paperflow.portable import compatible_profiles

            allowed_keys = (
                {digest(p) for p in compatible_profiles(profile(config))}
                if data.get("portable_runtime")
                else {key}
            )
            if (
                progress.get("plan_sha256") != plan_hash
                or progress.get("runtime_key") not in allowed_keys
            ):
                raise WriteConflict(
                    "バッチ計画・Provider・モデル・解析設定が前回と違います。別の計画を作成してください"
                )
            progress["runtime_key"] = key
        else:
            if report.exists():
                raise WriteConflict("管理外の既存バッチレポートは上書きしません")
            with Store(config.storage.database) as store:
                baseline = [r["run_id"] for r in store.rows("SELECT run_id FROM analysis_runs")]
            progress = dict(
                plan_sha256=plan_hash,
                runtime_key=key,
                started_at=now(),
                baseline_runs=baseline,
                paper_ids=[p["paper_id"] for p in data["items"]],
                status="running",
                provider=config.analysis.default_provider,
                model=config.analysis.model,
                failures=[],
            )
        progress["status"] = "running"
        progress.pop("halted", None)
        json_write(progress_path, progress)
        attempted = set()
        chunk_size = min(config.sync.max_papers_per_run, config.analysis.max_cli_calls_per_run)
        if save_each_paper:
            chunk_size = 1
        try:
            while True:
                if should_stop and should_stop():
                    break
                registry = Registry(config)
                pending = [
                    p
                    for p in data["items"]
                    if p["paper_id"] not in attempted and not registry.contains(p)
                ]
                if data["mode"] == "total":
                    pending = pending[: max(0, data["requested"] - len(registry.hashes))]
                if not pending:
                    break
                chunk = pending[:chunk_size]
                selected = {p["paper_id"]: p for p in chunk}
                attempted.update(selected)
                result = Pipeline(config, provider=provider).run(
                    provider_name=config.analysis.default_provider,
                    paper_ids=list(selected),
                    limit=len(chunk),
                    expected_items=selected,
                    _lock_held=True,
                    should_stop=should_stop,
                    on_paper=on_paper,
                    checkpoint=checkpoint,
                )
                progress["failures"].extend(result["failures"])
                progress["updated_at"] = now()
                json_write(progress_path, progress)
                if checkpoint:
                    checkpoint()
                summary = summarize(config, data, progress)
                write_report(report, summary)
                if on_progress:
                    on_progress(
                        dict(
                            registered=summary["registered"],
                            planned=summary["planned"],
                            attempted=len(attempted),
                            external_calls=summary["usage"]["external_calls"],
                            registered_ids=summary["registered_ids"],
                        )
                    )
                if result.get("halted"):
                    progress["halted"] = result["halted"]
                    break
            registry = Registry(config)
            progress["status"] = (
                "complete"
                if not any(not registry.contains(p) for p in data["items"])
                or (data["mode"] == "total" and len(registry.hashes) >= data["requested"])
                else "blocked"
                if progress.get("halted")
                else "stopped"
                if should_stop and should_stop()
                else "partial"
            )
        except BaseException:
            progress["status"] = "interrupted"
            raise
        finally:
            progress["wall_seconds"] = progress.get("wall_seconds", 0) + (
                time.monotonic() - started
            )
            progress["updated_at"] = now()
            json_write(progress_path, progress)
            summary = summarize(config, data, progress)
            write_report(report, summary)
    summary["report"] = str(report)
    summary["progress"] = str(progress_path)
    return summary


def report(config, path):
    data = load(config, path)
    progress_path = path.with_name(path.stem + ".progress.json")
    if not progress_path.exists():
        registry = Registry(config)
        return dict(
            status="not_started",
            planned=len(data["items"]),
            remaining=sum(not registry.contains(p) for p in data["items"]),
        )
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    if progress.get("plan_sha256") != sha(path):
        raise WriteConflict("バッチ計画が実行時から変わっています")
    return summarize(config, data, progress)


def live_report(config, path):
    """実行とは独立した読み取り専用の表示。G:/H:の列挙・検証・書き込みをしない。"""
    path = Path(path)
    data = load(config, path)
    progress_path = path.with_name(path.stem + ".progress.json")
    progress = (
        json.loads(progress_path.read_text(encoding="utf-8"))
        if progress_path.exists()
        else dict(
            status="not_started", baseline_runs=[], paper_ids=[p["paper_id"] for p in data["items"]]
        )
    )
    if progress.get("plan_sha256") and progress["plan_sha256"] != sha(path):
        raise WriteConflict("バッチ計画が実行時から変わっています")
    ids = {p["paper_id"] for p in data["items"]}
    with Store(config.storage.database, readonly=True) as store:
        exported = {
            r["paper_id"]
            for r in store.rows("SELECT paper_id FROM exports WHERE export_kind='WikiSource'")
        }
    registered = (exported | set(progress.get("registered_ids", []))) & ids
    return dict(
        batch_id=data["batch_id"],
        planned=len(ids),
        registered=len(registered),
        registered_ids=sorted(registered),
        remaining=len(ids) - len(registered),
        usage=usage_report(config, progress),
        status=progress["status"],
        wall_seconds=progress.get("wall_seconds", 0),
        halted=progress.get("halted"),
        registration_basis="保存完了をDBへ記録した件数。Vaultの再検証は実行しません。",
    )
