import pymupdf
import pytest
from conftest import tree

from paperflow.resources.wiki_check_changed import selected_paths
from paperflow.resources.wiki_extract_visuals import extract_visuals, sha
from paperflow.wiki.bundle import allowed_write


def figure_pdf(path):
    with pymupdf.open() as doc:
        page = doc.new_page(width=600, height=800)
        page.draw_rect(pymupdf.Rect(50, 80, 250, 220), color=(0, 0, 0), fill=(0.6, 0.8, 1))
        page.insert_text((50, 77), "Input label outside vector bounds", fontsize=8)
        page.insert_text((50, 235), "Fig. 1 Architecture of a synthetic model", fontsize=8)
        page.insert_text((50, 300), "Synthetic paper for local tests only.", fontsize=11)
        doc.save(path)


def test_visuals_crop_and_provenance_are_reusable(tmp_path):
    pdf = tmp_path / "source.pdf"
    figure_pdf(pdf)
    output = tmp_path / "figures"
    data = extract_visuals(pdf, output, expected_sha256=sha(pdf), source_id="paper:test", maximum=1)
    item = data["items"][0]
    assert item["kind"] == "figure" and item["number"] == 1 and item["page"] == 1
    assert item["mode"] == "crop" and item["bbox"][3] < 240
    assert item["bbox"][1] < 70  # vector外の図中ラベルを欠落させない
    assert item["review_status"] == "unreviewed"
    assert sha(output / item["file"]) == item["sha256"]
    before = tree(tmp_path)
    assert (
        extract_visuals(pdf, output, expected_sha256=sha(pdf), source_id="paper:test", maximum=1)
        == data
    )
    assert tree(tmp_path) == before


def test_captionless_pdf_is_honestly_a_page_excerpt(tmp_path):
    pdf = tmp_path / "source.pdf"
    with pymupdf.open() as doc:
        p = doc.new_page()
        p.insert_text((40, 40), "Synthetic abstract, no figures.")
        doc.save(pdf)
    data = extract_visuals(
        pdf, tmp_path / "figures", expected_sha256=sha(pdf), source_id="paper:test"
    )
    assert data["items"][0]["kind"] == "page"
    assert data["items"][0]["mode"] == "page-context"


def test_visuals_refuse_changed_input_or_user_edited_images(tmp_path):
    pdf = tmp_path / "source.pdf"
    figure_pdf(pdf)
    with pytest.raises(ValueError, match="ハッシュ"):
        extract_visuals(pdf, tmp_path / "wrong", expected_sha256="0" * 64, source_id="paper:test")
    assert not (tmp_path / "wrong").exists()
    out = tmp_path / "figures"
    data = extract_visuals(pdf, out, expected_sha256=sha(pdf), source_id="paper:test")
    (out / data["items"][0]["file"]).write_bytes(b"user edit")
    with pytest.raises(ValueError, match="変更"):
        extract_visuals(pdf, out, expected_sha256=sha(pdf), source_id="paper:test")


def test_visuals_do_not_overwrite_unmanaged_output(tmp_path):
    pdf = tmp_path / "source.pdf"
    figure_pdf(pdf)
    out = tmp_path / "figures"
    out.mkdir()
    (out / "human.txt").write_text("keep")
    with pytest.raises(ValueError, match="管理外"):
        extract_visuals(pdf, out, expected_sha256=sha(pdf), source_id="paper:test")
    assert (out / "human.txt").read_text() == "keep"


def test_changed_scope_rejects_app_state_missing_and_outside(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    note = root / "note.md"
    note.write_text("synthetic")
    assert selected_paths(root, ["note.md"]) == {"note.md"}
    outside = tmp_path / "outside.md"
    outside.write_text("synthetic")
    app = root / ".obsidian"
    app.mkdir()
    (app / "state.md").write_text("do not read")
    for path in [str(outside), ".obsidian/state.md", "missing.md"]:
        with pytest.raises(ValueError):
            selected_paths(root, [path])


def test_media_write_scope_stays_in_derived_assets():
    assert allowed_write("10_Papers/10_Sources/test/figures/paperflow-visuals-v1/figure-1-p001.png")
    assert allowed_write("10_Papers/10_Sources/test/figures/paperflow-visuals-v1/visuals.json")
    assert not allowed_write("10_Papers/10_Sources/test/figures/paperflow-visuals-v1/run.exe")
    assert not allowed_write("10_Papers/10_Sources/test/figures/arbitrary/user.png")


def test_compound_captions_match_distinct_crops_and_safe_filenames(tmp_path):
    pdf = tmp_path / "source.pdf"
    labels = ["4.4", "4.5", "4.10", "A.1", "S2", "2(a)"]
    with pymupdf.open() as doc:
        page = doc.new_page(width=600, height=850)
        for i, label in enumerate(labels):
            y = 40 + i * 130
            page.draw_rect(pymupdf.Rect(50, y, 250, y + 70), fill=(i / 6, 0.4, 0.6))
            page.insert_text((50, y + 85), f"Figure {label}: Synthetic result", fontsize=8)
        doc.save(pdf)
    selections = [dict(page=1, kind="figure", number=label) for label in labels]
    data = extract_visuals(
        pdf,
        tmp_path / "figures",
        expected_sha256=sha(pdf),
        source_id="paper:test",
        maximum=8,
        selections=selections,
    )
    assert [v["number"] for v in data["items"]] == labels
    assert all(v["mode"] == "crop" for v in data["items"])
    assert len({v["file"] for v in data["items"]}) == len(labels)
    for item in data["items"]:
        assert allowed_write(
            "10_Papers/10_Sources/test/figures/paperflow-visuals-v1/" + item["file"]
        )


def test_multiple_unlocated_selections_keep_separate_descriptions_and_assets(tmp_path):
    pdf = tmp_path / "source.pdf"
    figure_pdf(pdf)
    selections = [
        dict(page=1, kind="figure", number=None, title_ja=title)
        for title in ["番号不明の図", "別の番号不明の図"]
    ]
    output = tmp_path / "figures"
    data = extract_visuals(
        pdf, output, expected_sha256=sha(pdf), source_id="paper:test", selections=selections
    )
    assert len({item["file"] for item in data["items"]}) == 2
    assert [v["selection"] for v in data["items"]] == selections
    assert all(v["mode"] == "page-context" for v in data["items"])
    assert (
        extract_visuals(
            pdf, output, expected_sha256=sha(pdf), source_id="paper:test", selections=selections
        )
        == data
    )
