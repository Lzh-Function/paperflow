import pytest

from paperflow.ingestion.matcher import match_pdf
from paperflow.models import Bibliography, PDFCandidate


def test_paperpile_prefix_unicode_and_explicit_paths(tmp_path):
    path = tmp_path / "山田_2026_A sufficiently distinctive molecular title.pdf"
    c = PDFCandidate(path, 100, 1)
    bib = Bibliography("a", "A sufficiently distinctive molecular title")
    assert match_pdf(bib, [c], tmp_path).candidate == c
    bib.title = "Unrelated"
    bib.file = ":" + str(path) + ":PDF"
    assert match_pdf(bib, [c], tmp_path).score == 1


def test_ambiguous_and_supplement(tmp_path):
    bib = Bibliography("a", "A sufficiently distinctive molecular title")
    cs = [
        PDFCandidate(tmp_path / (prefix + bib.title + ".pdf"), 10, 1) for prefix in ["", "Author_"]
    ]
    assert match_pdf(bib, cs, tmp_path).status == "needs_review"
    assert match_pdf(bib, [], tmp_path).status == "metadata_only"
    cs.append(PDFCandidate(tmp_path / (bib.title + " supplement.pdf"), 10, 1))
    bib.file = ";".join(str(c.path) for c in [cs[0], cs[2]])
    assert match_pdf(bib, cs, tmp_path).candidate == cs[0]


def test_filename_never_creates_metadata(small_config):
    from paperflow.pipeline import Pipeline

    small_config.source.bibtex_file = None
    result = Pipeline(small_config).run(scan_only=True, dry_run=True)
    assert result["papers"] == 0
    assert result["pdf_candidates"] == 1


FULL_TITLE = (
    "Synthetic molecular generation with graph neural networks and uncertainty estimates "
    "across diverse chemical benchmarks"
)
CUT_TITLE = "Synthetic molecular generation with ... across diverse chemical benchmarks"


def candidate(root, name, folder="All Papers"):
    return PDFCandidate(root / folder / (name + ".pdf"), 100, 1)


def bibliography(**changes):
    from dataclasses import replace

    bib = Bibliography("fixture", FULL_TITLE, ["Yamada, Taro"], "2026", venue="J. Test.")
    return replace(bib, **changes)


@pytest.mark.parametrize("ellipsis", [" ... ", "...", "…"])
def test_truncated_paperpile_name_with_author_year(tmp_path, ellipsis):
    title = CUT_TITLE.replace(" ... ", ellipsis)
    pdf = candidate(tmp_path, f"J Test_Yamada_Suzuki_2026_{title}")
    result = match_pdf(bibliography(), [pdf], tmp_path)
    assert result.candidate == pdf
    assert result.reason == "truncated_title_author_year"


@pytest.mark.parametrize(
    "changes",
    [
        {"authors": ["Other, Taro"]},
        {"year": "2025"},
        {"authors": []},
        {"year": ""},
    ],
)
def test_truncation_without_supporting_metadata_is_not_adopted(tmp_path, changes):
    pdf = candidate(tmp_path, f"J Test_Yamada_Suzuki_2026_{CUT_TITLE}")
    result = match_pdf(bibliography(**changes), [pdf], tmp_path)
    assert result.status == "needs_review"
    assert result.candidate is None


@pytest.mark.parametrize(
    "cut",
    [
        CUT_TITLE.replace("Synthetic", "Other"),
        CUT_TITLE.replace("chemical", "biological"),
        "Synthetic ... benchmarks",
    ],
)
def test_truncation_requires_long_exact_prefix_and_suffix(tmp_path, cut):
    pdf = candidate(tmp_path, f"J Test_Yamada_Suzuki_2026_{cut}")
    assert match_pdf(bibliography(), [pdf], tmp_path).candidate is None


def test_two_plausible_truncated_files_remain_ambiguous(tmp_path):
    pdfs = [
        candidate(tmp_path, f"J {journal}_Yamada_Suzuki_2026_{CUT_TITLE}")
        for journal in ("Alpha", "Beta")
    ]
    result = match_pdf(bibliography(venue=""), pdfs, tmp_path)
    assert result.status == "needs_review" and result.reason == "ambiguous_candidates"


@pytest.mark.parametrize("secondary", ["Starred Papers", "Trashed Papers"])
@pytest.mark.parametrize("reverse", [False, True])
def test_all_papers_is_primary_over_secondary_same_name(tmp_path, secondary, reverse):
    name = f"J Test_Yamada_Suzuki_2026_{FULL_TITLE}"
    main = candidate(tmp_path, name)
    other = candidate(tmp_path, name, secondary)
    pdfs = [main, other] if not reverse else [other, main]
    assert match_pdf(bibliography(), pdfs, tmp_path).candidate == main


def test_trashed_only_is_not_auto_adopted(tmp_path):
    pdf = candidate(tmp_path, f"J Test_Yamada_Suzuki_2026_{FULL_TITLE}", "Trashed Papers")
    assert match_pdf(bibliography(), [pdf], tmp_path).status == "metadata_only"
    result = match_pdf(bibliography(file=str(pdf.path)), [pdf], tmp_path)
    assert result.status == "needs_review" and result.reason == "trashed_reference"


def test_starred_only_can_be_used_without_primary_copy(tmp_path):
    pdf = candidate(tmp_path, f"J Test_Yamada_Suzuki_2026_{FULL_TITLE}", "Starred Papers")
    assert match_pdf(bibliography(), [pdf], tmp_path).candidate == pdf


