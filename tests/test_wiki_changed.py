from types import SimpleNamespace as NS

import yaml

from paperflow.resources import wiki_check_changed as changed
from paperflow.wiki.framework_refresh import SKILLS, refresh


def test_framework_refresh_preserves_contracts_and_is_repeatable(tmp_path):
    root = tmp_path
    documents = [
        "AGENTS.md",
        "00_System/PaperFlow-Integration.md",
        "00_System/Templates/Paper-Source-Note.md",
    ]
    for name in documents:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("既存の原本保護と所有権の規約\n", encoding="utf-8")
    (root / ".llm-wiki-root").write_text('schema_version: 6\nframework_version: "1.8"\n')
    for name in SKILLS:
        path = root / f"agent-skills/{name}/SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text(
            f"---\nname: {name}\n---\n既存の規約\n"
            "uv run python scripts/validate_frontmatter.py --changed\n"
            "uv run python scripts/wiki_lint.py --changed\n",
            encoding="utf-8",
        )
    refresh(root)
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    refresh(root)
    after = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert after == before
    assert "既存の原本保護" in (root / "AGENTS.md").read_text(encoding="utf-8")
    assert yaml.safe_load((root / ".llm-wiki-root").read_text())["schema_version"] == 6
    for name in SKILLS:
        data = (root / f"agent-skills/{name}/SKILL.md").read_bytes()
        assert b"wiki_lint.py --changed" not in data
        for prefix in [".claude/skills", ".agents/skills"]:
            assert (root / prefix / name / "SKILL.md").read_bytes() == data


def test_changed_checker_detects_global_duplicate_and_checks_inbound_once(tmp_path, monkeypatch):
    from collections import Counter

    calls = Counter()

    class Note:
        def __init__(self, name, identifier, refs=()):
            self.path = tmp_path / name
            self.path.write_text("synthetic fixture")
            self.relpath = name
            self.id, self.name = identifier, name
            self.aliases = []
            self.fm_error = None
            self.frontmatter = {"type": "note"}
            self.refs = refs

        def body_without_code(self):
            return ""

        def headings(self):
            return []

        def block_ids(self):
            return []

        def links(self):
            return [NS(target=r) for r in self.refs]

        def property_links(self):
            return []

    notes = [
        Note("changed.md", "duplicate"),
        Note("old.md", "duplicate"),
        Note("citing.md", "citing", ["changed"]),
        Note("unrelated.md", "other"),
    ]

    class Index:
        def __init__(self, context):
            calls["index"] += 1
            self.by_path = {n.relpath: n for n in context}

        def resolve(self, target):
            return ([notes[0]] if target == "changed" else [], None)

    class Report:
        def __init__(self, name):
            self.findings = []

        def error(self, check, path, message):
            self.findings.append(dict(check=check, path=path, message=message, severity="error"))

        def to_dict(self):
            return dict(counts={"error": len(self.findings)}, findings=self.findings)

    def load_notes(paths):
        calls["load"] += 1
        return notes

    def nop(*args):
        pass

    fm = NS(
        build_validators=lambda: {},
        **{
            f"check_{name}": nop
            for name in [
                "schema",
                "vocabulary",
                "area",
                "tags",
                "link_properties",
                "unquoted_property_links",
                "task",
            ]
        },
    )
    markup = NS(
        **{
            f"check_{name}": nop
            for name in [
                "headings",
                "block_ids",
                "callouts",
                "markdown_links",
                "evidence_tables",
                "general_note",
                "locators",
            ]
        }
    )
    checked = []
    links = NS(check_note=lambda note, *args: checked.append(note.relpath))
    graph = NS(check_relations=nop, check_canonical=nop, check_source_note=nop, check_orphans=nop)
    vault = NS(
        VAULT=tmp_path,
        rel=lambda p: str(p),
        Report=Report,
        note_paths=lambda: [],
        load_notes=load_notes,
        LinkIndex=Index,
        load_yaml=lambda p: {},
        filename_problems=lambda p: [],
    )
    monkeypatch.setattr(changed, "modules", lambda: [vault, fm, markup, links, graph])
    result = changed.check(["changed.md"])
    assert calls == {"load": 1, "index": 1}
    assert checked == ["changed.md", "citing.md"]
    assert result["scope"]["inbound"] == 1
    assert [f["check"] for f in result["findings"]] == ["duplicate-id"]
