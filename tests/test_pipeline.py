import pymupdf
import pytest
from conftest import tree

from paperflow.agents.base import ProviderError
from paperflow.agents.fake import FakeProvider
from paperflow.pipeline import Pipeline
from paperflow.store import Store


def settle(config):
    Pipeline(config).run(scan_only=True)
    return Pipeline(config).run()


def test_ten_papers_idempotency_and_no_source_mutation(config):
    original = tree(config.source.paperpile_dir)
    assert settle(config)["exported"] == 20
    notes = tree(config.destination.vault_dir)
    result = Pipeline(config).run()
    assert result["analyzed"] == 0 and result["exported"] == 0
    assert tree(config.destination.vault_dir) == notes
    assert tree(config.source.paperpile_dir) == original
    assert not list(config.destination.vault_dir.rglob("*.pdf"))
    with Store(config.storage.database, readonly=True) as store:
        assert len(store.rows("SELECT * FROM analysis_runs")) == 10
        assert {r["coverage"] for r in store.rows("SELECT coverage FROM analysis_runs")} == {
            "fulltext"
        }
        assert len(store.rows("SELECT * FROM papers")) == 10


@pytest.mark.parametrize("with_db", [False, True])
def test_dry_run_no_disk_db_or_provider_calls(config, monkeypatch, with_db):
    if with_db:
        settle(config)
    before = tree(config.source.paperpile_dir.parent)

    def forbidden(*args, **kwargs):
        raise AssertionError("外部プロセスを呼び出しました")

    monkeypatch.setattr("subprocess.Popen", forbidden)
    monkeypatch.setattr(FakeProvider, "analyze", forbidden)
    for scan_only in (True, False):
        Pipeline(config).run(dry_run=True, scan_only=scan_only, provider_name="claude")
    assert tree(config.source.paperpile_dir.parent) == before


def test_unstable_then_zero_and_arriving_pdf(small_config):
    config = small_config
    pdf = next(config.source.paperpile_dir.glob("*.pdf"))
    content = pdf.read_bytes()
    pdf.unlink()
    assert Pipeline(config).run()["exported"] == 2  # abstract-only while no PDF exists
    pdf.write_bytes(b"")
    assert Pipeline(config).run()["analyzed"] == 0
    pdf.write_bytes(content)
    assert Pipeline(config).run()["analyzed"] == 0
    assert Pipeline(config).run()["analyzed"] == 1


def test_removed_bib_entries_retained(small_config):
    settle(small_config)
    small_config.source.bibtex_file.write_text("", encoding="utf-8")
    Pipeline(small_config).run(scan_only=True)
    with Store(small_config.storage.database, readonly=True) as store:
        assert store.rows("SELECT source_present FROM papers")[0]["source_present"] == 0
    assert len(list(small_config.destination.vault_dir.rglob("*.md"))) == 2


def test_bibliography_change_invalidates_analysis_preserves_paper(small_config):
    settle(small_config)
    paper = next((small_config.destination.vault_dir / "PaperFlow/Papers").glob("*.md"))
    paper.write_text(paper.read_text(encoding="utf-8") + "\n手動メモ\n", encoding="utf-8")
    original = paper.read_bytes()
    bib = small_config.source.bibtex_file
    bib.write_text(
        bib.read_text().replace("experiment 00", "experiment 00 corrected"), encoding="utf-8"
    )
    assert Pipeline(small_config).run()["analyzed"] == 1
    assert paper.read_bytes() == original


def test_edited_analysis_conflict_and_local_recovery(small_config):
    settle(small_config)
    analysis = next((small_config.destination.vault_dir / "PaperFlow/Analysis").glob("*.md"))
    analysis.write_text(analysis.read_text(encoding="utf-8") + "\n人手編集\n", encoding="utf-8")
    original = analysis.read_bytes()
    result = Pipeline(small_config).run(force=True)
    assert result["failures"][0]["category"] == "WriteConflict"
    assert analysis.read_bytes() == original
    assert list((small_config.storage.data_dir / "conflicts").glob("*.md"))


def test_external_default_never_receives_fulltext_or_file_paths(small_config):
    class Capture(FakeProvider):
        def analyze(self, request):
            assert request.coverage == "abstract"
            assert request.pages_available == []
            assert "file" not in request.metadata
            assert "[page" not in request.text
            return super().analyze(request)

    Pipeline(small_config).run(scan_only=True)
    assert not Pipeline(small_config, Capture()).run(provider_name="claude")["failures"]


def test_no_external_blocks_before_provider(small_config):
    class Forbidden(FakeProvider):
        def analyze(self, request):
            pytest.fail("外部送信禁止文献のProviderが呼ばれました")

    bib = small_config.source.bibtex_file
    bib.write_text(
        bib.read_text().replace("year={2026}", "no_external={true},year={2026}"), encoding="utf-8"
    )
    Pipeline(small_config).run(scan_only=True)
    result = Pipeline(small_config, Forbidden()).run(provider_name="codex")
    assert result["failures"][0]["category"] == "no_external"


def test_retry_transient_then_success(small_config):
    class Flaky(FakeProvider):
        calls = 0

        def analyze(self, request):
            self.calls += 1
            if self.calls == 1:
                raise ProviderError("rate_limit", "試験用429", True)
            return super().analyze(request)

    provider = Flaky()
    pipeline = Pipeline(small_config, provider)
    pipeline.run(scan_only=True)
    assert pipeline.run()["failures"]
    assert pipeline.run(retry=True)["analyzed"] == 1
    with Store(small_config.storage.database, readonly=True) as store:
        assert store.rows("SELECT state FROM papers")[0]["state"] == "exported"


