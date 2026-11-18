import json
import sqlite3
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner
from conftest import tree
from create_demo import make_demo
from test_wiki import CONTROL

from paperflow.agents.base import AnalysisResult, ProviderError
from paperflow.agents.fake import FakeProvider
from paperflow.agents.usage import claude_usage, extract, normalize
from paperflow.batches import Registry, execute, load, plan, report
from paperflow.cli import app
from paperflow.config import Destination, SettingsError, load_config
from paperflow.obsidian.writer import WriteConflict
from paperflow.pipeline import Pipeline
from paperflow.store import Store


@pytest.fixture
def batch_config(tmp_path):
    cfg = load_config(make_demo(tmp_path / "f", count=13))
    cfg.destination = Destination(
        vault_dir=cfg.destination.vault_dir,
        layout="llm_wiki",
        copy_pdf=True,
        allow_vault_write=True,
        include_visuals=False,
    )
    root = cfg.destination.vault_dir
    for folder in [
        "scripts",
        "00_System/Index",
        "00_System/Locks",
        "10_Papers/20_Source_Notes",
        "50_Wiki/Concepts",
    ]:
        (root / folder).mkdir(parents=True, exist_ok=True)
    (root / "00_System/PaperFlow-Integration.md").write_text(
        "合成フレームワーク\n", encoding="utf-8"
    )
    (root / "00_System/Index/log.md").write_text("既存ログ\n", encoding="utf-8")
    for script in [
        "append_log.py",
        "update_index.py",
        "validate_frontmatter.py",
        "validate_obsidian_markup.py",
        "resolve_links.py",
        "check_graph_integrity.py",
    ]:
        (root / "scripts" / script).write_text(CONTROL, encoding="utf-8")
    cfg.sync.max_papers_per_run = 2
    Pipeline(cfg).run(scan_only=True)
    return cfg


def test_random_count_total_and_no_overlap(batch_config):
    cfg = batch_config
    first = plan(cfg, count=3, seed=17)
    first_ids = {p["paper_id"] for p in first["items"]}
    assert len(first_ids) == 3 and first["seed"] == 17
    original = tree(cfg.source.paperpile_dir)
    done = execute(cfg, Path(first["plan"]))
    assert done["registered"] == 3 and done["status"] == "complete"
    assert done["usage"]["external_calls"] == 0
    before = tree(cfg.destination.vault_dir)
    assert execute(cfg, Path(first["plan"]))["usage"]["analysis_runs"] == 3
    assert tree(cfg.destination.vault_dir) == before
    second = plan(cfg, count=5, seed=29)
    assert len(second["items"]) == 5 and not first_ids & {p["paper_id"] for p in second["items"]}
    total = plan(cfg, total=7, seed=31)
    assert total["selected_count"] == 4 and total["registered_before"] == 3
    assert execute(cfg, Path(total["plan"]))["registered"] == 4
    assert len(Registry(cfg).hashes) == 7
    assert tree(cfg.source.paperpile_dir) == original
    assert plan(cfg, total=4)["selected_count"] == 0
    short = plan(cfg, count=99)
    assert short["selected_count"] == 6 and short["shortage"] == 93


def test_dry_runs_have_no_files_db_or_provider_calls(batch_config, monkeypatch):
    cfg = batch_config
    before = tree(cfg.source.paperpile_dir.parent)

    def forbidden(*a, **k):
        raise AssertionError("外部CLIを呼びました")

    monkeypatch.setattr("subprocess.Popen", forbidden)
    data = plan(cfg, count=4, seed=1, dry_run=True)
    assert data["selected_count"] == 4 and data["plan"] is None
    assert tree(cfg.source.paperpile_dir.parent) == before
    monkeypatch.undo()
    saved = plan(cfg, count=4, seed=1)
    before = tree(cfg.source.paperpile_dir.parent)
    assert execute(cfg, Path(saved["plan"]), dry_run=True)["remaining"] == 4
    assert tree(cfg.source.paperpile_dir.parent) == before


