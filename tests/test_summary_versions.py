import hashlib
import json
import shutil
from pathlib import Path

import pytest
from conftest import tree
from test_vision import visual_data
from test_wiki import wiki_config as wiki_fixture

from paperflow.agents.base import AnalysisResult, ProviderError
from paperflow.batches import plan
from paperflow.config import SettingsError
from paperflow.library import snapshot
from paperflow.obsidian.writer import WriteConflict
from paperflow.store import Store
from paperflow.summary_versions import adopt, comparison, generate, history, recover
from paperflow.sync_snapshot import capture, materialize, packed, unpack
from paperflow.wiki.bundle import apply
from paperflow.wiki.export import cached_items, prepare, record_exports


@pytest.fixture
def registered(small_config):
    cfg = wiki_fixture.__wrapped__(small_config)
    items = cached_items(cfg)
    bundle = cfg.storage.data_dir / "initial"
    planned = prepare(cfg, items, bundle)
    apply(cfg, bundle)
    with Store(cfg.storage.database) as store:
        record_exports(cfg, store, planned)
    return cfg, items[0]["paper"]["paper_id"], items[0]["analysis"]["run_id"]


class VisualProvider:
    def __init__(self, model="別モデル", *, failure=False):
        self.model, self.calls, self.failure = model, [], failure

    def analyze(self, request):
        self.calls.append(request.paper_id)
        if self.failure:
            raise ProviderError("authentication", "合成認証エラー")
        data = visual_data(request)
        data["tldr_ja"] = f"{self.model}による別の合成要約です。"
        return AnalysisResult(
            data, "claude", self.model, usage=dict(input_tokens=123, output_tokens=45)
        )


def external(cfg):
    cfg.analysis.default_provider = "claude"
    cfg.analysis.model = "合成指定モデル"
    cfg.analysis.policy = "fulltext_external"
    cfg.analysis.allow_external_fulltext = True
    cfg.analysis.require_visual_reading = True


@pytest.mark.parametrize("backend", ["claude", "codex", "agy"])
def test_selected_reanalysis_preserves_active_note_state_figures_and_excludes_normal_batch(
    registered,
    backend,
):
    cfg, paper_id, original_id = registered
    external(cfg)
    cfg.analysis.default_provider = backend
    before_vault, before_source = tree(cfg.destination.vault_dir), tree(cfg.source.paperpile_dir)
    with Store(cfg.storage.database, readonly=True) as store:
        before_paper = store.paper(paper_id)
    provider = VisualProvider()
    result = generate(cfg, [paper_id], provider=provider)
    assert not result["failures"] and provider.calls == [paper_id]
    assert tree(cfg.destination.vault_dir) == before_vault
    assert tree(cfg.source.paperpile_dir) == before_source
    with Store(cfg.storage.database, readonly=True) as store:
        assert store.paper(paper_id) == before_paper
        assert len(store.rows("SELECT * FROM summary_versions")) == 2
    versions = history(cfg, paper_id)
    assert versions["current"] == original_id
    assert len(versions["versions"]) == 2
    candidate = result["completed"][0]["run_id"]
    row = next(v for v in versions["versions"] if v["run_id"] == candidate)
    assert row["model"] == "別モデル" and json.loads(row["usage_json"])["input_tokens"] == 123
    assert row["provider"] == backend
    assert candidate in row["note_text"] and "合成図の説明" in row["note_text"]
    assert snapshot(cfg)["rows"][0]["model"] == "offline-fixture"
    assert plan(cfg, all_papers=True)["selected_count"] == 0
    assert not list((cfg.storage.data_dir / "summary-versions").glob(".extract-*"))


