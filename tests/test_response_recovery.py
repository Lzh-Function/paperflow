import json
from copy import deepcopy

import pytest
from conftest import tree
from test_vision import visual_data

from paperflow.agents.base import ProviderError
from paperflow.agents.json_response import parse_response, response_error
from paperflow.config import SettingsError
from paperflow.models import digest
from paperflow.pipeline import Pipeline
from paperflow.portable import (
    LEGACY_VISUAL_CONTRACT,
    compatible_profiles,
    profile,
    profiles_compatible,
)
from paperflow.response_recovery import revalidate_selected
from paperflow.store import Store


class RejectedProvider:
    def analyze(self, request):
        data = visual_data(request)
        selection = data["visual_review"]["selected_visuals"][0]
        selection.update(number=None, title_ja="Figure 4.4: 合成結果")
        data["visual_review"]["selected_visuals"].append(
            dict(selection, title_ja="Figure 4.5: 別の合成結果")
        )
        error = ProviderError("visual_reading", "図表のページ・番号・関連所見が不正です")
        error.rejected_response = data
        error.input_receipt = dict(
            provider="agy",
            model="synthetic-model",
            pdf_sha256=request.document.sha256,
            page_count=request.document.page_count,
            pdf_read_completed=True,
        )
        error.usage = dict(input_tokens=123, output_tokens=45, total_tokens=168)
        error.elapsed_seconds = 2.5
        raise error


class RawRejectedProvider:
    def analyze(self, request):
        data = visual_data(request)
        data["approach_ja"] = r"合成の立体記号（@、@@、/、\等）"
        raw = json.dumps(data, ensure_ascii=False).replace(r"\\等", r"\等")
        try:
            parse_response(raw)
        except ValueError as cause:
            error = response_error(
                "合成のJSON書式エラー",
                cause,
                raw=raw,
                receipt=dict(
                    provider="agy",
                    model="synthetic-model",
                    pdf_sha256=request.document.sha256,
                    page_count=request.document.page_count,
                    pdf_read_completed=True,
                ),
            )
            error.usage = dict(input_tokens=123, output_tokens=45, total_tokens=168)
            raise error from cause


@pytest.fixture
def raw_config(small_config):
    cfg = small_config
    cfg.analysis.default_provider = "agy"
    cfg.analysis.model = "synthetic-model"
    cfg.analysis.policy = "fulltext_external"
    cfg.analysis.allow_external_fulltext = True
    cfg.analysis.require_visual_reading = True
    Pipeline(cfg).run(scan_only=True)
    result = Pipeline(cfg, RawRejectedProvider()).run(export=False)
    assert result["failures"][0]["category"] == "invalid_json"
    return cfg


def test_selected_raw_json_revalidation_retains_failure_and_usage_without_ai(
    raw_config, monkeypatch
):
    cfg = raw_config
    with Store(cfg.storage.database, readonly=True) as store:
        run = store.rows("SELECT * FROM analysis_runs")[0]
        usage = store.rows("SELECT * FROM analysis_usage")
    diagnostic = cfg.storage.data_dir / "diagnostics" / (run["run_id"] + ".rejected.json")
    before = diagnostic.read_bytes()
    vault = tree(cfg.destination.vault_dir)
    monkeypatch.setattr(Pipeline, "_provider", lambda *a: pytest.fail("AIを呼びました"))
    revalidate_selected(cfg, [run["paper_id"]])
    assert diagnostic.read_bytes() == before and tree(cfg.destination.vault_dir) == vault
    audit = json.loads(diagnostic.with_name(run["run_id"] + ".revalidated.json").read_text())
    assert audit["response_format"] == "raw-json" and audit["external_calls"] == 0
    assert audit["original_category"] == "invalid_json"
    with Store(cfg.storage.database, readonly=True) as store:
        updated = store.rows("SELECT * FROM analysis_runs")[0]
        assert updated["status"] == "success" and updated["run_id"] == run["run_id"]
        data = json.loads(updated["output_json"])
        assert data["approach_ja"] == r"合成の立体記号（@、@@、/、\等）"
        assert "形式補正" in data["uncertainties_ja"][-1]
        assert store.rows("SELECT * FROM analysis_usage") == usage
        assert store.rows("SELECT * FROM exports") == []