def test_external_vault_import_excluded_even_with_other_database_ids(batch_config):
    cfg = batch_config
    old = plan(cfg, count=1)["items"][0]
    slug = "manual-" + old["pdf_sha256"][:20]
    sid = "paper:" + slug
    root = cfg.destination.vault_dir
    raw = root / "10_Papers/10_Sources" / slug
    raw.mkdir(parents=True)
    # 登録済み来歴だけ読む。過去PDFの全再ハッシュや本文読み取りは不要。
    (raw / "manifest.yml").write_text(
        yaml.safe_dump(
            dict(
                source_id=sid,
                source_status="frozen",
                sha256=old["pdf_sha256"],
                doi=old["doi"],
                title=old["title"],
                paperflow_id="another-database-id",
            )
        ),
        encoding="utf-8",
    )
    (root / "10_Papers/20_Source_Notes/manual.md").write_text(
        "---\n"
        + yaml.safe_dump(dict(id=sid, source_id=sid, title=old["title"]))
        + "---\n# 合成済みノート\n",
        encoding="utf-8",
    )
    result = plan(cfg, count=99)
    assert result["registered_before"] == 1
    assert old["paper_id"] not in {p["paper_id"] for p in result["items"]}


@pytest.mark.parametrize("change", ["pdf", "bib"])
def test_changed_fixed_input_never_calls_provider(batch_config, change):
    cfg = batch_config
    saved = plan(cfg, count=1)
    item = saved["items"][0]
    if change == "pdf":
        p = Path(item["pdf"])
        p.write_bytes(p.read_bytes() + b"\n% changed\n")
        Pipeline(cfg).run(scan_only=True)
    else:
        p = cfg.source.bibtex_file
        p.write_text(p.read_text().replace(item["title"], item["title"] + " updated"))

    class Forbidden:
        def analyze(self, request):
            raise AssertionError("変更された資料を送信しました")

    result = execute(cfg, Path(saved["plan"]), provider=Forbidden())
    assert result["status"] == "partial"
    assert result["usage"]["analysis_runs"] == 0
    assert result["remaining"] == 1


def test_partial_batch_resumes_same_targets_without_resending_success(batch_config):
    cfg = batch_config
    saved = plan(cfg, count=3, seed=6)
    seen = []

    class Sometimes:
        def analyze(self, request):
            seen.append(request.paper_id)
            if len(seen) == 2:
                raise ProviderError("synthetic_failure", "合成一時障害", True)
            return FakeProvider().analyze(request)

    first = execute(cfg, Path(saved["plan"]), provider=Sometimes())
    assert first["status"] == "partial" and first["registered"] == 2
    second = execute(cfg, Path(saved["plan"]), provider=Sometimes())
    assert second["status"] == "complete" and second["registered"] == 3
    assert len(seen) == 4 and seen.count(seen[1]) == 2
    assert {p["paper_id"] for p in saved["items"]} == set(seen)
    cfg.analysis.model = "different-model"
    with pytest.raises(WriteConflict, match="前回と違い"):
        execute(cfg, Path(saved["plan"]))


def test_total_does_not_overshoot_if_another_batch_imported(batch_config):
    cfg = batch_config
    goal = plan(cfg, total=5, seed=2)
    other = plan(cfg, count=3, seed=9)
    execute(cfg, Path(other["plan"]))
    result = execute(cfg, Path(goal["plan"]))
    assert len(Registry(cfg).hashes) == 5 and result["status"] == "complete"


def test_usage_is_persisted_and_cache_has_no_new_cost(batch_config):
    cfg = batch_config
    saved = plan(cfg, count=2)

    class Measured:
        def analyze(self, request):
            result = FakeProvider().analyze(request)
            return AnalysisResult(
                result.data,
                "claude",
                "synthetic-model",
                elapsed_seconds=1.5,
                usage=normalize(
                    "claude",
                    dict(
                        input_tokens=10,
                        cache_read_input_tokens=5,
                        cache_creation_input_tokens=2,
                        output_tokens=3,
                    ),
                    0.02,
                ),
            )

    # 実通信をしない合成Provider。外部名のrun記録だけを合成する。
    cfg.analysis.default_provider = "claude"
    cfg.analysis.policy = "fulltext_external"
    cfg.analysis.allow_external_fulltext = True
    cfg.analysis.require_visual_reading = False
    with pytest.raises(SettingsError, match="視覚読解"):
        execute(cfg, Path(saved["plan"]), provider=Measured())
    cfg.analysis.default_provider = "fake"
    execute(cfg, Path(saved["plan"]), provider=Measured())
    with Store(cfg.storage.database) as store:
        store.db.execute("UPDATE analysis_runs SET provider='claude'")
        store.db.commit()
    result = report(cfg, Path(saved["plan"]))
    assert result["usage"]["reported"]["input_tokens"] == 34
    assert result["usage"]["reported"]["total_tokens"] == 40
    assert result["usage"]["reported"]["cost_usd"] == 0.04
    again = execute(cfg, Path(saved["plan"]), provider=Measured())
    assert again["usage"]["analysis_runs"] == 2


