import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
import yaml
from conftest import tree
from test_wiki import wiki_config as wiki_config

from paperflow.resources import wiki_paper_database as db
from paperflow.wiki.bundle import allowed_write, apply
from paperflow.wiki.database import HOOK_ANCHOR, install, page_template
from paperflow.wiki.export import cached_items, prepare


def paper(root, slug, *, registered=True, year=2025, title=None):
    source = root / "10_Papers/10_Sources" / slug
    source.mkdir(parents=True)
    (source / "manifest.yml").write_text(
        yaml.safe_dump(
            dict(
                source_id="paper:" + slug,
                source_status="frozen" if registered else "registered",
                sha256="a" * 64,
            )
        ),
        encoding="utf-8",
    )
    (source / "source.pdf").write_bytes(b"synthetic fixture - never open")
    notes = root / "10_Papers/20_Source_Notes"
    notes.mkdir(parents=True, exist_ok=True)
    fm = dict(
        id="paper:" + slug,
        source_id="paper:" + slug,
        type="paper-note",
        title=title or slug,
        year=year,
        authors=["合成著者"],
        topics=["synthetic"],
        tags=["topic/synthetic"],
        review_status="unreviewed",
    )
    path = notes / (slug + ".md")
    path.write_text(
        "---\n" + yaml.safe_dump(fm, allow_unicode=True) + "---\n\n# 合成論文\n", encoding="utf-8"
    )
    return path, fm


def page(root):
    target = root / db.PAGE
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(page_template(), encoding="utf-8")
    return target


def base_config(text):
    return yaml.safe_load(text.split("```base\n", 1)[1].split("\n```", 1)[0])


def test_collect_only_registered_notes_never_reads_pdf_or_application_state(tmp_path, monkeypatch):
    good, fm = paper(tmp_path, "registered", title='A "quoted" | 合成論文')
    paper(tmp_path, "pending", registered=False)
    (tmp_path / ".obsidian").mkdir()
    (tmp_path / ".obsidian/private.md").write_text("private")
    before = tree(tmp_path)
    original = Path.open

    def guarded(path, *a, **k):
        assert path.suffix != ".pdf" and ".obsidian" not in path.parts
        return original(path, *a, **k)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", guarded)
        rows = db.collect(tmp_path)
        assert len(rows) == 1
        assert rows[0]["title"] == fm["title"]
        reused = db.collect(
            tmp_path, [NS(relpath=good.relative_to(tmp_path).as_posix(), frontmatter=fm)]
        )
        assert reused == rows
    assert tree(tmp_path) == before


def test_bases_views_are_readonly_and_empty_index_has_no_false_rows(tmp_path):
    config = db.bases([])
    assert config["filters"]["and"][-1] == {"or": ["false"]}
    assert {v["name"] for v in config["views"]} == {
        "すべて",
        "発行年別",
        "主題・タグ",
        "未レビュー",
        "Wiki統合状況",
    }
    for view in config["views"]:
        assert all(p.startswith("formula.") for p in view["order"])
    assert "未記載" in config["formulas"]["integration"]


def test_update_is_idempotent_and_check_is_read_only(tmp_path):
    paper(tmp_path, "registered")
    target = page(tmp_path)
    before = tree(tmp_path)
    assert db.update_database(tmp_path, check=True)
    assert tree(tmp_path) == before
    assert db.update_database(tmp_path) == []
    assert "1件" in target.read_text(encoding="utf-8")
    updated = tree(tmp_path)
    assert db.update_database(tmp_path) == []
    assert db.update_database(tmp_path, check=True) == []
    assert tree(tmp_path) == updated


def test_fallback_table_preserves_columns_and_unknown_metadata(tmp_path):
    paper(tmp_path, "synthetic", title="原題 | 特殊文字")
    rows = db.collect(tmp_path)
    rows[0]["authors"] = ["A | B", "合成著者"]
    output = db.render(rows)
    row = next(line for line in output.splitlines() if line.startswith("> | [["))
    assert len(re.split(r"(?<!\\)\|", row)) == 8
    assert "[[10_Papers/20_Source_Notes/synthetic]]" in row
    assert "未記載" in row and "Wiki統合済み" not in row


def test_custom_views_and_text_survive_new_paper(tmp_path):
    paper(tmp_path, "first")
    target = page(tmp_path)
    db.update_database(tmp_path)
    text = target.read_text(encoding="utf-8")
    config = base_config(text)
    custom = dict(
        type="table",
        name="独自の絞り込み",
        filters="note.year == 2025",
        order=["formula.paper", "formula.doi"],
    )
    config["views"].append(custom)
    old = text.split("```base\n", 1)[1].split("\n```", 1)[0]
    text = (
        text.replace(old, yaml.safe_dump(config, allow_unicode=True, sort_keys=False).rstrip())
        + "\nこの追記は保持する。\n"
    )
    target.write_text(text, encoding="utf-8")
    paper(tmp_path, "second", year=2026)
    db.update_database(tmp_path)
    updated = target.read_text(encoding="utf-8")
    assert base_config(updated)["views"][-1] == custom
    assert "2件" in updated and updated.endswith("この追記は保持する。\n")