def test_compare_adopt_and_restore_keep_versions_and_original_assets(registered):
    cfg, paper_id, original_id = registered
    external(cfg)
    original = history(cfg, paper_id)["versions"][0]["note_text"]
    old_images = {k: v for k, v in tree(cfg.destination.vault_dir).items() if k.endswith(".png")}
    candidate = generate(cfg, [paper_id], provider=VisualProvider())["completed"][0]["run_id"]
    preview = Path(comparison(cfg, paper_id, candidate))
    content = preview.read_text(encoding="utf-8")
    assert content.count('<img alt="論文の図表"') >= 2
    assert "現在の正式版" in content and "合成図の説明" in content
    assert "<script" not in content and "https://" not in content
    adopted = adopt(cfg, paper_id, candidate)
    assert adopted["written"] > 0
    assert history(cfg, paper_id)["current"] == candidate
    current = next(v for v in history(cfg, paper_id)["versions"] if v["run_id"] == candidate)
    note = cfg.destination.vault_dir / current["note_path"]
    assert note.read_text(encoding="utf-8") == current["note_text"]
    for asset in json.loads(current["visuals_json"])["assets"]:
        assert (cfg.destination.vault_dir / asset["path"]).is_file()
    for name, original_file in old_images.items():
        assert tree(cfg.destination.vault_dir)[name] == original_file
    assert snapshot(cfg)["rows"][0]["model"] == "別モデル"
    adopt(cfg, paper_id, original_id)
    assert note.read_text(encoding="utf-8") == original
    assert history(cfg, paper_id)["current"] == original_id
    assert len(history(cfg, paper_id)["versions"]) == 2


@pytest.mark.parametrize("reason", ["human", "integrated", "permission"])
def test_adoption_refuses_human_edits_integration_and_missing_permission(registered, reason):
    cfg, paper_id, _ = registered
    candidate = generate(cfg, [paper_id])["completed"][0]["run_id"]
    record = next(v for v in history(cfg, paper_id)["versions"] if v["run_id"] == candidate)
    if reason == "human":
        note = cfg.destination.vault_dir / record["note_path"]
        note.write_text(note.read_text(encoding="utf-8") + "\n人間の追記\n", encoding="utf-8")
    elif reason == "integrated":
        path = cfg.destination.vault_dir / f"00_System/PaperFlow/receipts/{paper_id}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["status"] = "integrated"
        path.write_text(json.dumps(data), encoding="utf-8")
    else:
        cfg.destination.allow_vault_write = False
    before = tree(cfg.destination.vault_dir)
    with pytest.raises((WriteConflict, SettingsError)):
        adopt(cfg, paper_id, candidate)
    assert tree(cfg.destination.vault_dir) == before


def test_failed_reanalysis_retains_formal_state_and_does_not_automatically_retry(registered):
    cfg, paper_id, original_id = registered
    external(cfg)
    provider = VisualProvider(failure=True)
    before = tree(cfg.destination.vault_dir)
    with Store(cfg.storage.database, readonly=True) as store:
        original_paper = store.paper(paper_id)
    result = generate(cfg, [paper_id], provider=provider)
    assert provider.calls == [paper_id] and len(result["failures"]) == 1
    assert tree(cfg.destination.vault_dir) == before
    with Store(cfg.storage.database, readonly=True) as store:
        assert store.paper(paper_id) == original_paper
    data = history(cfg, paper_id)
    assert data["current"] == original_id and len(data["versions"]) == 1
    assert data["failures"][0]["error_message"] == "合成認証エラー"


def test_version_sync_and_lazy_image_recovery_do_not_resend_pdf(registered, tmp_path, monkeypatch):
    cfg, paper_id, _ = registered
    external(cfg)
    candidate = generate(cfg, [paper_id], provider=VisualProvider())["completed"][0]["run_id"]
    data = capture(cfg)
    blob = packed(data)
    decoded = unpack(blob, hashlib.sha256(blob).hexdigest())
    other = cfg.model_copy(deep=True)
    other.destination.vault_dir = tmp_path / "other-vault"
    other.source.paperpile_dir = tmp_path / "other-source"
    other.storage.data_dir = tmp_path / "other-data"
    other.storage.database = other.storage.data_dir / "state.db"
    shutil.copytree(cfg.destination.vault_dir, other.destination.vault_dir)
    shutil.copytree(cfg.source.paperpile_dir, other.source.paperpile_dir)
    tables, _ = materialize(other, decoded)
    from paperflow.sync_snapshot import insert

    with Store(other.storage.database) as store:
        with store.transaction():
            insert(store.db, tables)
    monkeypatch.setattr(
        "paperflow.pipeline.Pipeline._provider", lambda *a: pytest.fail("再送しない")
    )
    assert len(history(other, paper_id)["versions"]) == 2
    assert Path(comparison(other, paper_id, candidate)).is_file()
    assert not list((other.storage.data_dir / "summary-versions").glob(".restore-*"))
    adopt(other, paper_id, candidate)
    assert history(other, paper_id)["current"] == candidate