def test_plan_binds_database_and_cannot_write_sources(batch_config, tmp_path):
    cfg = batch_config
    saved = plan(cfg, count=2)
    with pytest.raises(SettingsError):
        plan(cfg, count=1, total=2)
    with pytest.raises(SettingsError):
        plan(cfg, count=1, output=cfg.source.paperpile_dir / "plan.json")
    with pytest.raises(SettingsError):
        plan(cfg, count=1, output=cfg.destination.vault_dir / "plan.json")
    cfg.storage.database = tmp_path / "other.db"
    with pytest.raises(SettingsError, match="別のVault"):
        load(cfg, Path(saved["plan"]))


def test_cli_arbitrary_count_and_total(batch_config):
    cfg = batch_config
    path = cfg.source.paperpile_dir.parent / "config.yaml"
    data = cfg.model_dump(mode="json")
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(
        app, ["--config", str(path), "batch-plan", "--count", "4", "--seed", "43"]
    )
    assert result.exit_code == 0, (result.output, result.exception)
    saved = json.loads(result.output)
    result = runner.invoke(
        app,
        [
            "--config",
            str(path),
            "batch-run",
            "--plan",
            saved["plan"],
            "--provider",
            "fake",
            "--no-interactive",
        ],
    )
    assert result.exit_code == 0, (result.output, result.exception)
    result = runner.invoke(
        app, ["--config", str(path), "--json", "batch-report", "--plan", saved["plan"]]
    )
    assert result.exit_code == 0 and json.loads(result.output)["registered"] == 4


def test_usage_parses_all_claude_models_without_double_counting():
    usage = claude_usage(
        dict(
            usage=dict(input_tokens=999, output_tokens=999),
            total_cost_usd=0.04,
            modelUsage={
                "main": dict(
                    inputTokens=10,
                    outputTokens=4,
                    cacheReadInputTokens=2,
                    cacheCreationInputTokens=3,
                ),
                "helper": dict(
                    inputTokens=5,
                    outputTokens=1,
                    cacheReadInputTokens=0,
                    cacheCreationInputTokens=0,
                ),
            },
        )
    )
    assert usage["input_tokens"] == 20 and usage["total_tokens"] == 25 and usage["cost_usd"] == 0.04
    raw = "\n".join(
        json.dumps(
            dict(
                type="turn.completed",
                usage=dict(input_tokens=10, cached_input_tokens=3, output_tokens=4),
            )
        )
        for _ in range(2)
    )
    codex = extract("codex", raw)
    assert (
        codex["total_tokens"] == 28
        and codex["cached_input_tokens"] == 6
        and codex["cost_usd"] is None
    )
    assert extract("codex", "not a usage event")["total_tokens"] is None


def test_old_database_migration_is_readonly_until_write(tmp_path):
    db = tmp_path / "old.db"
    with Store(db):
        pass
    con = sqlite3.connect(db)
    con.execute("DROP TABLE analysis_usage")
    con.execute("PRAGMA user_version=1")
    con.commit()
    con.close()
    before = db.read_bytes()
    with Store(db, readonly=True) as store:
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == 1
    assert db.read_bytes() == before
    with Store(db, snapshot=True) as store:
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == 4
    assert db.read_bytes() == before
    with Store(db) as store:
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == 4


