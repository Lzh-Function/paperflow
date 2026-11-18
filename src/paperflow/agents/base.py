from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass
class PDFDocument:
    path: Path
    sha256: str
    page_count: int
    max_selected_visuals: int = 3


@dataclass
class AnalysisRequest:
    paper_id: str
    metadata: dict
    text: str
    coverage: str
    pages_available: list[int]
    taxonomy: list[str]
    text_truncated: bool = False
    extraction_quality: str = "not_extracted"
    research_context: str = ""
    document: PDFDocument | None = None


@dataclass
class AnalysisResult:
    data: dict
    provider: str
    model: str | None = None
    raw_output_path: Path | None = None
    elapsed_seconds: float = 0.0
    usage: dict | None = None


class AgentProvider(Protocol):
    name: str

    def analyze(self, request: AnalysisRequest) -> AnalysisResult: ...


class ProviderError(RuntimeError):
    def __init__(self, category: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.category = category
        self.retryable = retryable


def authorize_request(config, request: AnalysisRequest):
    if request.metadata.get("no_external"):
        raise ProviderError("no_external", "この文献は外部送信禁止です")
    if (request.coverage != "abstract" or request.document is not None) and not (
        config.analysis.policy == "fulltext_external" and config.analysis.allow_external_fulltext
    ):
        raise ProviderError("fulltext_permission", "全文送信の明示許可がありません")
