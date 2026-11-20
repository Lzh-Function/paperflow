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

# 図表番号の表現だけを拡張した旧契約。モデル・本文・語彙の変更には使わない。
LEGACY_VISUAL_CONTRACT = {
    "prompt": "226372f7bbd0340f84308012504caa9f90a20d487aa1286f87e8baceb7fa184e",
    "schema": "702a9479555b0f85a7fc818374409aeab49e4dc5536f4f072f89413615a7ae2e",
}
NUMBERED_VISUAL_CONTRACT = {
    "prompt": "83486c2046e29246854bc6c4718a461f302ea022f8dac1f1a3cc0e3de0f5018b",
    "schema": "8dc7a59d990875fb06ef5e961cc7b83a2dd88db8e964753d6ddef88ab129fd38",
}


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


def compatible_profiles(current):
    if all(current.get(k) == v for k, v in NUMBERED_VISUAL_CONTRACT.items()):
        return [current, dict(current, **LEGACY_VISUAL_CONTRACT)]
    return [current]


def profiles_compatible(shared, current):
    return shared in compatible_profiles(current)


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
