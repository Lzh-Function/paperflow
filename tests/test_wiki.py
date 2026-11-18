import json

import pytest
import yaml
from click.testing import CliRunner
from conftest import tree

from paperflow.cli import app
from paperflow.config import Destination, SettingsError
from paperflow.obsidian.writer import WriteConflict
from paperflow.pipeline import Pipeline
from paperflow.wiki.bundle import apply, inventory, json_write, sha, wiki_lock
from paperflow.wiki.export import cached_items, prepare

# 合成Vaultの制御スクリプト。実際のVault版検証器は別途C:リハーサルで検証する。
CONTROL = """
import json,sys
from pathlib import Path
name=Path(__file__).name
root=Path(__file__).resolve().parents[1]
if name=='append_log.py':
 with (root/'00_System/Index/log.md').open('a',encoding='utf-8') as f:f.write('\\n'+' '.join(sys.argv[1:]))
elif name=='update_index.py':
 notes=sorted(p.stem for p in (root/'10_Papers/20_Source_Notes').glob('*.md'))
 (root/'00_System/Index/source-index.md').write_text('\\n'.join(notes),encoding='utf-8')
else:
 print(json.dumps({'counts':{'error':0,'warning':0,'info':0},'findings':[]}))
"""


@pytest.fixture
def wiki_config(small_config):
    cfg = small_config
    Pipeline(cfg).run(scan_only=True)
    Pipeline(cfg).run(export=False)
    cfg.destination = Destination(
        vault_dir=cfg.destination.vault_dir,
        layout="llm_wiki",
        copy_pdf=True,
        allow_vault_write=True,
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
    (root / "00_System/Index/intake-index.md").write_text("既存Inboxを保持\n", encoding="utf-8")
    for script in [
        "append_log.py",
        "update_index.py",
        "validate_frontmatter.py",
        "validate_obsidian_markup.py",
        "resolve_links.py",
        "check_graph_integrity.py",
    ]:
        (root / "scripts" / script).write_text(CONTROL, encoding="utf-8")
    return cfg


def bundle_for(cfg, name="bundle"):
    path = cfg.storage.data_dir.parent / name
    plan = prepare(cfg, cached_items(cfg), path)
    return path, plan


def test_freeze_register_and_pending_notes_preserve_original_and_vault(wiki_config):
    cfg = wiki_config
    before_source = tree(cfg.source.paperpile_dir)
    before_vault = tree(cfg.destination.vault_dir)
    bundle, plan = bundle_for(cfg)
    assert tree(cfg.source.paperpile_dir) == before_source
    assert tree(cfg.destination.vault_dir) == before_vault
    assert len(plan["new_source_dirs"]) == 1
    frozen = bundle / "preview" / plan["new_source_dirs"][0]
    assert sha(frozen / "source.pdf") == sha(next(cfg.source.paperpile_dir.glob("*.pdf")))
    manifest = yaml.safe_load((frozen / "manifest.yml").read_text(encoding="utf-8"))
    payload = json.loads((frozen / "extracted.json").read_text(encoding="utf-8"))
    assert payload["sha256"] == manifest["sha256"]
    assert payload["source_id"] == manifest["source_id"]
    assert payload["blocks"][0]["block_id"] == "p001-b001"
    note = bundle / "preview" / plan["registered"][0]["note"]
    fm = yaml.safe_load(note.read_text(encoding="utf-8").split("---", 2)[1])
    assert fm["source_id"] == manifest["source_id"]
    assert fm["type"] == "paper-note" and fm["integration_status"] == "proposal-pending"
    assert fm["source_ids"] == [manifest["source_id"]]
    assert fm["evidence_status"] == "unverified"
    assert fm["source_file"].endswith("/source.pdf]]")
    text = note.read_text(encoding="utf-8")
    assert "## 主要な図表" in text and "[!info]- 処理記録" in text
    assert plan["derived_assets"]
    for name in plan["derived_assets"]:
        assert any(change["path"] == name for change in plan["changes"])
        assert (bundle / "preview" / name).is_file()
    assert not any(e["path"].startswith("50_Wiki/Concepts/") for e in plan["changes"])


def test_apply_and_repeated_cached_export_are_idempotent(wiki_config):
    cfg = wiki_config
    bundle, plan = bundle_for(cfg)
    result = apply(cfg, bundle)
    assert result["written"] == len(plan["changes"])
    before = tree(cfg.destination.vault_dir)
    assert apply(cfg, bundle)["written"] == 0
    assert tree(cfg.destination.vault_dir) == before
    second, again = bundle_for(cfg, "bundle-again")
    assert again["changes"] == []
    assert apply(cfg, second)["written"] == 0
    assert tree(cfg.destination.vault_dir) == before
    assert not (cfg.destination.vault_dir / "00_System/Locks/agent-write-lock.yml").exists()


@pytest.mark.parametrize("kind", ["source", "payload", "note", "new-note"])
def test_changes_abort_without_overwriting(wiki_config, kind):
    cfg = wiki_config
    bundle, plan = bundle_for(cfg)
    if kind == "payload":
        p = bundle / "preview" / plan["changes"][0]["path"]
        p.write_bytes(b"tampered")
    elif kind == "source":
        next(cfg.source.paperpile_dir.glob("*.pdf")).write_bytes(b"changed original")
        with pytest.raises(WriteConflict):
            cached_items(cfg)
        return
    elif kind == "note":
        (cfg.destination.vault_dir / "00_System/Index/log.md").write_text(
            "別のWriterの編集", encoding="utf-8"
        )
    else:
        (cfg.destination.vault_dir / "50_Wiki/Concepts/new.md").write_text(
            "別のWriterの新規ノート", encoding="utf-8"
        )
    before = tree(cfg.destination.vault_dir)
    with pytest.raises(WriteConflict):
        apply(cfg, bundle)
    assert tree(cfg.destination.vault_dir) == before


def test_manual_edit_to_managed_source_note_is_preserved(wiki_config):
    cfg = wiki_config
    bundle, plan = bundle_for(cfg)
    apply(cfg, bundle)
    note = cfg.destination.vault_dir / plan["registered"][0]["note"]
    note.write_text(note.read_text(encoding="utf-8") + "\n人間の補足\n", encoding="utf-8")
    with pytest.raises(WriteConflict):
        bundle_for(cfg, "conflict")
    assert "人間の補足" in note.read_text(encoding="utf-8")


def test_initial_permission_and_same_host_lock_are_enforced(wiki_config):
    cfg = wiki_config
    bundle, _ = bundle_for(cfg)
    cfg.destination.allow_vault_write = False
    with pytest.raises(SettingsError):
        apply(cfg, bundle)
    cfg.destination.allow_vault_write = True
    with wiki_lock(cfg.destination.vault_dir):
        before = tree(cfg.destination.vault_dir)
        with pytest.raises(WriteConflict):
            apply(cfg, bundle)
        assert tree(cfg.destination.vault_dir) == before


def test_apply_failure_rolls_back_and_can_retry(wiki_config, monkeypatch):
    import paperflow.wiki.bundle as module

    cfg = wiki_config
    bundle, plan = bundle_for(cfg)
    before = inventory(cfg.destination.vault_dir)
    original = module.atomic_copy
    calls = []

    def fail_once(source, dest, **kwargs):
        calls.append(dest)
        if len(calls) == 3:
            raise OSError("合成した一時障害")
        return original(source, dest, **kwargs)

    monkeypatch.setattr(module, "atomic_copy", fail_once)
    with pytest.raises(OSError):
        apply(cfg, bundle)
    assert inventory(cfg.destination.vault_dir) == before
    assert json.loads((bundle / "journal.json").read_text())["status"] == "rolled-back"
    assert apply(cfg, bundle)["written"] == len(plan["changes"])


def test_crash_journal_recovery(wiki_config):
    from paperflow.wiki.bundle import atomic_copy

    cfg = wiki_config
    bundle, plan = bundle_for(cfg)
    entry = plan["changes"][0]
    atomic_copy(
        bundle / "preview" / entry["path"],
        cfg.destination.vault_dir / entry["path"],
        replace=entry["before"] is not None,
    )
    json_write(bundle / "journal.json", dict(status="applying", touched=[entry]))
    assert apply(cfg, bundle)["written"] == len(plan["changes"])
    assert json.loads((bundle / "journal.json").read_text())["status"] == "complete"


def test_dry_run_starts_no_process_and_writes_nothing(wiki_config, monkeypatch):
    cfg = wiki_config
    path = cfg.source.paperpile_dir.parent / "config.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["destination"].update(layout="llm_wiki", copy_pdf=True)
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    before = tree(path.parent)
    monkeypatch.setattr(
        "subprocess.Popen", lambda *a, **k: pytest.fail("dry-runのプロセス起動は禁止")
    )
    result = CliRunner().invoke(
        app, ["--config", str(path), "wiki-prepare", "--cached-only", "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["dry_run"] is True
    result = CliRunner().invoke(
        app, ["--config", str(path), "sync", "--dry-run", "--no-interactive"]
    )
    assert result.exit_code == 0, result.output
    assert tree(path.parent) == before


def test_same_doi_different_pdf_is_not_silently_merged(wiki_config):
    cfg = wiki_config
    item = cached_items(cfg)[0]
    directory = cfg.destination.vault_dir / "10_Papers/10_Sources/other-version"
    directory.mkdir(parents=True)
    (directory / "manifest.yml").write_text(
        yaml.safe_dump(dict(source_id="paper:old", sha256="0" * 64, doi=item["metadata"]["doi"])),
        encoding="utf-8",
    )
    with pytest.raises(WriteConflict, match="版"):
        bundle_for(cfg)


def test_existing_human_source_note_is_reused_without_body_change(wiki_config):
    cfg = wiki_config
    bundle, plan = bundle_for(cfg)
    apply(cfg, bundle)
    record = plan["registered"][0]
    receipt = cfg.destination.vault_dir / f"00_System/PaperFlow/receipts/{record['paper_id']}.json"
    receipt.unlink()
    note = cfg.destination.vault_dir / record["note"]
    note.write_text(note.read_text(encoding="utf-8") + "\n既存の読解\n", encoding="utf-8")
    proposal = next((cfg.destination.vault_dir / "70_Review/Merge_Proposals").glob("*.md"))
    proposal.unlink()
    second, again = bundle_for(cfg, "reuse-existing")
    assert record["note"] not in [e["path"] for e in again["changes"]]
    assert again["new_source_dirs"] == []
    assert "既存の読解" in note.read_text(encoding="utf-8")


def test_tampered_plan_cannot_write_canonical_claim(wiki_config):
    cfg = wiki_config
    bundle, plan = bundle_for(cfg)
    plan["changes"].append(dict(path="50_Wiki/Claims/unsafe.md", before=None, after="0" * 64))
    json_write(bundle / "plan.json", plan)
    with pytest.raises(SettingsError, match="許可範囲"):
        apply(cfg, bundle)


def test_normal_sync_uses_formal_wiki_flow_and_cached_repeat(wiki_config, monkeypatch):
    cfg = wiki_config
    monkeypatch.setattr(
        "paperflow.agents.claude_cli.ClaudeCliProvider.analyze",
        lambda *a: pytest.fail("外部LLM禁止"),
    )
    result = Pipeline(cfg).run()
    assert result["analyzed"] == 0 and result["exported"] > 0
    assert not (cfg.destination.vault_dir / "PaperFlow").exists()
    assert len(list((cfg.destination.vault_dir / "10_Papers/10_Sources").glob("*/source.pdf"))) == 1
    before = tree(cfg.destination.vault_dir)
    again = Pipeline(cfg).run()
    assert again["analyzed"] == 0 and again["exported"] == 0
    assert tree(cfg.destination.vault_dir) == before


def test_reused_frozen_pdf_is_checked_before_apply(wiki_config):
    cfg = wiki_config
    first, _ = bundle_for(cfg)
    apply(cfg, first)
    second, plan = bundle_for(cfg, "reused-guard")
    assert plan["frozen_guards"]
    pdf = next((cfg.destination.vault_dir / "10_Papers/10_Sources").glob("*/source.pdf"))
    pdf.write_bytes("合成した出典破損".encode())
    with pytest.raises(WriteConflict, match="凍結出典"):
        apply(cfg, second)


def test_formal_report_links_and_edit_protection(wiki_config):
    cfg = wiki_config
    Pipeline(cfg).run()
    path = cfg.source.paperpile_dir.parent / "config.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["destination"].update(layout="llm_wiki", copy_pdf=True)
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    runner = CliRunner()
    for _ in range(2):
        result = runner.invoke(
            app, ["--config", str(path), "report", "--min-priority", "1", "--write"]
        )
        assert result.exit_code == 0, (result.output, result.exception)
    report = cfg.destination.vault_dir / "60_Outputs/PaperFlow/読書候補.md"
    assert "[[10_Papers/20_Source_Notes/" in report.read_text(encoding="utf-8")
    assert "PaperFlow/Analysis" not in report.read_text(encoding="utf-8")
    report.write_text(report.read_text(encoding="utf-8") + "\n人間の追記\n", encoding="utf-8")
    result = runner.invoke(app, ["--config", str(path), "report", "--min-priority", "1", "--write"])
    assert isinstance(result.exception, WriteConflict)
    assert "人間の追記" in report.read_text(encoding="utf-8")


def test_render_preserves_evidence_with_different_claim_wording(wiki_config):
    from paperflow.wiki.render import render

    item = cached_items(wiki_config)[0]
    item["analysis"]["data"]["evidence"] = [
        dict(
            claim="所見とは別の表現の根拠候補 <unsafe>",
            page=1,
            locator="Table 1 '長い原文引用はWikiに転載しない'",
        )
    ]
    text, _ = render(
        item,
        "paper:test",
        "10_Papers/20_Source_Notes/test.md",
        "70_Review/Merge_Proposals/test.md",
        [dict(page=1)],
        [],
    )
    assert "所見とは別の表現の根拠候補" in text
    assert "PDF p. 1; Table 1" in text
    assert "&lt;unsafe&gt;" in text
    assert "長い原文引用はWikiに転載しない" not in text
    assert "文章が一致しない候補も保持" in text
    assert "evidence_status: unverified" in text


def test_render_rejects_invalid_page_even_for_paraphrased_evidence(wiki_config):
    from paperflow.wiki.render import render

    item = cached_items(wiki_config)[0]
    item["analysis"]["data"]["evidence"] = [dict(claim="異なる文章", page=99, locator="不正な位置")]
    with pytest.raises(ValueError, match="根拠ページ"):
        render(
            item,
            "paper:test",
            "10_Papers/20_Source_Notes/test.md",
            "70_Review/Merge_Proposals/test.md",
            [dict(page=1)],
            [],
        )


def test_promotion_requires_reviewed_plan_hash(wiki_config):
    from paperflow.wiki.bundle import allowed_promotion_write

    cfg = wiki_config
    bundle, plan = bundle_for(cfg)
    name = "50_Wiki/Concepts/合成した統合先.md"
    payload = bundle / "preview" / name
    payload.parent.mkdir(parents=True, exist_ok=True)
    payload.write_text("合成した承認対象の追記\n", encoding="utf-8")
    plan["changes"] = [dict(path=name, before=None, after=sha(payload))]
    moc = "50_Wiki/Areas/Academic.md"
    moc_payload = bundle / "preview" / moc
    moc_payload.parent.mkdir(parents=True, exist_ok=True)
    moc_payload.write_text("合成した領域索引\n", encoding="utf-8")
    plan["changes"].append(dict(path=moc, before=None, after=sha(moc_payload)))
    assert not allowed_promotion_write("50_Wiki/Areas/無関係.md")
    plan["registered"] = []
    plan["operation"] = "promote"
    json_write(bundle / "plan.json", plan)
    with pytest.raises(SettingsError, match="承認"):
        apply(cfg, bundle)
    with pytest.raises(SettingsError, match="承認"):
        apply(cfg, bundle, approved_promotion_sha256="0" * 64)
    assert not (cfg.destination.vault_dir / name).exists()
    assert apply(cfg, bundle, approved_promotion_sha256=sha(bundle / "plan.json"))[
        "written"
    ] == len(plan["changes"])


def test_promotion_rejects_frozen_source_changes(wiki_config):
    cfg = wiki_config
    bundle, plan = bundle_for(cfg)
    plan["operation"] = "promote"
    json_write(bundle / "plan.json", plan)
    with pytest.raises(SettingsError, match="許可範囲"):
        apply(cfg, bundle, approved_promotion_sha256=sha(bundle / "plan.json"))


def test_integrated_source_requires_review_for_new_analysis(wiki_config):
    cfg = wiki_config
    first, plan = bundle_for(cfg)
    apply(cfg, first)
    paper_id = plan["registered"][0]["paper_id"]
    receipt = cfg.destination.vault_dir / f"00_System/PaperFlow/receipts/{paper_id}.json"
    data = json.loads(receipt.read_text(encoding="utf-8"))
    data["status"] = "integrated"
    json_write(receipt, data)
    item = cached_items(cfg)[0]
    item["analysis"]["run_id"] = "new-synthetic-analysis"
    before = tree(cfg.destination.vault_dir)
    with pytest.raises(WriteConflict, match="統合済み"):
        prepare(cfg, [item], cfg.storage.data_dir.parent / "new-analysis-review")
    assert tree(cfg.destination.vault_dir) == before


def test_visual_reading_selection_and_description_reach_source_note(wiki_config):
    cfg = wiki_config
    item = cached_items(cfg)[0]
    item["analysis"]["data"]["visual_review"] = dict(
        status="completed",
        reason_ja="合成試験",
        pages_viewed=[1],
        selected_visuals=[
            dict(
                page=1,
                kind="page",
                number=None,
                title_ja="合成ページの表示",
                explanation_ja="視覚入力から選んだ図の説明を保持する合成試験です。",
                finding_indices=[],
                uncertainty_ja="試験用です。",
            )
        ],
    )
    bundle = cfg.storage.data_dir.parent / "visual-reading-bundle"
    plan = prepare(cfg, [item], bundle)
    note = bundle / "preview" / plan["registered"][0]["note"]
    text = note.read_text(encoding="utf-8")
    assert "合成ページの表示" in text
    assert "視覚入力から選んだ図の説明" in text
    assert "AIの視覚読解: 1ページ" in text
    image_plan = next(c for c in plan["changes"] if c["path"].endswith("/visuals.json"))
    record = json.loads((bundle / "preview" / image_plan["path"]).read_text(encoding="utf-8"))
    assert record["selection_method"] == "agent"
