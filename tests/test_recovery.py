import json
from pathlib import Path

import pytest
from conftest import tree
from test_batches import batch_config as batch_fixture
from test_response_recovery import RawRejectedProvider, RejectedProvider

from paperflow import recovery
from paperflow.agents.base import AnalysisResult, ProviderError
from paperflow.agents.fake import FakeProvider
from paperflow.config import SettingsError
from paperflow.pipeline import Pipeline
from paperflow.store import Store


@pytest.fixture
def cfg(tmp_path):
    cfg = batch_fixture.__wrapped__(tmp_path)
    cfg.sync.max_papers_per_run = 100
    return cfg


def ids(cfg):
    with Store(cfg.storage.database, readonly=True) as store:
        return [r["paper_id"] for r in store.rows("SELECT paper_id FROM papers ORDER BY paper_id")]


def forbid(*a, **k):
    raise AssertionError("AIを呼び出しました")


def test_overview_is_read_only_and_categorizes_failures_and_ready(cfg, monkeypatch):
    chosen = ids(cfg)[:3]
    Pipeline(cfg).run(paper_ids=[chosen[0]], export=False)

    class Fail:
        def analyze(self, request):
            raise ProviderError("authentication", "合成の認証失敗")

    Pipeline(cfg, Fail()).run(paper_ids=[chosen[1]], export=False)
    with Store(cfg.storage.database) as store, store.transaction():
        store.update(
            chosen[2], error_type="sync_result_unknown", error_message="結果未確認", state="blocked"
        )
    before = tree(cfg.storage.data_dir.parent)
    monkeypatch.setattr(Pipeline, "_provider", forbid)
    first = recovery.overview(cfg)
    assert first["counts"] == {"ready": 1, "retry": 1, "attention": 1}
    assert tree(cfg.storage.data_dir.parent) == before
    assert [r for r in first["rows"] if r["paper_id"] == chosen[1]][0]["failed_runs"] == 1


def test_save_only_job_never_calls_ai_and_is_resumable(cfg, monkeypatch):
    chosen = ids(cfg)[:2]
    Pipeline(cfg).run(paper_ids=chosen, export=False)
    original = tree(cfg.source.paperpile_dir)
    before = tree(cfg.storage.data_dir.parent)
    job = recovery.prepare_job(cfg, chosen, "save")
    assert tree(cfg.storage.data_dir.parent) == before
    monkeypatch.setattr(Pipeline, "_provider", forbid)
    stop = []
    result = recovery.execute(
        cfg,
        job,
        should_stop=lambda: bool(stop),
        on_progress=lambda j: (
            stop.append(True) if any(i["status"] == "saved" for i in j["items"]) else None
        ),
    )
    assert result["status"] == "stopped", result
    assert [i["status"] for i in result["items"]].count("saved") == 1
    stop.clear()
    loaded = recovery.load_job(cfg, job["job_id"])
    assert recovery.execute(cfg, loaded)["status"] == "complete"
    assert all(i["external_calls"] == 0 for i in loaded["items"])
    assert tree(cfg.source.paperpile_dir) == original
    with Store(cfg.storage.database, readonly=True) as store:
        assert len(store.rows("SELECT * FROM exports")) == 2
        assert len(store.rows("SELECT * FROM analysis_runs")) == 2


def test_bulk_local_revalidation_continues_past_missing_answers(cfg, monkeypatch):
    cfg.analysis.default_provider = "agy"
    cfg.analysis.model = "synthetic-model"
    cfg.analysis.policy = "fulltext_external"
    cfg.analysis.allow_external_fulltext = True
    cfg.analysis.require_visual_reading = True
    chosen = ids(cfg)[:2]
    Pipeline(cfg, RejectedProvider()).run(paper_ids=chosen, export=False)
    with Store(cfg.storage.database, readonly=True) as store:
        run = store.rows("SELECT run_id FROM analysis_runs WHERE paper_id=?", (chosen[0],))[0]
    (cfg.storage.data_dir / "diagnostics" / (run["run_id"] + ".rejected.json")).unlink()
    job = recovery.prepare_job(cfg, chosen, "validate")
    vault = tree(cfg.destination.vault_dir)
    monkeypatch.setattr(Pipeline, "_provider", forbid)
    result = recovery.execute(cfg, job)
    assert result["status"] == "partial"
    assert [i["status"] for i in result["items"]] == ["needs_analysis", "ready"]
    assert tree(cfg.destination.vault_dir) == vault
    assert all(i["external_calls"] == 0 for i in result["items"])


