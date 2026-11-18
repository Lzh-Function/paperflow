import json

import pytest
import yaml
from conftest import tree
from test_wiki import wiki_config as wiki_fixture

from paperflow.subject_tags import patch_note, register_topics, subject_tags
from paperflow.wiki.bundle import sha
from paperflow.wiki.export import cached_items
from paperflow.wiki.export import prepare as prepare_ingest
from paperflow.wiki.tag_migration import apply_migration, inspect, prepare


@pytest.fixture
def wiki_config(tmp_path, small_config):
    return wiki_fixture.__wrapped__(small_config)


def note(owner="agent", newline="\n"):
    return (
        (
            "---\ntitle: テスト\nowner: "
            + owner
            + "\nagent_editable: true\npaperflow_managed: true\ntags:\n- note/paper\n- area/academic\ntopics: [smiles, benchmark]\n---\n\n# 見出し\n\n人間が補足した内容 *保持*。\n"
        )
        .replace("\n", newline)
        .encode("utf-8")
    )


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("bom", [b"", b"\xef\xbb\xbf"])
def test_minimal_metadata_edit_preserves_body_other_properties_and_is_idempotent(newline, bom):
    raw = bom + note(newline=newline)
    changed = patch_note(raw)
    assert changed != raw
    assert changed.split(b"---", 2)[2] == raw.split(b"---", 2)[2]
    fm = yaml.safe_load(changed.decode("utf-8-sig").split("---", 2)[1])
    assert fm["tags"] == ["note/paper", "area/academic", "topic/smiles", "topic/benchmark"]
    assert fm["topics"] == ["smiles", "benchmark"]
    assert patch_note(changed) == changed
    assert changed.startswith(bom + b"---")


def test_human_and_unmanaged_notes_are_kept():
    for raw in (
        note(owner="human"),
        note().replace(b"paperflow_managed: true", b"paperflow_managed: false"),
        note().replace(b"agent_editable: true", b"agent_editable: false"),
    ):
        assert patch_note(raw) == raw


def test_registered_topic_tags_preserve_dictionary_comments(tmp_path):
    path = tmp_path / "00_System/Dictionaries/tag-taxonomy.yml"
    path.parent.mkdir(parents=True)
    path.write_text(
        "# コメント保持\nversion: 3\nallowed:\n  note: [paper]\nrules:\n- 既存規約\n",
        encoding="utf-8",
    )
    assert register_topics(tmp_path, ["smiles", "benchmark"])
    assert path.read_text(encoding="utf-8").startswith("# コメント保持\nversion: 4")
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["allowed"]["topic"] == [
        "smiles",
        "benchmark",
    ]
    assert register_topics(tmp_path, ["smiles", "benchmark"]) is False
    assert register_topics(tmp_path, ["smiles", "other"])
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["allowed"]["topic"] == [
        "smiles",
        "benchmark",
        "other",
    ]
    with pytest.raises(ValueError):
        subject_tags(["unsafe/value"])


def test_generator_emits_obsidian_tags(wiki_config):
    cfg = wiki_config
    bundle = cfg.storage.data_dir.parent / "new-ingest"
    plan = prepare_ingest(cfg, cached_items(cfg), bundle)
    path = bundle / "preview" / plan["registered"][0]["note"]
    fm = yaml.safe_load(path.read_text(encoding="utf-8").split("---", 2)[1])
    assert fm["topics"]
    assert all("topic/" + topic in fm["tags"] for topic in fm["topics"])


def test_backfill_updates_only_generated_metadata_and_matching_receipt(wiki_config):
    cfg = wiki_config
    root = cfg.destination.vault_dir
    path = root / "10_Papers/20_Source_Notes/generated.md"
    raw = note().replace(b"topics:", b"paperflow_id: fixture\ntopics:")
    path.write_bytes(raw)
    human = path.with_name("human.md")
    human.write_bytes(note(owner="human"))
    receipt = root / "00_System/PaperFlow/receipts/fixture.json"
    receipt.parent.mkdir(parents=True)
    relative = path.relative_to(root).as_posix()
    receipt.write_text(json.dumps(dict(managed={relative: sha(path)})), encoding="utf-8")
    dictionary = root / "00_System/Dictionaries/tag-taxonomy.yml"
    dictionary.parent.mkdir(parents=True)
    dictionary.write_text("version: 3\nallowed:\n  note: [paper]\n", encoding="utf-8")
    before = tree(root)
    source_before = tree(cfg.source.paperpile_dir)
    assert len(inspect(cfg)) == 1
    assert tree(root) == before
    bundle = cfg.storage.data_dir.parent / "tags"
    data = prepare(cfg, bundle)
    assert tree(root) == before
    assert len(data["notes"]) == 1
    result = apply_migration(cfg, bundle)
    assert result["notes"] == 1
    assert human.read_bytes() == note(owner="human")
    assert path.read_bytes().split(b"---", 2)[2] == raw.split(b"---", 2)[2]
    assert json.loads(receipt.read_text(encoding="utf-8"))["managed"][relative] == sha(path)
    assert tree(cfg.source.paperpile_dir) == source_before
    assert inspect(cfg) == []