@pytest.mark.parametrize("problem", ["receipt", "missing-raw", "structure", "pages", "model"])
def test_raw_json_revalidation_rejects_incomplete_or_changed_input(
    raw_config, monkeypatch, problem
):
    cfg = raw_config
    with Store(cfg.storage.database, readonly=True) as store:
        run = store.rows("SELECT * FROM analysis_runs")[0]
    diagnostic = cfg.storage.data_dir / "diagnostics" / (run["run_id"] + ".rejected.json")
    record = json.loads(diagnostic.read_text(encoding="utf-8"))
    if problem == "receipt":
        record["input_receipt"]["pdf_read_completed"] = False
    elif problem == "missing-raw":
        record.pop("raw_response")
    elif problem == "structure":
        record["raw_response"] += "{}"
    elif problem == "pages":
        record["input_receipt"]["page_count"] += 1
    else:
        cfg.analysis.model = "changed-model"
    diagnostic.write_text(json.dumps(record), encoding="utf-8")
    monkeypatch.setattr(Pipeline, "_provider", lambda *a: pytest.fail("AIを呼びました"))
    with pytest.raises((SettingsError, ValueError, ProviderError)):
        revalidate_selected(cfg, [run["paper_id"]])
    with Store(cfg.storage.database, readonly=True) as store:
        assert store.rows("SELECT status FROM analysis_runs")[0]["status"] == "failed"
        assert store.rows("SELECT * FROM exports") == []


@pytest.fixture
def rejected_config(small_config):
    cfg = small_config
    cfg.analysis.default_provider = "agy"
    cfg.analysis.model = "synthetic-model"
    cfg.analysis.policy = "fulltext_external"
    cfg.analysis.allow_external_fulltext = True
    cfg.analysis.require_visual_reading = True
    Pipeline(cfg).run(scan_only=True)
    result = Pipeline(cfg, RejectedProvider()).run(export=False)
    assert result["failures"][0]["category"] == "visual_reading"
    return cfg


def test_selected_revalidation_has_no_ai_or_vault_and_preserves_usage(rejected_config, monkeypatch):
    cfg = rejected_config
    with Store(cfg.storage.database, readonly=True) as store:
        run = store.rows("SELECT * FROM analysis_runs")[0]
        usage = store.rows("SELECT * FROM analysis_usage")
    original = tree(cfg.source.paperpile_dir)
    vault = tree(cfg.destination.vault_dir)
    diagnostic = cfg.storage.data_dir / "diagnostics" / (run["run_id"] + ".rejected.json")
    rejected = diagnostic.read_bytes()

    def forbidden(*args, **kwargs):
        raise AssertionError("AIを呼びました")

    monkeypatch.setattr(Pipeline, "_provider", forbidden)
    result = revalidate_selected(cfg, [run["paper_id"]])
    assert result[0]["external_calls"] == 0
    assert diagnostic.read_bytes() == rejected
    assert tree(cfg.source.paperpile_dir) == original
    assert tree(cfg.destination.vault_dir) == vault
    with Store(cfg.storage.database, readonly=True) as store:
        assert store.paper(run["paper_id"])["state"] == "analyzed"
        assert store.rows("SELECT * FROM analysis_usage") == usage
        updated = store.rows("SELECT * FROM analysis_runs")[0]
        assert updated["status"] == "success" and updated["run_id"] == run["run_id"]
        data = json.loads(updated["output_json"])
        assert [v["number"] for v in data["visual_review"]["selected_visuals"]] == ["4.4", "4.5"]
        assert store.rows("SELECT * FROM exports") == []
    assert Pipeline(cfg).run(export=False)["analyzed"] == 0


