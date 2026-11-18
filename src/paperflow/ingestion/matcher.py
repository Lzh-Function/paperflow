"""原本を開かず、タイトル・Paperpileの命名メタデータからPDFを照合する。"""

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path
from urllib.parse import unquote, urlsplit

from paperflow.ingestion.bibtex_reader import normalized
from paperflow.models import Bibliography, Match, PDFCandidate

MATCH_REASONS = {
    "explicit_reference": "BibTeXの明示参照",
    "exact_title": "タイトル一致（既知の著者・年・版の矛盾なし）",
    "truncated_title_author_year": "省略名前後の一致＋第一著者・出版年の一致",
    "fuzzy_title": "高信頼の唯一の類似タイトル候補",
    "no_candidate": "該当候補なし",
    "ambiguous_candidates": "同じ書誌に複数の候補が残る",
    "bibliography_mismatch": "タイトル候補の著者・年・版が書誌と矛盾する",
    "truncation_metadata_missing": "省略名の確認に必要な著者・年が不足する",
    "low_confidence": "類似度または候補間の差が不足する",
    "trashed_reference": "明示参照先がTrashed Papers内にある",
}


@dataclass(frozen=True)
class FilenameMetadata:
    title: str
    title_norm: str
    first_author: str = ""
    year: str = ""
    venue: str = ""

    @property
    def truncated(self) -> bool:
        return bool(re.search(r"\.{3,}|…", self.title))


@lru_cache(maxsize=4096)
def filename_metadata(stem: str) -> FilenameMetadata:
    # Recognize only the known underscore-based Paperpile naming convention.
    match = re.fullmatch(r"(?P<prefix>.*?)_(?P<year>(?:19|20)\d{2})_(?P<title>.+)", stem)
    if match:
        parts = match["prefix"].split("_")
        author = parts[-2] if len(parts) >= 3 else parts[-1]
        venue = parts[0] if len(parts) >= 3 else ""
        title = match["title"]
        return FilenameMetadata(
            title, normalized(title), normalized(author), match["year"], normalized(venue)
        )
    # Legacy filenames can still use title matching, but supply no author/year evidence.
    title = re.split(r"(?:19|20)\d{2}[_ -]+", stem, maxsplit=1)[-1]
    return FilenameMetadata(title, normalized(title))


def route(candidate: PDFCandidate, root: Path) -> str:
    parts = candidate.path.relative_to(root).parts
    folder = parts[0] if len(parts) > 1 else root.name
    return folder.casefold()


def primary_candidates(candidates: list[PDFCandidate], root: Path) -> list[PDFCandidate]:
    # All Papers is the primary folder. Starred is a secondary view, never a tie breaker
    # between two different files in All Papers. Trashed entries are not auto-adopted.
    all_names = {c.path.name.casefold() for c in candidates if route(c, root) == "all papers"}
    return [
        c
        for c in candidates
        if route(c, root) != "trashed papers"
        and not (route(c, root) == "starred papers" and c.path.name.casefold() in all_names)
    ]


def reference_paths(value: str) -> set[str]:
    paths = set()
    for item in value.split(";"):
        ref = re.sub(r":PDF$", "", item.strip(), flags=re.I)
        if ref.startswith(":"):
            ref = ref[1:]
        if ref.lower().startswith("file:"):
            uri = urlsplit(ref)
            ref = unquote(uri.path)
            if uri.netloc and uri.netloc.casefold() != "localhost":
                ref = "//" + uri.netloc + ref
            if re.match(r"^/[A-Za-z]:/", ref):
                ref = ref[1:]
        else:
            ref = unquote(ref)
        if ref:
            paths.add(ref.replace("\\", "/").casefold())
    return paths


def author_names(bib: Bibliography) -> set[str]:
    if not bib.authors:
        return set()
    first = bib.authors[0].strip()
    if not first:
        return set()
    if "," in first:
        return {normalized(first.split(",", 1)[0])}
    # Corporate authors retain their complete identity ("Example Team" is not "Team").
    if re.search(
        r"\b(team|consortium|collaboration|group|institute|laboratory|committee)$", first, re.I
    ):
        return {normalized(first)}
    return {normalized(first), normalized(first.split()[-1])}


def arxiv_bibliography(bib: Bibliography) -> bool:
    return (
        normalized(bib.venue).startswith("arxiv")
        or bool(bib.doi and bib.doi.startswith("10.48550/arxiv."))
        or bool(bib.arxiv_id and not bib.venue)
    )


