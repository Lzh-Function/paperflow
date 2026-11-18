"""PCのパスを含まない、解析とバッチの識別。"""

from paperflow.agents.schema import PROMPT, VISUAL_PROMPT, VISUAL_SCHEMA
from paperflow.models import digest
from paperflow.parsing.pdf_text import PARSER_VERSION

PROFILE_FIELDS = (
    "research_context",
    "policy",
    "require_visual_reading",
    "language",
    "max_chars",
    "visual_max_pages",
    "visual_max_pdf_mb",
    "visual_max_image_mb",
    "prompt_version",
)


def bibliography(metadata):
    return {k: v for k, v in metadata.items() if k != "file"}


def bib_key(metadata):
    return digest(bibliography(metadata))


def profile(config, provider=None, model=None):
    return dict(
        analysis={k: getattr(config.analysis, k) for k in PROFILE_FIELDS},
        provider=provider or config.analysis.default_provider,
        model=config.analysis.model if model is None else model,
        taxonomy=config.taxonomy(),
        visuals=config.destination.visual_max_items,
        parser=PARSER_VERSION,
        prompt=digest(PROMPT + VISUAL_PROMPT),
        schema=digest(VISUAL_SCHEMA),
    )


def analysis_key(config, metadata, pdf_sha256, request, provider):
    data = dict(
        bib=bib_key(metadata),
        pdf=pdf_sha256,
        profile=profile(config, provider),
        text=digest(request.text),
        coverage=request.coverage,
        pages=request.pages_available,
        visual_pages=request.document.page_count if request.document else 0,
    )
    return digest(data), data
