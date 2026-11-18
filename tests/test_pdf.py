from pathlib import Path

from paperflow.parsing.pdf_text import extract_pdf


def test_page_aware_extraction_and_truncation():
    pdf = Path(__file__).parent / "fixtures/sample.pdf"
    full = extract_pdf(pdf)
    assert full.quality == "good"
    assert full.pages[0]["page_number"] == 1
    assert not full.text_truncated and full.coverage == "fulltext"
    partial = extract_pdf(pdf, max_chars=100)
    assert partial.text_truncated and partial.coverage == "partial"
    assert len(partial.text) == 100
