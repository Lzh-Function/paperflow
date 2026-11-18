from dataclasses import dataclass
from pathlib import Path

import pymupdf

PARSER_VERSION = f"pymupdf-{pymupdf.VersionBind}/v1"


@dataclass
class Extraction:
    pages: list[dict]
    quality: str
    text: str
    text_truncated: bool
    coverage: str


def extract_pdf(path: Path, max_chars: int = 3000000) -> Extraction:
    pages = []
    try:
        document = pymupdf.open(path)
    except RuntimeError as exc:
        raise ValueError("PDFを開けません。破損または未取得のPDFを確認してください") from exc
    with document as doc:
        if doc.needs_pass:
            raise ValueError("暗号化PDFは手動で確認してください")
        for number, page in enumerate(doc, 1):
            pages.append({"page_number": number, "text": page.get_text(sort=True)})
    text = "\n\n".join(f"[page {p['page_number']}]\n{p['text']}" for p in pages)
    plain = "".join(p["text"] for p in pages)
    tokens = plain.split()
    repeated = len(tokens) > 100 and len(set(tokens)) / len(tokens) < 0.03
    quality = (
        "low"
        if (
            len(plain.strip()) < 100
            or plain.count("\ufffd") > len(plain) * 0.02
            or repeated
            or any(not p["text"].strip() for p in pages)
        )
        else "good"
    )
    truncated = len(text) > max_chars
    return Extraction(
        pages,
        quality,
        text[:max_chars],
        truncated,
        "partial" if truncated or quality == "low" else "fulltext",
    )
