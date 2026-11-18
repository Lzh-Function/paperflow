"""承認されたPDFだけを隔離領域に固定し、CLI用の視覚入力を作る。"""

import hashlib
import json
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

import pymupdf

from paperflow.agents.base import ProviderError

# このPCのCLIでは16MiBのPDFは視認でき、19MiBでは添付が除去された。
# APIの128MB上限とは別。base64化とCLI側の会話データの余裕を確保する。
CLAUDE_PDF_BYTES = 16 * 1024 * 1024
CLAUDE_PART_PAGES = 100


@dataclass
class PDFPart:
    path: Path
    first_page: int
    last_page: int


def prepare_claude_pdf(pdf, cwd):
    with pymupdf.open(pdf) as doc:
        count = len(doc)
    if pdf.stat().st_size <= CLAUDE_PDF_BYTES and count <= CLAUDE_PART_PAGES:
        return [PDFPart(pdf, 1, count)], "original"
    # 大きなPDFの画像再圧縮はC拡張で長時間GILを占めるため、GUIとは別プロセスで行う。
    from paperflow.agents.process import run_bounded

    executable = Path(sys.executable)
    if os.name == "nt":
        executable = executable.with_name("python.exe")
    raw = run_bounded(
        [str(executable), "-m", "paperflow.agents.pdf_prepare", str(pdf), str(cwd)],
        cwd=cwd,
        timeout=180,
        max_bytes=32768,
    )
    data = json.loads(raw)
    if data.get("error"):
        raise ProviderError(data["error"], data["message"])
    parts = []
    for item in data["parts"]:
        path = cwd / item["file"]
        if path.parent.resolve() != cwd.resolve() or not path.is_file():
            raise ProviderError("document_changed", "作業PDFの保存先が不正です")
        parts.append(PDFPart(path, item["first_page"], item["last_page"]))
    return parts, data["method"]


def claude_pdf_parts(pdf, cwd):
    """原本を変えずCLI用PDFを準備する。本文・ページは省略しない。"""
    working = pdf
    method = "original"
    with pymupdf.open(pdf) as source:
        boxes = [list(p.rect) for p in source]
        texts = [p.get_text(sort=True) for p in source]
        count = len(source)
        if pdf.stat().st_size > CLAUDE_PDF_BYTES:
            working = cwd / "packed.pdf"
            source.save(
                working,
                garbage=4,
                deflate=True,
                deflate_images=True,
                deflate_fonts=True,
                use_objstms=1,
            )
            method = "lossless_repack"
            if working.stat().st_size > CLAUDE_PDF_BYTES:
                if not hasattr(source, "rewrite_images"):
                    raise ProviderError(
                        "cli_input_limit", "大容量PDFの視覚入力準備には新しいPyMuPDFが必要です"
                    )
                working = cwd / "image-optimized.pdf"
                source.rewrite_images(dpi_threshold=180, dpi_target=150, quality=85, bitonal=False)
                source.save(
                    working,
                    garbage=4,
                    deflate=True,
                    deflate_images=True,
                    deflate_fonts=True,
                    use_objstms=1,
                )
                method = "images_150dpi_jpeg85"
    with pymupdf.open(working) as revised:
        if (
            len(revised) != count
            or boxes != [list(p.rect) for p in revised]
            or texts != [p.get_text(sort=True) for p in revised]
        ):
            raise ProviderError(
                "document_changed", "視覚入力用PDFの本文またはページ構成が変わったため送信しません"
            )
        parts = []
        for start in range(0, count, CLAUDE_PART_PAGES):
            end = min(count, start + CLAUDE_PART_PAGES)
            if count <= CLAUDE_PART_PAGES:
                path = working
            else:
                path = cwd / f"part-{start + 1:03d}-{end:03d}.pdf"
                with pymupdf.open() as part:
                    part.insert_pdf(revised, from_page=start, to_page=end - 1)
                    part.save(path, garbage=4, deflate=True, use_objstms=1)
            parts.append(PDFPart(path, start + 1, end))
    total = sum(p.path.stat().st_size for p in parts)
    if total > CLAUDE_PDF_BYTES:
        raise ProviderError(
            "cli_input_limit",
            "Claude CLIで添付が消えない容量へ収まりません。ページを省略せず、送信前に停止しました。Codex等の別経路で処理してください",
        )
    return parts, method


def snapshot(document, cwd, config):
    if document.page_count > config.analysis.visual_max_pages:
        raise ProviderError(
            "visual_input_limit",
            "PDFが視覚読解のページ上限を超えています。上限を見直してください。文字だけの解析には切り替えません",
        )
    if document.path.stat().st_size > config.analysis.visual_max_pdf_mb * 1024 * 1024:
        raise ProviderError("visual_input_limit", "PDFが視覚読解の容量上限を超えています")
    output = cwd / "input.pdf"
    shutil.copyfile(document.path, output)
    with output.open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    if actual != document.sha256:
        raise ProviderError("document_changed", "視覚読解用PDFが解析時のハッシュと一致しません")
    with pymupdf.open(output) as doc:
        if doc.needs_pass or len(doc) != document.page_count or not len(doc):
            raise ProviderError(
                "document_changed", "視覚読解用PDFのページ数・暗号化状態を確認できません"
            )
    return output


def page_images(pdf, cwd, *, max_bytes=128 * 1024 * 1024):
    result = []
    total = 0
    with pymupdf.open(pdf) as doc:
        for number, page in enumerate(doc, 1):
            path = cwd / f"page-{number:03d}.png"
            scale = min(2.0, 2000 / max(page.rect.width, page.rect.height))
            page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False).save(path)
            total += path.stat().st_size
            if total > max_bytes:
                raise ProviderError(
                    "visual_input_limit",
                    "全ページ画像の合計容量が上限を超えています。文字だけの解析には切り替えません",
                )
            result.append(path)
    return result