def test_all_selects_remaining_and_runs_across_chunks(batch_config):
    cfg = batch_config
    first = plan(cfg, count=3)
    execute(cfg, Path(first["plan"]))
    all_plan = plan(cfg, all_papers=True)
    assert all_plan["mode"] == "all" and all_plan["requested"] is None
    assert all_plan["selected_count"] == 10 and all_plan["shortage"] == 0
    assert not {p["paper_id"] for p in first["items"]} & {p["paper_id"] for p in all_plan["items"]}
    done = execute(cfg, Path(all_plan["plan"]))
    assert done["status"] == "complete" and done["registered"] == 10
    assert len(Registry(cfg).hashes) == 13
    assert plan(cfg, all_papers=True)["selected_count"] == 0
    for options in [dict(all_papers=True, count=1), dict(all_papers=True, total=4), {}]:
        with pytest.raises(SettingsError):
            plan(cfg, **options)


def test_all_dry_run_and_cli(batch_config):
    cfg = batch_config
    before = tree(cfg.source.paperpile_dir.parent)
    result = plan(cfg, all_papers=True, dry_run=True)
    assert result["selected_count"] == 13 and tree(cfg.source.paperpile_dir.parent) == before
    path = cfg.source.paperpile_dir.parent / "config.yaml"
    path.write_text(
        yaml.safe_dump(cfg.model_dump(mode="json"), allow_unicode=True), encoding="utf-8"
    )
    output = CliRunner().invoke(app, ["--config", str(path), "batch-plan", "--all", "--dry-run"])
    assert output.exit_code == 0, (output.output, output.exception)
    assert json.loads(output.output)["mode"] == "all"


def test_removed_from_bib_keeps_vault_and_restored_reference_is_not_reimported(batch_config):
    cfg = batch_config
    saved = plan(cfg, count=1)
    item = saved["items"][0]
    execute(cfg, Path(saved["plan"]))
    before = tree(cfg.destination.vault_dir)
    bib = cfg.source.bibtex_file
    original = bib.read_text()
    # 完全に空のエクスポートも削除反映として扱われることを確認。
    bib.write_text("", encoding="utf-8")
    Pipeline(cfg).run(scan_only=True)
    with Store(cfg.storage.database, readonly=True) as store:
        assert not store.paper(item["paper_id"])["source_present"]
        assert store.rows("SELECT COUNT(*) AS n FROM analysis_runs")[0]["n"] == 1
    assert tree(cfg.destination.vault_dir) == before
    assert plan(cfg, all_papers=True)["selected_count"] == 0
    bib.write_text(original, encoding="utf-8")
    restored = plan(cfg, all_papers=True)
    assert item["paper_id"] not in {p["paper_id"] for p in restored["items"]}
    assert tree(cfg.destination.vault_dir) == before


def test_removed_pdf_keeps_export_and_is_not_selected(batch_config):
    cfg = batch_config
    saved = plan(cfg, count=1)
    item = saved["items"][0]
    execute(cfg, Path(saved["plan"]))
    before = tree(cfg.destination.vault_dir)
    Path(item["pdf"]).unlink()
    Pipeline(cfg).run(scan_only=True)
    with Store(cfg.storage.database, readonly=True) as store:
        paper = store.paper(item["paper_id"])
        assert paper["source_present"] == 1
        match = json.loads(paper["match_json"])
        assert match["status"] in {"metadata_only", "needs_review"} and match["path"] is None
    assert tree(cfg.destination.vault_dir) == before
    assert item["paper_id"] not in {p["paper_id"] for p in plan(cfg, all_papers=True)["items"]}


def test_reference_removed_after_plan_never_sent(batch_config):
    cfg = batch_config
    saved = plan(cfg, count=1)
    cfg.source.bibtex_file.write_text("", encoding="utf-8")

    class Forbidden:
        def analyze(self, request):
            raise AssertionError("削除済みの論文を送信しました")

    result = execute(cfg, Path(saved["plan"]), provider=Forbidden())
    assert result["status"] == "partial" and result["usage"]["analysis_runs"] == 0
    progress = json.loads(Path(result["progress"]).read_text())
    assert progress["failures"][0]["category"] == "missing_from_source"
