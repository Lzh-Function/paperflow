import json
from dataclasses import asdict, dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Any


def digest(value: Any) -> str:
    if isinstance(value, bytes):
        return sha256(value).hexdigest()
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


@dataclass
class Bibliography:
    citekey: str
    title: str
    authors: list[str] = field(default_factory=list)
    year: str = ""
    doi: str | None = None
    arxiv_id: str | None = None
    abstract: str = ""
    url: str = ""
    venue: str = ""
    keywords: list[str] = field(default_factory=list)
    file: str = ""
    no_external: bool = False

    def as_dict(self):
        return asdict(self)


@dataclass
class PDFCandidate:
    path: Path
    size: int
    mtime_ns: int
    stable_count: int = 1


@dataclass
class Match:
    status: str
    candidate: PDFCandidate | None = None
    score: float = 0.0
    candidates: list[str] = field(default_factory=list)
    reason: str | None = None
