import json
import uuid
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path

from paperflow.agents.agy_cli import AgyCliProvider
from paperflow.agents.base import AnalysisRequest, PDFDocument, ProviderError
from paperflow.agents.claude_cli import ClaudeCliProvider
from paperflow.agents.codex_cli import CodexCliProvider
from paperflow.agents.fake import FakeProvider
from paperflow.agents.schema import PROMPT, SCHEMA_VERSION, VISUAL_PROMPT, schema_for, validate
from paperflow.config import Config, SettingsError
from paperflow.ingestion.bibtex_reader import read_bibtex
from paperflow.ingestion.matcher import match_pdf
from paperflow.ingestion.pdf_discovery import discover
from paperflow.models import digest
from paperflow.obsidian.renderer import render_analysis, render_paper
from paperflow.obsidian.writer import SafeWriter, WriteConflict, local_lock
from paperflow.parsing.pdf_text import PARSER_VERSION, extract_pdf
from paperflow.portable import analysis_key, bib_key, compatible_profiles, profile
from paperflow.store import Store, now


class Pipeline:
    def __init__(self, config: Config, provider=None):
        self.config = config
        self.injected_provider = provider
        self.cli_calls = 0

    def _provider(self, name):
        if self.injected_provider is not None:
            return self.injected_provider
        return {
            "fake": FakeProvider,
            "claude": lambda: ClaudeCliProvider(self.config),
            "codex": lambda: CodexCliProvider(self.config),
            "agy": lambda: AgyCliProvider(self.config),
        }[name]()

    def scan(self, store: Store):
        config = self.config
        candidates = discover(config.source.paperpile_dir, config.source.recurse)
        bibfile = config.source.bibtex_file
        entries = read_bibtex(bibfile) if bibfile else None
        issues = []
        ids = []
        with store.transaction():
            # Only uninterrupted consecutive observations count toward stability.
            seen_paths = {str(c.path) for c in candidates}
            for old in store.rows("SELECT source_path FROM observations"):
                if old["source_path"] not in seen_paths:
                    store.db.execute(
                        "DELETE FROM observations WHERE source_path=?", (old["source_path"],)
                    )
            for candidate in candidates:
                previous = store.rows(
                    "SELECT * FROM observations WHERE source_path=?", (str(candidate.path),)
                )
                if (
                    previous
                    and previous[0]["size"] == candidate.size
                    and previous[0]["mtime_ns"] == candidate.mtime_ns
                ):
                    candidate.stable_count = min(
                        previous[0]["stable_count"] + 1, config.sync.stable_scan_count
                    )
                store.db.execute(
                    "INSERT OR REPLACE INTO observations VALUES (?,?,?,?)",
                    (
                        str(candidate.path),
                        candidate.size,
                        candidate.mtime_ns,
                        candidate.stable_count,
                    ),
                )
            if entries is not None:
                for bib in entries:
                    paper_id = store.upsert_bib(bib)
                    ids.append(paper_id)
                    old = store.paper(paper_id)
                    match = match_pdf(bib, candidates, config.source.paperpile_dir)
                    status = match.status
                    match_data = {
                        "status": status,
                        "score": match.score,
                        "path": None,
                        "candidates": match.candidates,
                        "reason": match.reason,
                    }
                    if match.candidate:
                        c = match.candidate
                        match_data.update(path=str(c.path), size=c.size, mtime_ns=c.mtime_ns)
                        if c.size == 0 or c.stable_count < config.sync.stable_scan_count:
                            status = "retry_wait"
                        store.db.execute(
                            """INSERT INTO attachments
                            (attachment_id,paper_id,source_path,size,mtime_ns,is_primary,match_score,match_status)
                            VALUES (?,?,?,?,?,1,?,?) ON CONFLICT(paper_id,source_path) DO UPDATE SET
                            size=excluded.size,mtime_ns=excluded.mtime_ns,match_score=excluded.match_score,
                            match_status=excluded.match_status""",
                            (
                                uuid.uuid4().hex,
                                paper_id,
                                str(c.path),
                                c.size,
                                c.mtime_ns,
                                match.score,
                                status,
                            ),
                        )
                    serialized = json.dumps(match_data, sort_keys=True, ensure_ascii=False)
                    changed = old["match_json"] != serialized or old["state"] == "metadata_ready"
                    if changed:
                        if old["error_type"] in {"sync_result_unknown", "sync_legacy_review"}:
                            store.update(paper_id, match_json=serialized)
                            continue
                        store.update(
                            paper_id,
                            state=status if status != "metadata_only" else "metadata_ready",
                            match_json=serialized,
                            error_type=None,
                            error_message=None,
                            retry_count=0,
                            next_retry_at=None,
                            source_hash=None,
                        )
                    elif (
                        old["state"] == "retry_wait"
                        and not old["error_type"]
                        and status == "pdf_matched"
                    ):
                        store.update(paper_id, state=status)
                    if status in {"needs_review", "retry_wait"}:
                        issues.append(
                            {"paper_id": paper_id, "reason": status, "match_reason": match.reason}
                        )
                store.db.execute("UPDATE papers SET source_present=0")
                for paper_id in set(ids):
                    store.db.execute(
                        "UPDATE papers SET source_present=1 WHERE paper_id=?", (paper_id,)
                    )
        return {
            "pdf_candidates": len(candidates),
            "bib_entries": len(entries) if entries is not None else 0,
            "papers": len(set(ids)),
            "issues": issues,
            "missing_from_source": len(
                store.rows("SELECT paper_id FROM papers WHERE source_present=0")
            ),
            "mode": "bibliography" if entries is not None else "pdf_candidates_only",
            "message": None
            if entries is not None
            else "BibTeX未設定。PDF候補の検出のみ。書誌情報は生成しません。",
        }

    def run(
        self,
        *,
        dry_run=False,
        scan_only=False,
        provider_name=None,
        limit=None,
        paper_ids=None,
        force=False,
        export=True,
        retry=False,
        expected_items=None,
        _lock_held=False,
        should_stop=None,
        on_paper=None,
        checkpoint=None,
    ):
        config = self.config
        name = provider_name or config.analysis.default_provider
        self.cli_calls = 0
        self.wiki_items = []
        self.checkpoint = checkpoint or (lambda: None)
        if not dry_run and not scan_only and checkpoint is None:
            from paperflow.progress_sync import SyncError, settings

            if settings(config).get("enabled"):
                raise SyncError("PC間同期が有効です。共有状態を確認するGUIから実行してください")
        if expected_items is not None and (not paper_ids or set(expected_items) != set(paper_ids)):
            raise SettingsError("固定対象と処理する文献IDが一致しません")
        if limit is not None and limit < 1:
            raise SettingsError("limitは1以上にしてください")
        if not dry_run and not scan_only and export:
            SafeWriter(config).preflight()
        lock = nullcontext() if dry_run or _lock_held else local_lock(config.storage.data_dir)
        with lock, Store(config.storage.database, snapshot=dry_run) as store:
            if not dry_run and export and not scan_only:
                existing_notes = config.destination.vault_dir / "PaperFlow/Papers"
                if (
                    existing_notes.is_dir()
                    and any(existing_notes.glob("*.md"))
                    and not store.rows("SELECT paper_id FROM papers LIMIT 1")
                ):
                    raise SettingsError(
                        "既存生成ノートがありますがDBが空です。DBを復元してから再実行してください"
                    )
            summary = self.scan(store)
            summary.update(
                dry_run=dry_run, analyzed=0, exported=0, skipped=0, failures=[], proposed=[]
            )
            if scan_only or summary["mode"] == "pdf_candidates_only":
                return summary
            papers = store.rows(
                "SELECT * FROM papers WHERE source_present=1 ORDER BY created_at,paper_id"
            )
            if paper_ids:
                for paper_id in paper_ids:
                    current = store.paper(paper_id)
                    if expected_items is not None and not current["source_present"]:
                        summary["failures"].append(
                            dict(
                                paper_id=paper_id,
                                category="missing_from_source",
                                message="固定した文献が現在のBibTeXにありません。外部送信しません",
                            )
                        )
                papers = [p for p in papers if p["paper_id"] in paper_ids]
            if retry:
                papers = [
                    p
                    for p in papers
                    if p["state"] == "retry_wait"
                    and p["error_type"]
                    and p["retry_count"] <= config.analysis.max_retries
                    and (not p["next_retry_at"] or p["next_retry_at"] <= now())
                ]
            attempted = 0
            for paper in papers:
                if should_stop and should_stop():
                    break
                if attempted >= min(
                    limit or config.sync.max_papers_per_run, config.sync.max_papers_per_run
                ):
                    break
                if paper["state"] in {"failed", "blocked", "conflict"} and not (force or paper_ids):
                    summary["skipped"] += 1
                    continue
                if paper["error_type"] in {"sync_result_unknown", "sync_legacy_review"}:
                    from paperflow.progress_sync import SyncError

                    raise SyncError(
                        "前回の解析結果が未確認です。「PC間同期」で確認済みとして解除してください"
                    )
                if (
                    paper["error_type"]
                    and paper["state"] == "retry_wait"
                    and not (retry or force or paper_ids)
                ):
                    summary["skipped"] += 1
                    continue
                match = json.loads(paper["match_json"] or "{}")
                if paper["state"] == "retry_wait" and not paper["error_type"]:
                    summary["skipped"] += 1
                    continue
                metadata = json.loads(paper["metadata_json"])
                if dry_run:
                    summary["proposed"].append(
                        {
                            "paper_id": paper["paper_id"],
                            "title": paper["title"],
                            "match": match.get("status"),
                            "provider": name,
                            "operation": "extract/analyze/export"
                            if match.get("path") or metadata["abstract"]
                            else "metadata_only",
                        }
                    )
                    attempted += 1
                    continue
                try:
                    if on_paper:
                        on_paper(
                            dict(phase="started", paper_id=paper["paper_id"], title=paper["title"])
                        )
                    outcome = self._process(
                        store,
                        paper,
                        metadata,
                        match,
                        name,
                        force,
                        export,
                        (expected_items or {}).get(paper["paper_id"]),
                    )
                    if outcome["analyzed"] or outcome["exported"]:
                        attempted += 1
                    for key, value in outcome.items():
                        summary[key] += value
                    if on_paper:
                        on_paper(
                            dict(
                                phase="processed", paper_id=paper["paper_id"], title=paper["title"]
                            )
                        )
                except (ProviderError, WriteConflict, OSError, ValueError) as exc:
                    attempted += 1
                    self._record_error(store, paper, exc)
                    self.checkpoint()
                    summary["failures"].append(
                        {
                            "paper_id": paper["paper_id"],
                            "category": getattr(exc, "category", type(exc).__name__),
                            "message": str(exc)
                            if isinstance(exc, (ProviderError, WriteConflict))
                            else "ローカルファイルの読み取り・解析・出力に失敗しました",
                        }
                    )
                    if on_paper:
                        on_paper(
                            dict(
                                phase="failed",
                                paper_id=paper["paper_id"],
                                title=paper["title"],
                                message=summary["failures"][-1]["message"],
                            )
                        )
                    if getattr(exc, "category", None) in {
                        "authentication",
                        "missing_cli",
                        "cli_version",
                        "cli_wrapper",
                        "launch",
                    }:
                        summary["halted"] = summary["failures"][-1]
                        break
            if not dry_run:
                store.db.commit()
            if self.wiki_items:
                from paperflow.wiki.bundle import apply
                from paperflow.wiki.export import prepare, record_exports

                bundle = config.storage.data_dir / "wiki-bundles" / uuid.uuid4().hex[:12]
                self.checkpoint()
                plan = prepare(config, self.wiki_items, bundle)
                result = apply(config, bundle)
                summary["exported"] += result["written"]
                summary["wiki_bundle"] = str(bundle)
                record_exports(config, store, plan)
                self.checkpoint()
            return summary

    def analyze_variant(self, store, paper, metadata, match, *, checkpoint=None, on_started=None):
        """指定文献の新しい解析だけを保存し、正式版・文献の処理状態は変えない。"""
        self.cli_calls = 0
        self.checkpoint = checkpoint or (lambda: None)
        self.variant_started = on_started
        try:
            self._process(
                store,
                paper,
                metadata,
                match,
                self.config.analysis.default_provider,
                True,
                False,
                preserve_paper=True,
            )
        except (ProviderError, WriteConflict, OSError, ValueError) as exc:
            self._record_error(store, paper, exc, preserve_paper=True)
            self.checkpoint()
            raise
        return store.latest_analysis(paper["paper_id"])

    def _process(
        self,
        store,
        paper,
        metadata,
        match,
        name,
        force,
        export,
        expected_item=None,
        *,
        preserve_paper=False,
        revalidate_only=False,
        cached_only=False,
    ):
        config = self.config
        external = name != "fake"
        if external and metadata.get("no_external") and not (revalidate_only or cached_only):
            raise ProviderError("no_external", "この文献は外部送信禁止です")
        extraction, pdf_hash, pdf = None, None, match.get("path")
        if pdf and match["status"] == "pdf_matched":
            path = Path(pdf)
            # Metadata stability is checked again immediately before and after extraction.
            before = path.stat()
            if before.st_size != match["size"] or before.st_mtime_ns != match["mtime_ns"]:
                raise ProviderError("file_changing", "PDFが更新中です", True)
            with path.open("rb") as stream:
                from hashlib import file_digest

                pdf_hash = file_digest(stream, "sha256").hexdigest()
            if expected_item is not None and (
                (
                    bib_key(metadata) != expected_item["portable_bib"]
                    if "portable_bib" in expected_item
                    else paper["bib_hash"] != expected_item["bib_hash"]
                )
                or str(path) != expected_item["pdf"]
                or pdf_hash != expected_item["pdf_sha256"]
            ):
                raise WriteConflict(
                    "固定したバッチの書誌・PDFが変わっています。再送せず停止しました"
                )
            extraction = extract_pdf(path, config.analysis.max_chars)
            after = path.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ProviderError("file_changing", "PDFが抽出中に更新されました", True)
            with store.transaction():
                store.db.execute(
                    "UPDATE attachments SET sha256=? WHERE paper_id=? AND source_path=?",
                    (pdf_hash, paper["paper_id"], pdf),
                )
        if expected_item is not None and (not pdf_hash or match["status"] != "pdf_matched"):
            raise WriteConflict("固定したバッチのPDF対応を確認できません")
        text, coverage, pages = metadata["abstract"][: config.analysis.max_chars], "abstract", []
        fulltext = not external or (
            config.analysis.policy == "fulltext_external"
            and config.analysis.allow_external_fulltext
        )
        if fulltext and extraction and extraction.quality == "good":
            text, coverage = extraction.text, extraction.coverage
            # Only pages actually included in the truncated prompt may be cited.
            pages = [
                p["page_number"] for p in extraction.pages if f"[page {p['page_number']}]" in text
            ]
        # File paths and any fulltext cache never enter the metadata sent to external CLIs.
        safe_metadata = {
            k: metadata[k] for k in ("title", "authors", "year", "doi", "arxiv_id", "venue")
        }
        request = AnalysisRequest(
            paper["paper_id"],
            safe_metadata,
            text,
            coverage,
            pages,
            config.taxonomy(),
            text_truncated=(
                extraction.text_truncated
                if fulltext and extraction and extraction.quality == "good"
                else len(metadata["abstract"]) > config.analysis.max_chars
            ),
            extraction_quality=extraction.quality if extraction else "not_extracted",
            research_context=config.analysis.research_context,
        )
        if external and fulltext and config.analysis.require_visual_reading:
            if extraction is None or not pdf_hash:
                raise ProviderError("visual_input", "視覚読解には確定したPDFが必要です")
            request.document = PDFDocument(
                Path(pdf), pdf_hash, len(extraction.pages), config.destination.visual_max_items
            )
            if request.document.page_count > config.analysis.visual_max_pages:
                raise ProviderError(
                    "visual_input_limit",
                    "PDFが視覚読解のページ上限を超えています。文字のみには切り替えません",
                )
            if before.st_size > config.analysis.visual_max_pdf_mb * 1024 * 1024:
                raise ProviderError("visual_input_limit", "PDFが視覚読解の容量上限を超えています")
            request.pages_available = [p["page_number"] for p in extraction.pages]
            if extraction.quality != "good":
                request.text = extraction.text
                request.coverage = coverage = "partial"
                request.text_truncated = extraction.text_truncated
        identity = dict(
            bib=paper["bib_hash"],
            pdf=pdf_hash,
            match=match,
            provider=name,
            model=config.analysis.model,
            policy=config.analysis.policy,
            text=digest(request.text),
            coverage=coverage,
            taxonomy=request.taxonomy,
            research_context=request.research_context,
            schema=digest(schema_for(request)),
            prompt=digest(PROMPT + (VISUAL_PROMPT if request.document else "")),
            parser=PARSER_VERSION,
        )
        if request.document:
            identity["visual_input"] = dict(
                page_count=request.document.page_count,
                max_selected_visuals=request.document.max_selected_visuals,
                input_version={"claude": "native-pdf-v1", "agy": "native-pdf-readonly-v1"}.get(
                    name, "page-images-v1"
                ),
            )
        source_hash = digest(identity)
        if revalidate_only:
            from paperflow.response_recovery import revalidate_run

            return revalidate_run(config, store, paper, request, identity)
        latest = store.latest_analysis(paper["paper_id"], source_hash)
        portable_key, portable_input = analysis_key(config, metadata, pdf_hash, request, name)
        latest = latest or store.portable_analysis(paper["paper_id"], portable_key)
        if latest is None:
            for compatible in compatible_profiles(portable_input["profile"])[1:]:
                key = digest(dict(portable_input, profile=compatible))
                latest = store.portable_analysis(paper["paper_id"], key)
                if latest:
                    break
        if latest is None:
            expected_legacy = dict(
                bib=bib_key(metadata),
                pdf=pdf_hash,
                profile=profile(config, name),
                coverage=request.coverage,
                schema="v2-visual" if request.document else SCHEMA_VERSION,
            )
            for compatible in compatible_profiles(expected_legacy["profile"]):
                legacy_key = digest(dict(legacy_fixed=dict(expected_legacy, profile=compatible)))
                latest = store.portable_analysis(paper["paper_id"], legacy_key)
                if latest:
                    break
        analysis = None
        analyzed = 0
        if request.text.strip() or request.document is not None:
            if latest and not force:
                analysis = dict(latest, data=validate(json.loads(latest["output_json"]), request))
                with store.transaction():
                    store.db.execute(
                        "INSERT OR IGNORE INTO analysis_aliases VALUES (?,?)",
                        (latest["run_id"], source_hash),
                    )
                    store.db.execute(
                        "INSERT OR REPLACE INTO portable_analyses VALUES (?,?,?)",
                        (
                            latest["run_id"],
                            portable_key,
                            json.dumps(portable_input, ensure_ascii=False),
                        ),
                    )
            else:
                if cached_only:
                    raise ProviderError(
                        "saved_unavailable",
                        "現在の入力と一致する保存済み解析がありません。AIへの再送は行いません",
                    )
                if external and self.cli_calls >= config.analysis.max_cli_calls_per_run:
                    raise ProviderError("call_limit", "今回のCLI呼び出し回数上限です", True)
                run_id, started = uuid.uuid4().hex, now()
                with store.transaction():
                    store.db.execute(
                        "UPDATE analysis_runs SET status='interrupted',ended_at=? "
                        "WHERE paper_id=? AND status='running'",
                        (started, paper["paper_id"]),
                    )
                    store.db.execute(
                        """INSERT INTO analysis_runs
                        (run_id,paper_id,provider,model,status,coverage,source_hash,schema_version,
                         prompt_version,parser_version,started_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            run_id,
                            paper["paper_id"],
                            name,
                            config.analysis.model,
                            "running",
                            coverage,
                            source_hash,
                            "v2-visual" if request.document else SCHEMA_VERSION,
                            config.analysis.prompt_version,
                            PARSER_VERSION,
                            started,
                        ),
                    )
                if preserve_paper and self.variant_started:
                    self.variant_started(run_id, started)
                self.checkpoint()
                if external:
                    self.cli_calls += 1
                result = self._provider(name).analyze(request)
                if result.usage is not None:
                    with store.transaction():
                        store.db.execute(
                            "INSERT OR REPLACE INTO analysis_usage VALUES (?,?)",
                            (run_id, json.dumps(result.usage)),
                        )
                result.data = validate(result.data, request)
                output_dir = config.storage.data_dir / "analysis"
                output_dir.mkdir(parents=True, exist_ok=True)
                output_path = output_dir / f"{run_id}.json"
                encoded = json.dumps(result.data, ensure_ascii=False, indent=2)
                output_path.write_text(encoded, encoding="utf-8")
                with store.transaction():
                    store.db.execute(
                        """UPDATE analysis_runs SET status='success',model=?,output_json_path=?,
                        output_json=?,ended_at=?,elapsed_seconds=? WHERE run_id=?""",
                        (
                            result.model,
                            str(output_path),
                            encoded,
                            now(),
                            result.elapsed_seconds,
                            run_id,
                        ),
                    )
                    store.db.execute(
                        "INSERT OR REPLACE INTO portable_analyses VALUES (?,?,?)",
                        (run_id, portable_key, json.dumps(portable_input, ensure_ascii=False)),
                    )
                    if not preserve_paper:
                        store.update(paper["paper_id"], state="analyzed", source_hash=source_hash)
                self.checkpoint()
                analysis = dict(
                    data=result.data,
                    run_id=run_id,
                    provider=name,
                    model=result.model,
                    started_at=started,
                )
                analyzed = 1
        exports = 0
        if export and config.destination.layout == "llm_wiki":
            if not pdf_hash or not analysis:
                raise ProviderError("wiki_input", "正式Wiki登録には確定PDFと有効な解析が必要です")
            self.wiki_items.append(
                dict(
                    paper=paper,
                    metadata=metadata,
                    analysis=analysis,
                    pdf=pdf,
                    pdf_hash=pdf_hash,
                    text_truncated=request.text_truncated,
                )
            )
        elif export:
            writer = SafeWriter(config)
            texts = {
                "Papers": render_paper(paper, metadata, pdf=pdf),
                "Analysis": render_analysis(
                    paper, metadata, analysis, source_hash, pdf, extraction
                ),
            }
            for kind, text in texts.items():
                old = store.rows(
                    "SELECT * FROM exports WHERE paper_id=? AND export_kind=?",
                    (paper["paper_id"], kind),
                )
                try:
                    status, generated_hash = writer.write(
                        f"{kind}/{paper['paper_id']}.md",
                        text,
                        old[0]["generated_hash"] if old else None,
                    )
                except WriteConflict:
                    conflicts = config.storage.data_dir / "conflicts"
                    conflicts.mkdir(parents=True, exist_ok=True)
                    (conflicts / f"{paper['paper_id']}-{uuid.uuid4().hex}.md").write_text(
                        text, encoding="utf-8"
                    )
                    raise
                with store.transaction():
                    store.db.execute(
                        "INSERT OR REPLACE INTO exports VALUES (?,?,?,?,?,?,?)",
                        (
                            paper["paper_id"],
                            kind,
                            str(writer.target(f"{kind}/{paper['paper_id']}.md")),
                            generated_hash,
                            analysis["run_id"] if analysis else None,
                            now(),
                            status,
                        ),
                    )
                exports += status == "written"
        if preserve_paper:
            return dict(analyzed=analyzed, exported=0, skipped=int(not analyzed))
        with store.transaction():
            store.update(
                paper["paper_id"],
                state="needs_review"
                if match.get("status") == "needs_review"
                else (
                    "exported"
                    if export and config.destination.layout != "llm_wiki"
                    else "abstract_analyzed"
                    if coverage == "abstract"
                    else "analyzed"
                ),
                source_hash=source_hash,
                error_type=None,
                error_message=None,
                retry_count=0,
                next_retry_at=None,
            )
        return dict(analyzed=analyzed, exported=exports, skipped=int(not analyzed and not exports))

    def _record_error(self, store, paper, error, *, preserve_paper=False):
        retryable = isinstance(error, OSError) or getattr(error, "retryable", False)
        category = getattr(error, "category", type(error).__name__)
        count = paper["retry_count"] + 1
        state = (
            "retry_wait" if retryable and count <= self.config.analysis.max_retries else "failed"
        )
        if isinstance(error, WriteConflict):
            state = "conflict"
        elif category in {"no_external", "authentication"}:
            state = "blocked"
        delay = self.config.analysis.retry_base_seconds * 2 ** (count - 1)
        next_retry = (
            (datetime.now(UTC) + timedelta(seconds=delay)).isoformat()
            if state == "retry_wait"
            else None
        )
        message = (
            str(error)
            if isinstance(error, (ProviderError, WriteConflict))
            else "ローカル処理に失敗しました"
        )
        with store.transaction():
            if hasattr(error, "rejected_response") or hasattr(error, "raw_response"):
                try:
                    diagnostics = self.config.storage.data_dir / "diagnostics"
                    diagnostics.mkdir(parents=True, exist_ok=True)
                    for run in store.rows(
                        "SELECT run_id FROM analysis_runs WHERE paper_id=? AND status='running'",
                        (paper["paper_id"],),
                    ):
                        diagnostic = diagnostics / (run["run_id"] + ".rejected.json")
                        detail = dict(
                            type="paperflow-rejected-response",
                            accepted=False,
                            run_id=run["run_id"],
                            category=category,
                            input_receipt=getattr(error, "input_receipt", None),
                            response=getattr(error, "rejected_response", None),
                        )
                        if hasattr(error, "raw_response"):
                            detail.update(
                                raw_response=error.raw_response,
                                parse_error=getattr(error, "parse_error", None),
                            )
                        if hasattr(error, "normalized_response"):
                            detail["normalized_response"] = error.normalized_response
                        diagnostic.write_text(
                            json.dumps(
                                detail,
                                ensure_ascii=True,
                                indent=2,
                            ),
                            encoding="utf-8",
                        )
                except OSError:
                    message += "（診断JSONの保存にも失敗しました）"
            if getattr(error, "usage", None) is not None:
                for run in store.rows(
                    "SELECT run_id FROM analysis_runs WHERE paper_id=? AND status='running'",
                    (paper["paper_id"],),
                ):
                    store.db.execute(
                        "INSERT OR REPLACE INTO analysis_usage VALUES (?,?)",
                        (run["run_id"], json.dumps(error.usage)),
                    )
            if not preserve_paper:
                store.update(
                    paper["paper_id"],
                    state=state,
                    error_type=category,
                    error_message=message,
                    retry_count=count,
                    next_retry_at=next_retry,
                )
            store.db.execute(
                """UPDATE analysis_runs SET status='failed',error_type=?,error_message=?,
                retryable=?,ended_at=?,elapsed_seconds=COALESCE(?,elapsed_seconds)
                WHERE paper_id=? AND status='running'""",
                (
                    category,
                    message,
                    int(retryable),
                    now(),
                    getattr(error, "elapsed_seconds", None),
                    paper["paper_id"],
                ),
            )
