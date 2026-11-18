from dataclasses import replace
from pathlib import Path

import pytest

from paperflow.ingestion.bibtex_reader import read_bibtex
from paperflow.models import Bibliography
from paperflow.store import IdentityConflict, Store


def test_unicode_strings_braces_doi():
    entry = read_bibtex(Path(__file__).parent / "fixtures/sample.bib")[0]
    assert entry.title == "Synthetic Molecular Benchmark"
    assert entry.authors == ["山田, 太郎", "Müller, Anna"]
    assert entry.doi == "10.9999/fixture"
    assert entry.venue == "Synthetic Research"


def test_identity_doi_added_citekey_changed_title_corrected(tmp_path):
    with Store(tmp_path / "state.db") as store, store.transaction():
        bib = Bibliography("old", "A molecular paper", ["山田"], "2026")
        first = store.upsert_bib(bib)
        assert store.upsert_bib(replace(bib, citekey="new", doi="10.1234/x")) == first
        assert (
            store.upsert_bib(replace(bib, citekey="new", title="Corrected", doi="10.1234/x"))
            == first
        )
        assert len(store.rows("SELECT * FROM papers")) == 1


def test_same_title_different_authors_and_identifier_conflicts(tmp_path):
    with Store(tmp_path / "state.db") as store, store.transaction():
        a = Bibliography("A", "Same title", ["Author A"], "2026", doi="10.1/a")
        b = Bibliography("B", "Same title", ["Author B"], "2026", doi="10.1/b")
        assert store.upsert_bib(a) != store.upsert_bib(b)
        with pytest.raises(IdentityConflict):
            store.upsert_bib(replace(a, doi="10.1/b"))


def test_duplicate_doi_preserves_id(tmp_path):
    with Store(tmp_path / "state.db") as store, store.transaction():
        a = Bibliography("a", "One", doi="10.1/a")
        assert store.upsert_bib(a) == store.upsert_bib(replace(a, citekey="b"))


def test_arxiv_version_normalized(tmp_path):
    path = tmp_path / "lib.bib"
    path.write_text("@article{x,title={Test},eprint={2401.12345v3}}", encoding="utf-8")
    assert read_bibtex(path)[0].arxiv_id == "2401.12345"


def test_partial_bib_export_never_silently_drops_entries(tmp_path):
    path = tmp_path / "library.bib"
    path.write_text("@article{a,title={Complete}}\n@article{b,title={Incomplete}", encoding="utf-8")
    with pytest.raises(ValueError, match="部分取り込み"):
        read_bibtex(path)