def test_unowned_page_and_invalid_markers_are_not_overwritten(tmp_path):
    target = page(tmp_path)
    text = target.read_text(encoding="utf-8").replace("owner: agent", "owner: human")
    target.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match="管理権限"):
        db.update_database(tmp_path)
    assert target.read_text(encoding="utf-8") == text
    target.write_text(page_template().replace(db.END, ""), encoding="utf-8")
    with pytest.raises(ValueError, match="自動更新領域"):
        db.update_database(tmp_path)


INDEX_SCRIPT = (
    """import argparse,os
from pathlib import Path
from types import SimpleNamespace as NS
import yaml
VAULT=Path(os.environ['LLM_WIKI_ROOT'])
INDEX_DIR=VAULT/'00_System/Index'
def apply(*a):return []
def build_source_index(notes):return {}
def main():
    args=argparse.ArgumentParser()
    args.add_argument('--check',action='store_true')
    args=args.parse_args()
    notes=[NS(relpath=p.relative_to(VAULT).as_posix(),frontmatter=yaml.safe_load(p.read_text(encoding='utf-8').split('---',2)[1])) for p in (VAULT/'10_Papers/20_Source_Notes').glob('*.md')]
    problems=[]
"""
    + HOOK_ANCHOR
    + """
    return int(bool(problems))
if __name__=='__main__':raise SystemExit(main())
"""
)


def infrastructure(root):
    (root / ".llm-wiki-root").write_text(
        'schema_version: 6\nframework_version: "1.11"\nsession_history_integration: 1\n',
        encoding="utf-8",
    )
    for name in [
        "00_System/Index/index.md",
        "00_System/Index/source-index.md",
        "00_System/PaperFlow-Integration.md",
        "agent-skills/wiki-ingest-paper/SKILL.md",
    ]:
        p = root / name
        if not p.is_file():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("# 既存の制御文書\n\n## 既存の説明\n\n保持する\n", encoding="utf-8")
    (root / "scripts").mkdir(exist_ok=True)
    (root / "scripts/update_index.py").write_text(INDEX_SCRIPT, encoding="utf-8")


def test_install_is_repeatable_and_native_index_command_updates_automatically(tmp_path):
    infrastructure(tmp_path)
    paper(tmp_path, "first")
    install(tmp_path)
    before = tree(tmp_path)
    install(tmp_path)
    assert tree(tmp_path) == before
    marker = yaml.safe_load((tmp_path / ".llm-wiki-root").read_text(encoding="utf-8"))
    assert marker["schema_version"] == 6 and marker["session_history_integration"] == 1
    assert marker["framework_version"] == "1.12"
    paper(tmp_path, "second", year=2026)
    for check, code in [(True, 1), (False, 0), (True, 0)]:
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                "-X",
                "utf8",
                str(tmp_path / "scripts/update_index.py"),
                *(["--check"] if check else []),
            ],
            env=dict(os.environ, LLM_WIKI_ROOT=str(tmp_path)),
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert result.returncode == code, result.stdout + result.stderr
    assert "2件" in (tmp_path / db.PAGE).read_text(encoding="utf-8")


@pytest.mark.parametrize("archive_zone", ["35_Remote_Captures", "35_Agent_Sessions"])
def test_new_paper_bundle_updates_database_and_validates_it_once(wiki_config, archive_zone):
    cfg = wiki_config
    infrastructure(cfg.destination.vault_dir)
    install(cfg.destination.vault_dir)
    # 本番Vaultと同じく、既存ナビゲーションが指す廃止済み制御索引を含める。
    # sparse previewでもリンク解決でき、凍結済み索引自体は書き換えない。
    archive = archive_zone + "/90_Archive/legacy-remote-capture/index/remote-capture-index.md"
    archived_index = cfg.destination.vault_dir / archive
    archived_index.parent.mkdir(parents=True)
    archived_index.write_text("# 合成の凍結索引\n", encoding="utf-8")
    checker = cfg.destination.vault_dir / "scripts/check_changed.py"
    checker.write_text(
        'import json,sys\nprint(json.dumps({"counts":{"error":0},"findings":[],"args":sys.argv[1:]}))\n',
        encoding="utf-8",
    )
    bundle = cfg.storage.data_dir.parent / "database-flow"
    plan = prepare(cfg, cached_items(cfg), bundle)
    assert db.PAGE in [e["path"] for e in plan["changes"]]
    assert plan["validation"]["check_changed.py"]["args"].count(db.PAGE) == 1
    assert (bundle / "preview" / archive).read_bytes() == archived_index.read_bytes()
    assert archive in plan["frozen_guards"]
    assert archive not in [e["path"] for e in plan["changes"]]
    # 新旧どちらの配置でも、準備後の変更を無視して反映しない。
    from paperflow.obsidian.writer import WriteConflict

    original = archived_index.read_bytes()
    archived_index.write_text("準備後の変更\n", encoding="utf-8")
    after_edit = tree(cfg.destination.vault_dir)
    with pytest.raises(WriteConflict, match="凍結出典"):
        apply(cfg, bundle)
    assert tree(cfg.destination.vault_dir) == after_edit
    archived_index.write_bytes(original)
    assert allowed_write(db.PAGE) and allowed_write("scripts/paper_database.py")
    assert not allowed_write("00_System/Index/任意のデータベース.md")
    apply(cfg, bundle)
    assert len(db.collect(cfg.destination.vault_dir)) == 1
    assert "1件" in (cfg.destination.vault_dir / db.PAGE).read_text(encoding="utf-8")