def test_corrupt_and_image_only_pdf(small_config):
    pdf = next(small_config.source.paperpile_dir.glob("*.pdf"))
    pdf.write_bytes(b"not a PDF")
    result = settle(small_config)
    assert result["failures"]
    pdf.unlink()
    doc = pymupdf.open()
    doc.new_page()
    doc.save(pdf)
    doc.close()
    Pipeline(small_config).run(scan_only=True)
    result = Pipeline(small_config).run()
    assert not result["failures"]  # falls back to abstract; never claims fulltext
    with Store(small_config.storage.database, readonly=True) as store:
        assert (
            store.latest_analysis(store.rows("SELECT paper_id FROM papers")[0]["paper_id"])[
                "coverage"
            ]
            == "abstract"
        )


def test_resume_after_writer_crash_uses_analysis_cache(small_config, monkeypatch):
    Pipeline(small_config).run(scan_only=True)
    from paperflow.obsidian.writer import SafeWriter

    original = SafeWriter.write
    calls = [0]

    def crash(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        calls[0] += 1
        if calls[0] == 2:
            raise KeyboardInterrupt
        return result

    monkeypatch.setattr(SafeWriter, "write", crash)
    with pytest.raises(KeyboardInterrupt):
        Pipeline(small_config).run()
    monkeypatch.setattr(SafeWriter, "write", original)
    result = Pipeline(small_config).run()
    assert not result["failures"] and result["analyzed"] == 0
    assert len(list(small_config.destination.vault_dir.rglob("*.md"))) == 2


def test_drive_disconnect_does_not_create_vault(small_config):
    vault = small_config.destination.vault_dir
    (vault / ".llm-wiki-root").unlink()
    vault.rmdir()
    with pytest.raises(FileNotFoundError):
        Pipeline(small_config).run()
    assert not vault.exists()


def test_source_disconnect_and_reconnect(small_config):
    settle(small_config)
    source = small_config.source.paperpile_dir
    hidden = source.with_name("temporarily-disconnected")
    source.rename(hidden)
    try:
        with pytest.raises(FileNotFoundError):
            Pipeline(small_config).run()
    finally:
        hidden.rename(source)
    assert not Pipeline(small_config).run()["failures"]


def test_same_size_mtime_pdf_replacement_invalidates(small_config):
    import os

    settle(small_config)
    pdf = next(small_config.source.paperpile_dir.glob("*.pdf"))
    with pymupdf.open(pdf) as doc:
        doc.set_metadata({"title": "Changed fixture"})
        replacement = doc.tobytes()
    stat = pdf.stat()
    pdf.write_bytes(replacement)
    os.utime(pdf, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    Pipeline(small_config).run(scan_only=True)
    assert Pipeline(small_config).run()["analyzed"] == 1


def test_retry_cap_and_nonretryable(small_config):
    class Failure(FakeProvider):
        def analyze(self, request):
            raise ProviderError("rate_limit", "synthetic", True)

    small_config.analysis.max_retries = 1
    pipeline = Pipeline(small_config, Failure())
    pipeline.run(scan_only=True)
    pipeline.run()
    pipeline.run(retry=True)
    assert pipeline.run(retry=True)["analyzed"] == 0
    with Store(small_config.storage.database, readonly=True) as store:
        row = store.rows("SELECT * FROM papers")[0]
        assert row["retry_count"] == 2 and row["state"] == "failed"


def test_lost_db_does_not_duplicate_vault(small_config):
    from paperflow.config import SettingsError

    settle(small_config)
    small_config.storage.database.unlink()
    with pytest.raises(SettingsError, match="DBが空"):
        Pipeline(small_config).run()
    assert len(list(small_config.destination.vault_dir.rglob("*.md"))) == 2


def test_scan_invalidates_report_until_reanalysis(small_config):
    from paperflow.reporting.reports import report

    settle(small_config)
    bib = small_config.source.bibtex_file
    bib.write_text(
        bib.read_text().replace("experiment 00", "changed experiment 00"), encoding="utf-8"
    )
    Pipeline(small_config).run(scan_only=True)
    with Store(small_config.storage.database, readonly=True) as store:
        assert "[[PaperFlow/Analysis/" not in report(store, 1)


def test_research_context_change_invalidates_analysis(small_config):
    settle(small_config)
    small_config.analysis.research_context = "ALifeとLLM社会シミュレーションに関心。"

    class Capture(FakeProvider):
        def analyze(self, request):
            assert request.research_context == small_config.analysis.research_context
            return super().analyze(request)

    assert Pipeline(small_config, Capture()).run()["analyzed"] == 1


def test_review_report_explains_filename_metadata_conflict(small_config):
    from paperflow.ingestion.bibtex_reader import read_bibtex
    from paperflow.reporting.reports import review_report

    bib = read_bibtex(small_config.source.bibtex_file)[0]
    pdf = next(small_config.source.paperpile_dir.glob("*.pdf"))
    pdf.rename(pdf.with_name(f"J Test_Other_Suzuki_2026_{bib.title}.pdf"))
    Pipeline(small_config).run(scan_only=True)
    with Store(small_config.storage.database, readonly=True) as store:
        text = review_report(store)
    assert "needs_review" in text
    assert "著者・年・版が書誌と矛盾する" in text