def metadata_conflicts(bib: Bibliography, info: FilenameMetadata) -> bool:
    authors = author_names(bib)
    if authors and info.first_author and info.first_author not in authors:
        return True
    if bib.year and info.year and bib.year != info.year:
        return True
    if info.venue:
        if arxiv_bibliography(bib) and not info.venue.startswith("arxiv"):
            return True
        if bib.venue and not arxiv_bibliography(bib) and info.venue.startswith("arxiv"):
            return True
    return False


def truncated_title_matches(title: str, info: FilenameMetadata) -> bool:
    parts = re.split(r"\.{3,}|…", info.title)
    if len(parts) != 2:
        return False
    prefix, suffix = map(normalized, parts)
    # Both visible ends must match exactly; never lower the fuzzy threshold for elisions.
    return (
        len(prefix) >= 12
        and len(suffix) >= 12
        and len(prefix) + len(suffix) >= 50
        and len(prefix) + len(suffix) < len(title)
        and (len(prefix) + len(suffix)) / len(title) >= 0.5
        and title.startswith(prefix)
        and title.endswith(suffix)
    )


def review(candidates: list[PDFCandidate], reason: str) -> Match:
    return Match("needs_review", candidates=[str(c.path) for c in candidates[:5]], reason=reason)


def choose(candidates: list[PDFCandidate], score: float, reason: str) -> Match:
    if len(candidates) != 1:
        return review(candidates, "ambiguous_candidates")
    return Match("pdf_matched", candidates[0], score, reason=reason)


def match_pdf(bib: Bibliography, candidates: list[PDFCandidate], root: Path) -> Match:
    refs = reference_paths(bib.file) if bib.file else set()
    eligible = primary_candidates(candidates, root)
    if refs:

        def referenced(c):
            return bool(
                refs
                & {
                    str(c.path).replace("\\", "/").casefold(),
                    c.path.relative_to(root).as_posix().casefold(),
                }
            )

        explicit = primary_candidates([c for c in candidates if referenced(c)], root)
        if explicit:
            body = [
                c
                for c in explicit
                if not re.search(r"supplement|supporting|\bSI\b", c.path.stem, re.I)
            ]
            return choose(body or explicit, 1.0, "explicit_reference")
        if any(referenced(c) and route(c, root) == "trashed papers" for c in candidates):
            return review([c for c in candidates if referenced(c)], "trashed_reference")

    title = normalized(bib.title)
    primary = [c for c in eligible if not re.search(r"supplement|supporting", c.path.stem, re.I)]
    strong = []
    for candidate in primary:
        info = filename_metadata(candidate.path.stem)
        stem = normalized(candidate.path.stem)
        exact = info.title_norm == title or (len(title) >= 15 and stem.endswith(title))
        truncated = info.truncated and truncated_title_matches(title, info)
        if exact or truncated:
            strong.append((candidate, info, not exact))
    if strong:
        compatible = [
            (c, info, cut) for c, info, cut in strong if not metadata_conflicts(bib, info)
        ]
        if not compatible:
            return review([c for c, _, _ in strong], "bibliography_mismatch")
        if len(compatible) > 1 and bib.venue:
            venue_matches = [
                entry for entry in compatible if entry[1].venue == normalized(bib.venue)
            ]
            if venue_matches:
                compatible = venue_matches
        if len(compatible) != 1:
            return review([c for c, _, _ in compatible], "ambiguous_candidates")
        candidate, info, cut = compatible[0]
        if cut and not (
            bib.year and info.year == bib.year and info.first_author in author_names(bib)
        ):
            return review([candidate], "truncation_metadata_missing")
        return choose(
            [candidate],
            0.94 if cut else 0.95,
            "truncated_title_author_year" if cut else "exact_title",
        )

    scores = []
    for candidate in primary:
        info = filename_metadata(candidate.path.stem)
        score = SequenceMatcher(None, title, info.title_norm).ratio() * 0.9
        if score >= 0.65:
            scores.append((score, candidate, info))
    scores.sort(key=lambda x: x[0], reverse=True)
    if not scores:
        return Match("metadata_only", reason="no_candidate")
    plausible = [
        (score, c)
        for score, c, info in scores
        if not info.truncated and not metadata_conflicts(bib, info)
    ]
    if not plausible:
        return review([c for _, c, _ in scores], "low_confidence")
    if plausible[0][0] < 0.88 or (len(plausible) > 1 and plausible[0][0] - plausible[1][0] < 0.08):
        return review([c for _, c in plausible], "low_confidence")
    return choose([plausible[0][1]], plausible[0][0], "fuzzy_title")