def test_different_primary_files_are_not_collapsed_by_basename(tmp_path):
    name = f"J Test_Yamada_Suzuki_2026_{FULL_TITLE}"
    pdfs = [candidate(tmp_path, name, f"All Papers/{sub}") for sub in ("a", "b")]
    assert match_pdf(bibliography(), pdfs, tmp_path).status == "needs_review"


@pytest.mark.parametrize(
    "year,venue,index", [("2026", "J. Test.", 0), ("2025", "arXiv [cs.LG]", 1)]
)
def test_preprint_and_published_version_keep_separate_pdfs(tmp_path, year, venue, index):
    pdfs = [
        candidate(tmp_path, f"J Test_Yamada_Suzuki_2026_{FULL_TITLE}"),
        candidate(tmp_path, f"arXiv [cs LG]_Yamada_Suzuki_2025_{FULL_TITLE}"),
    ]
    assert match_pdf(bibliography(year=year, venue=venue), pdfs, tmp_path).candidate == pdfs[index]


@pytest.mark.parametrize(
    "venue,doi,index", [("J. Test.", None, 0), ("", "10.48550/arxiv.2601.12345", 1)]
)
def test_same_year_versions_use_venue_or_arxiv_doi(tmp_path, venue, doi, index):
    pdfs = [
        candidate(tmp_path, f"J Test_Yamada_Suzuki_2026_{FULL_TITLE}"),
        candidate(tmp_path, f"arXiv [cs LG]_Yamada_Suzuki_2026_{FULL_TITLE}"),
    ]
    assert match_pdf(bibliography(venue=venue, doi=doi), pdfs, tmp_path).candidate == pdfs[index]


def test_published_truncated_version_beats_exact_title_of_wrong_year(tmp_path):
    published = candidate(tmp_path, f"J Test_Yamada_Suzuki_2026_{CUT_TITLE}")
    preprint = candidate(tmp_path, f"arXiv [cs LG]_Yamada_Suzuki_2025_{FULL_TITLE}")
    assert match_pdf(bibliography(), [preprint, published], tmp_path).candidate == published


def test_identical_title_with_wrong_author_or_year_is_held(tmp_path):
    wrong = candidate(tmp_path, f"J Test_Other_Suzuki_2025_{FULL_TITLE}")
    result = match_pdf(bibliography(), [wrong], tmp_path)
    assert result.status == "needs_review" and result.reason == "bibliography_mismatch"


def test_versions_with_insufficient_bibliography_remain_ambiguous(tmp_path):
    pdfs = [
        candidate(tmp_path, f"J Test_Yamada_Suzuki_{year}_{FULL_TITLE}") for year in (2025, 2026)
    ]
    assert match_pdf(bibliography(year=""), pdfs, tmp_path).status == "needs_review"


@pytest.mark.parametrize("reference", ["absolute", "relative", "uri", "jabref"])
def test_explicit_reference_forms(tmp_path, reference):
    pdf = candidate(tmp_path, "Unrelated fixture title")
    value = {
        "absolute": str(pdf.path),
        "relative": pdf.path.relative_to(tmp_path).as_posix(),
        "uri": pdf.path.as_uri(),
        "jabref": f":{pdf.path}:PDF",
    }[reference]
    assert match_pdf(bibliography(file=value), [pdf], tmp_path).score == 1


def test_absolute_reference_outside_source_cannot_match_relative_suffix(tmp_path):
    pdf = candidate(tmp_path, "Unrelated fixture title")
    outside = tmp_path.parent / "outside" / pdf.path.relative_to(tmp_path)
    result = match_pdf(bibliography(file=str(outside)), [pdf], tmp_path)
    assert result.score != 1 and result.candidate is None


def test_matching_never_opens_pdf(tmp_path, monkeypatch):
    from pathlib import Path

    pdf = candidate(tmp_path, f"J Test_Yamada_Suzuki_2026_{CUT_TITLE}")

    def forbidden(*args, **kwargs):
        raise AssertionError("照合中に原本を開いてはいけません")

    monkeypatch.setattr(Path, "open", forbidden)
    assert match_pdf(bibliography(), [pdf], tmp_path).candidate == pdf


@pytest.mark.parametrize(
    "author,filename_author",
    [
        ("Synthetic Team", "Synthetic Team"),
        ("Example Consortium", "Example Consortium"),
        ("Taro Yamada", "Yamada"),
    ],
)
def test_corporate_and_uncommaed_authors(tmp_path, author, filename_author):
    pdf = candidate(tmp_path, f"J Test_{filename_author}_Suzuki_2026_{CUT_TITLE}")
    assert match_pdf(bibliography(authors=[author]), [pdf], tmp_path).candidate == pdf


def test_corporate_authors_are_not_matched_by_generic_team_word(tmp_path):
    pdf = candidate(tmp_path, f"J Test_Other Team_Suzuki_2026_{FULL_TITLE}")
    assert (
        match_pdf(bibliography(authors=["Synthetic Team"]), [pdf], tmp_path).status
        == "needs_review"
    )


def test_explicit_reference_to_starred_is_not_replaced_by_unreferenced_primary(tmp_path):
    name = f"J Test_Yamada_Suzuki_2026_{FULL_TITLE}"
    main = candidate(tmp_path, name)
    starred = candidate(tmp_path, name, "Starred Papers")
    result = match_pdf(bibliography(file=str(starred.path)), [main, starred], tmp_path)
    assert result.candidate == starred and result.reason == "explicit_reference"


def test_file_uri_preserves_encoded_hash_and_unicode(tmp_path):
    pdf = candidate(tmp_path, "日本語 C# fixture")
    result = match_pdf(bibliography(file=pdf.path.as_uri()), [pdf], tmp_path)
    assert result.candidate == pdf and result.reason == "explicit_reference"