def test_saved_raw_json_can_be_selected_and_saved_without_ai(cfg, monkeypatch):
    cfg.analysis.default_provider = "agy"
    cfg.analysis.model = "synthetic-model"
    cfg.analysis.policy = "fulltext_external"
    cfg.analysis.allow_external_fulltext = True
    cfg.analysis.require_visual_reading = True
    chosen = ids(cfg)[:1]
    Pipeline(cfg, RawRejectedProvider()).run(paper_ids=chosen, export=False)
    assert recovery.overview(cfg)["counts"] == {"revalidate": 1}
    job = recovery.prepare_job(cfg, chosen, "save")
    monkeypatch.setattr(Pipeline, "_provider", forbid)
    result = recovery.execute(cfg, job)
    assert result["status"] == "complete", result
    assert result["items"][0]["status"] == "saved"
    assert result["items"][0]["external_calls"] == 0
    with Store(cfg.storage.database, readonly=True) as store:
        assert len(store.rows("SELECT * FROM analysis_runs")) == 1
        assert len(store.rows("SELECT * FROM exports")) == 1


def test_retry_new_model_does_not_change_shared_analysis_config(cfg):
    chosen = ids(cfg)[:2]

    class Provider:
        def analyze(self, request):
            if request.paper_id == chosen[0]:
                raise ProviderError("schema", "合成JSON不適合")
            result = FakeProvider().analyze(request)
            return AnalysisResult(result.data, "agy", "new-synthetic-model")

    job = recovery.prepare_job(
        cfg, chosen, "reanalyze", provider="fake", model="new-synthetic-model"
    )
    original = cfg.analysis.model_dump()
    result = recovery.execute(cfg, job, provider=Provider())
    assert [i["status"] for i in result["items"]] == ["failed", "saved"]
    assert cfg.analysis.model_dump() == original


def test_authentication_error_stops_following_papers(cfg):
    chosen = ids(cfg)[:3]

    class Fail:
        def analyze(self, request):
            raise ProviderError("authentication", "ログインが必要")

    job = recovery.prepare_job(cfg, chosen, "reanalyze")
    result = recovery.execute(cfg, job, provider=Fail())
    assert result["status"] == "blocked"
    assert [i["status"] for i in result["items"]] == ["failed", "pending", "pending"]
    with Store(cfg.storage.database, readonly=True) as store:
        assert len(store.rows("SELECT * FROM analysis_runs")) == 1


def test_saved_job_does_not_fallback_to_ai_and_protects_registered(cfg, monkeypatch):
    chosen = ids(cfg)[:1]
    job = recovery.prepare_job(cfg, chosen, "save")
    monkeypatch.setattr(Pipeline, "_provider", forbid)
    assert recovery.execute(cfg, job)["items"][0]["status"] == "needs_analysis"
    monkeypatch.undo()
    Pipeline(cfg).run(paper_ids=chosen)
    with pytest.raises(SettingsError, match="取り込み済み"):
        recovery.prepare_job(cfg, chosen, "reanalyze")


def test_changed_pdf_is_not_sent_and_model_changes_require_new_plan(cfg, monkeypatch):
    chosen = ids(cfg)[:1]
    job = recovery.prepare_job(cfg, chosen, "reanalyze")
    with Store(cfg.storage.database, readonly=True) as store:
        match = json.loads(store.paper(chosen[0])["match_json"])
    Path(match["path"]).write_bytes(b"changed fixture")
    monkeypatch.setattr(Pipeline, "_provider", forbid)
    assert recovery.execute(cfg, job)["items"][0]["status"] == "failed"
    cfg.analysis.max_chars += 1
    with pytest.raises(SettingsError, match="解析設定"):
        recovery.execute(cfg, job)


