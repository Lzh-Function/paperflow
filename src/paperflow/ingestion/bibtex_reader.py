import re
import unicodedata
from pathlib import Path
from urllib.parse import unquote

import bibtexparser
from bibtexparser.bparser import BibTexParser
from bibtexparser.customization import convert_to_unicode
from pyparsing import ParseBaseException

from paperflow.models import Bibliography


def normalized(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKC", text).casefold() if c.isalnum())


def clean(value: str) -> str:
    return " ".join(value.replace("{", "").replace("}", "").split())


def normalize_doi(value: str) -> str | None:
    value = unquote(value.strip()).lower()
    value = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", value)
    return value.rstrip(" .") or None


def identity_key(bib: Bibliography) -> str:
    return "|".join(
        [normalized(bib.title), normalized(bib.authors[0]) if bib.authors else "", bib.year]
    )


def read_bibtex(path: Path) -> list[Bibliography]:
    before = path.stat()
    text = path.read_text(encoding="utf-8-sig")
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise OSError("BibTeXが更新中です。エクスポート完了後に再実行してください")
    parser = BibTexParser(common_strings=True)
    parser.customization = convert_to_unicode
    # Fail closed on incomplete exports: never mark an entire library missing on a partial parse.
    parser.expect_multiple_parse = False
    try:
        database = bibtexparser.loads(text, parser=parser)
    except ParseBaseException as exc:
        raise ValueError("BibTeXを解析できません。未完了のエクスポートを確認してください") from exc
    if any(re.search(r"(?m)^\s*@\w+\s*[({]", comment) for comment in database.comments):
        raise ValueError("未完了または未対応のBibTeXエントリがあります。部分取り込みを中止します")
    entries = []
    for item in database.entries:
        if not item.get("title"):
            raise ValueError(f"タイトルのないBibTeXエントリ: {item.get('ID', '?')}")
        arxiv = item.get("eprint", "")
        if not arxiv and "arxiv.org/" in item.get("url", ""):
            arxiv = re.split(r"/(?:abs|pdf)/", item["url"])[-1]
        arxiv = re.sub(r"^(?:arxiv:|https?://arxiv.org/(?:abs|pdf)/)", "", arxiv, flags=re.I)
        arxiv = re.sub(r"(?:v\d+)?(?:\.pdf)?$", "", arxiv) or None
        entries.append(
            Bibliography(
                citekey=item["ID"],
                title=clean(item["title"]),
                authors=[clean(a) for a in re.split(r"\s+and\s+", item.get("author", "")) if a],
                year=clean(item.get("year", "")),
                doi=normalize_doi(item.get("doi", "")),
                arxiv_id=arxiv,
                abstract=clean(item.get("abstract", "")),
                url=item.get("url", ""),
                venue=clean(item.get("journal", item.get("booktitle", ""))),
                keywords=[clean(k) for k in re.split(r"[,;]", item.get("keywords", "")) if k],
                file=item.get("file", ""),
                no_external=item.get("no_external", "false").lower() in {"true", "yes", "1"},
            )
        )
    if text.strip() and not entries:
        raise ValueError("BibTeXエントリを取得できません。未完了のエクスポートを確認してください")
    return entries