def test_gui_revalidation_requires_confirmation_and_uses_selected_model(monkeypatch):
    from types import SimpleNamespace as NS

    from paperflow.gui import PaperFlowApp

    calls = []
    status = []
    app = NS(
        root=None,
        library_tree=NS(selection=lambda: ("synthetic-id",)),
        provider_code=lambda: "agy",
        model_code=lambda: "synthetic-model",
        service=NS(
            revalidate_responses=lambda *args: calls.append(args) or [dict(status="analyzed")]
        ),
        status=NS(set=status.append),
        refresh_library=lambda: calls.append("refresh"),
        submit=lambda action, job, done: done(job()),
    )
    monkeypatch.setattr("paperflow.gui.messagebox.askyesno", lambda *a, **k: False)
    PaperFlowApp.revalidate_responses(app)
    assert calls == []
    monkeypatch.setattr("paperflow.gui.messagebox.askyesno", lambda *a, **k: True)
    PaperFlowApp.revalidate_responses(app)
    assert calls == [(["synthetic-id"], "agy", "synthetic-model"), "refresh"]
    assert "1本" in status[-1]


@pytest.mark.parametrize("problem", ["model", "pdf", "receipt", "missing-page", "registered"])
def test_revalidation_refuses_unverified_input_without_ai(rejected_config, monkeypatch, problem):
    cfg = rejected_config
    with Store(cfg.storage.database, readonly=True) as store:
        run = store.rows("SELECT * FROM analysis_runs")[0]
    diagnostic = cfg.storage.data_dir / "diagnostics" / (run["run_id"] + ".rejected.json")
    record = json.loads(diagnostic.read_text(encoding="utf-8"))
    if problem == "model":
        cfg.analysis.model = "another-model"
    elif problem == "pdf":
        next(cfg.source.paperpile_dir.glob("*.pdf")).write_bytes(b"modified fixture")
    elif problem == "receipt":
        record["input_receipt"]["pdf_read_completed"] = False
    elif problem == "missing-page":
        record["response"]["visual_review"]["pages_viewed"] = []
    else:
        with Store(cfg.storage.database) as store, store.transaction():
            store.db.execute(
                "INSERT INTO exports (paper_id,export_kind) VALUES (?,?)",
                (run["paper_id"], "source"),
            )
    diagnostic.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")

    def forbidden(*args, **kwargs):
        raise AssertionError("AIを呼びました")

    monkeypatch.setattr(Pipeline, "_provider", forbidden)
    with pytest.raises((SettingsError, ProviderError)):
        revalidate_selected(cfg, [run["paper_id"]])
    with Store(cfg.storage.database, readonly=True) as store:
        assert store.rows("SELECT status FROM analysis_runs")[0]["status"] == "failed"


def test_contract_compatibility_does_not_accept_model_pdf_policy_or_vocabulary_changes(
    small_config,
):
    current = profile(small_config)
    old = dict(current, **LEGACY_VISUAL_CONTRACT)
    assert profiles_compatible(old, current)
    assert digest(old) in {digest(p) for p in compatible_profiles(current)}
    for key in ["model", "provider", "taxonomy", "parser", "visuals"]:
        bad = deepcopy(old)
        bad[key] = "different"
        assert not profiles_compatible(bad, current)
    bad = deepcopy(old)
    bad["analysis"]["max_chars"] += 1
    assert not profiles_compatible(bad, current)

    future = dict(current, schema="0" * 64)
    assert not profiles_compatible(old, future)
    assert compatible_profiles(future) == [future]


def test_legacy_success_cache_survives_contract_upgrade(rejected_config, monkeypatch):
    cfg = rejected_config
    with Store(cfg.storage.database, readonly=True) as store:
        paper_id = store.rows("SELECT paper_id FROM papers")[0]["paper_id"]
    revalidate_selected(cfg, [paper_id])
    with Store(cfg.storage.database) as store, store.transaction():
        cached = store.rows("SELECT * FROM portable_analyses")[0]
        identity = json.loads(cached["input_json"])
        identity["profile"].update(LEGACY_VISUAL_CONTRACT)
        store.db.execute(
            "UPDATE portable_analyses SET cache_key=?,input_json=?",
            (digest(identity), json.dumps(identity)),
        )
        store.db.execute("DELETE FROM analysis_aliases")
        store.db.execute("UPDATE analysis_runs SET source_hash=?", ("0" * 64,))

    def forbidden(*args, **kwargs):
        raise AssertionError("互換契約の保存済み回答を再送しました")

    monkeypatch.setattr(Pipeline, "_provider", forbidden)
    assert Pipeline(cfg).run(export=False)["analyzed"] == 0