def artifact(cfg, folder, identifier):
    from test_vision import visual_data

    from paperflow.agents.base import AnalysisRequest, PDFDocument
    from paperflow.wiki.bundle import sha

    with Store(cfg.storage.database, readonly=True) as store:
        p = store.paper(identifier)
    pdf = Path(json.loads(p["match_json"])["path"])
    request = AnalysisRequest(
        identifier,
        {},
        "合成本文",
        "fulltext",
        [1],
        cfg.taxonomy(),
        document=PDFDocument(pdf, sha(pdf), 1),
    )
    folder.mkdir()
    records = {
        "manifest.json": dict(
            paper_id=identifier,
            title=p["title"],
            pdf_sha256=sha(pdf),
            pages=1,
            provider="claude",
            model="synthetic-model",
        ),
        "result.json": dict(
            status="success",
            model="synthetic-model",
            usage=dict(input_tokens=123, output_tokens=45, total_tokens=168),
            elapsed_seconds=1.25,
        ),
        "input-audit.json": dict(source_pdf_sha256=sha(pdf), model="synthetic-model"),
        "verification.json": dict(status="success", source_unchanged=True, original_pdf_pages=1),
        "analysis.json": visual_data(request),
    }
    for name, data in records.items():
        (folder / name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return folder


def test_saved_trial_result_imports_without_ai_and_usage_is_not_new_spending(
    cfg, tmp_path, monkeypatch
):
    from paperflow.batches import usage_report
    from paperflow.saved_result import prepare_import

    chosen = ids(cfg)[0]
    folder = artifact(cfg, tmp_path / "trial", chosen)
    original = tree(folder)
    job = prepare_import(cfg, folder)
    monkeypatch.setattr(Pipeline, "_provider", forbid)
    result = recovery.execute(cfg, job)
    assert result["status"] == "complete", result["items"]
    assert result["items"][0]["external_calls"] == 0
    assert tree(folder) == original
    with Store(cfg.storage.database, readonly=True) as store:
        assert store.paper(chosen)["state"] == "exported"
        run = store.rows("SELECT * FROM analysis_runs")[0]
        assert run["model"] == "synthetic-model" and run["provider"] == "claude"
        assert (
            json.loads(store.rows("SELECT * FROM analysis_usage")[0]["usage_json"])["total_tokens"]
            == 168
        )
    usage = usage_report(cfg, dict(baseline_runs=[], paper_ids=[chosen]))
    assert usage["external_calls"] == 0
    assert usage["reported"]["total_tokens"] == 0


def test_modified_trial_or_incomplete_visual_reading_is_never_adopted(cfg, tmp_path, monkeypatch):
    from paperflow.saved_result import prepare_import

    chosen = ids(cfg)[0]
    folder = artifact(cfg, tmp_path / "trial", chosen)
    job = prepare_import(cfg, folder)
    analysis = json.loads((folder / "analysis.json").read_text(encoding="utf-8"))
    analysis["visual_review"]["pages_viewed"] = []
    (folder / "analysis.json").write_text(json.dumps(analysis), encoding="utf-8")
    monkeypatch.setattr(Pipeline, "_provider", forbid)
    before = tree(cfg.destination.vault_dir)
    result = recovery.execute(cfg, job)
    assert result["items"][0]["status"] == "failed"
    assert tree(cfg.destination.vault_dir) == before
    second = prepare_import(cfg, folder)
    assert recovery.execute(cfg, second)["items"][0]["status"] == "failed"
    with Store(cfg.storage.database, readonly=True) as store:
        assert store.rows("SELECT * FROM analysis_runs") == []


def test_import_save_failure_is_resumable_without_duplicate_result(cfg, tmp_path, monkeypatch):
    from paperflow.saved_result import prepare_import

    chosen = ids(cfg)[0]
    folder = artifact(cfg, tmp_path / "trial", chosen)
    job = prepare_import(cfg, folder)
    save = recovery._save
    monkeypatch.setattr(
        recovery, "_save", lambda *a: (_ for _ in ()).throw(SettingsError("合成の保存失敗"))
    )
    assert recovery.execute(cfg, job)["status"] == "partial"
    assert recovery.overview(cfg)["counts"]["ready"] == 1
    monkeypatch.setattr(recovery, "_save", save)
    monkeypatch.setattr(Pipeline, "_provider", forbid)
    assert recovery.execute(cfg, recovery.load_job(cfg, job["job_id"]))["status"] == "complete"
    with Store(cfg.storage.database, readonly=True) as store:
        assert len(store.rows("SELECT * FROM analysis_runs")) == 1


def test_partial_queue_does_not_hide_successes_after_local_validation_failure(cfg, monkeypatch):
    chosen = ids(cfg)[:2]
    Pipeline(cfg).run(paper_ids=[chosen[1]], export=False)
    monkeypatch.setattr(Pipeline, "_provider", forbid)
    job = recovery.prepare_job(cfg, chosen, "save")
    result = recovery.execute(cfg, job)
    assert [i["status"] for i in result["items"]] == ["needs_analysis", "saved"]


def test_local_save_discovers_successful_trial(cfg, monkeypatch):
    chosen = ids(cfg)[0]

    class Fail:
        def analyze(self, request):
            raise ProviderError("visual_reading", "合成の過去失敗")

    Pipeline(cfg, Fail()).run(paper_ids=[chosen], export=False)
    parent = cfg.storage.data_dir / "pdf-delivery-retrial"
    parent.mkdir()
    artifact(cfg, parent / "synthetic", chosen)
    assert recovery.overview(cfg)["counts"]["artifact"] == 1
    job = recovery.prepare_job(cfg, [chosen], "save")
    assert job["items"][0].get("artifact")
    monkeypatch.setattr(Pipeline, "_provider", forbid)
    result = recovery.execute(cfg, job)
    assert result["status"] == "complete", result["items"]
    assert result["items"][0]["external_calls"] == 0


def test_revalidated_alias_is_ready_in_library(cfg):
    from paperflow.library import snapshot

    chosen = ids(cfg)[0]
    Pipeline(cfg).run(paper_ids=[chosen], export=False)
    with Store(cfg.storage.database) as store, store.transaction():
        run = store.rows("SELECT run_id,source_hash FROM analysis_runs")[0]
        store.db.execute(
            "INSERT OR IGNORE INTO analysis_aliases VALUES (?,?)",
            (run["run_id"], run["source_hash"]),
        )
        store.db.execute(
            "UPDATE analysis_runs SET source_hash=? WHERE run_id=?", ("0" * 64, run["run_id"])
        )
    assert snapshot(cfg)["counts"]["ready"] == 1
    assert recovery.overview(cfg)["counts"]["ready"] == 1