def test_legacy_snapshot_and_v3_database_remain_readable(registered):
    cfg, _, _ = registered
    old = capture(cfg)
    old["version"] = 1
    del old["tables"]["summary_versions"]
    blob = packed(old)
    assert unpack(blob, hashlib.sha256(blob).hexdigest())["tables"]["summary_versions"] == []
    with Store(cfg.storage.database) as store:
        store.db.executescript("DROP TABLE summary_versions; PRAGMA user_version=3;")
    before = cfg.storage.database.read_bytes()
    with Store(cfg.storage.database, snapshot=True) as store:
        assert store.rows("SELECT * FROM summary_versions") == []
    assert cfg.storage.database.read_bytes() == before


def test_saved_answer_recovers_after_artifact_failure_without_another_model_call(
    registered, monkeypatch
):
    cfg, paper_id, original_id = registered
    external(cfg)
    provider = VisualProvider()
    from paperflow import summary_versions

    actual = summary_versions._build

    def unavailable(*args):
        raise OSError("合成の画像保存エラー")

    monkeypatch.setattr(summary_versions, "_build", unavailable)
    result = generate(cfg, [paper_id], provider=provider)
    assert len(result["failures"]) == 1 and len(provider.calls) == 1
    assert len(history(cfg, paper_id)["pending"]) == 1
    with pytest.raises(SettingsError, match="復元"):
        generate(cfg, [paper_id], provider=provider)
    assert len(provider.calls) == 1
    # 途中の共有スナップショットも受信できる。
    blob = packed(capture(cfg))
    unpack(blob, hashlib.sha256(blob).hexdigest())
    monkeypatch.setattr(summary_versions, "_build", actual)
    assert len(recover(cfg, paper_id)) == 1
    assert len(provider.calls) == 1
    state = history(cfg, paper_id)
    assert not state["pending"] and len(state["versions"]) == 2
    assert state["current"] == original_id


def test_only_selected_papers_are_sent_and_stop_preserves_remaining(tmp_path):
    from test_gui import gui_config

    from paperflow.agents.fake import FakeProvider
    from paperflow.gui_service import GuiService

    service = GuiService(gui_config(tmp_path))
    planned = service.prepare("all")
    service.run(planned["plan"], "fake", None)
    chosen = [p["paper_id"] for p in planned["items"][:2]]

    class Counter(FakeProvider):
        calls = []

        def analyze(self, request):
            self.calls.append(request.paper_id)
            return super().analyze(request)

    provider = Counter()
    result = generate(
        service.config(), chosen, provider=provider, should_stop=lambda: bool(provider.calls)
    )
    assert provider.calls == chosen[:1]
    assert len(result["completed"]) == 1 and result["stopped"]
    assert len(history(service.config(), planned["items"][2]["paper_id"])["versions"]) == 1


def test_synced_variant_cannot_write_control_files(registered):
    cfg, paper_id, _ = registered
    generate(cfg, [paper_id])
    data = capture(cfg)
    data["tables"]["summary_versions"][-1]["visuals_json"] = json.dumps(
        dict(
            assets=[dict(path="AGENTS.md", sha256="a" * 64)],
            recipe=None,
        )
    )
    blob = packed(data)
    with pytest.raises(ValueError, match="図表保存先"):
        unpack(blob, hashlib.sha256(blob).hexdigest())


def test_gui_service_allows_per_job_model_override_with_shared_progress(tmp_path, monkeypatch):
    from test_gui import gui_config

    from paperflow.gui_config import save_changes
    from paperflow.gui_service import GuiService
    from paperflow.progress_sync import settings

    service = GuiService(gui_config(tmp_path))
    planned = service.prepare("all")
    service.run(planned["plan"], "fake", None)
    paper_id = planned["items"][0]["paper_id"]
    save_changes(
        service.path,
        {
            "analysis.policy": "fulltext_external",
            "analysis.allow_external_fulltext": True,
            "analysis.require_visual_reading": True,
            "destination.include_visuals": True,
        },
    )
    service.create_progress_folder(tmp_path / "shared", "合成共有")
    provider = VisualProvider("選択したモデル")
    monkeypatch.setattr("paperflow.pipeline.Pipeline._provider", lambda self, name: provider)
    result = service.generate_summaries([paper_id], "claude", "選択したモデル")
    assert not result["failures"] and provider.calls == [paper_id]
    assert settings(service.config())["shared_profile"]["provider"] == "fake"
    assert service.config().analysis.default_provider == "fake"
    assert len(service.summary_history(paper_id)["versions"]) == 2
