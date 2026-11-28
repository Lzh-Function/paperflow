import os
import re
from pathlib import Path, PureWindowsPath
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from paperflow.paths import resolve_path


class SettingsError(ValueError):
    pass


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Source(StrictModel):
    paperpile_dir: Path
    bibtex_file: Path | None = None
    recurse: bool = True
    read_only: Literal[True] = True


class Destination(StrictModel):
    vault_dir: Path
    namespace: Literal["PaperFlow"] = "PaperFlow"
    layout: Literal["paperflow", "llm_wiki"] = "paperflow"
    copy_pdf: bool = False
    include_visuals: bool = True
    visual_max_items: int = Field(default=3, ge=1, le=8)
    allow_vault_write: bool = False
    require_wiki_marker: bool = True

    @model_validator(mode="after")
    def frozen_copy(self):
        if self.copy_pdf != (self.layout == "llm_wiki"):
            raise ValueError("llm_wiki出力はcopy_pdf: true、従来出力はfalseを指定してください")
        return self


class Storage(StrictModel):
    data_dir: Path = Path(".data")
    database: Path = Path(".data/state.db")


class Analysis(StrictModel):
    research_context: str = Field(
        default=(
            "専門はケモインフォマティクス。バイオインフォマティクス、純粋なNLP、"
            "画像系AI、ALife・人工生命、LLMシミュレーションなどAI全般にも広く関心がある。"
            "専門分野への直接貢献と、他分野の知的関心・手法上の有用性を分けて説明する。"
            "ケモインフォへの関連が薄いことだけを理由に他の関心分野の優先度を下げない。"
        ),
        max_length=10000,
    )
    default_provider: Literal["fake", "claude", "codex", "agy"] = "fake"
    policy: Literal["abstract_only", "fulltext_external"] = "abstract_only"
    allow_external_fulltext: bool = False
    require_visual_reading: bool = True
    # 2026-11-17のPaperpile実測に基づく運用上限。モデル固有の上限とは区別する。
    visual_max_pages: int = Field(default=300, ge=1)
    visual_max_pdf_mb: int = Field(default=64, ge=1)
    visual_max_image_mb: int = Field(default=128, ge=1)
    language: Literal["ja"] = "ja"
    max_chars: int = Field(default=3000000, ge=100)
    max_concurrency: Literal[1] = 1
    timeout_seconds: int = Field(default=240, ge=1, le=3600)
    max_retries: int = Field(default=3, ge=0, le=10)
    max_cli_calls_per_run: int = Field(default=10, ge=1, le=100)
    retry_base_seconds: float = Field(default=30, ge=0, le=3600)
    taxonomy_file: Path = Path("taxonomy.yaml")
    prompt_version: Literal["v1"] = "v1"
    model: str | None = None
    claude_command: str = "claude"
    codex_command: str = "codex"
    agy_command: str = "agy"
    max_output_bytes: int = Field(default=2097152, ge=1024, le=16777216)
    max_budget_usd: float = Field(default=0.5, gt=0, le=100)

    @model_validator(mode="after")
    def fulltext_permission(self):
        if self.policy == "fulltext_external" and not self.allow_external_fulltext:
            raise ValueError("全文送信にはallow_external_fulltextの明示許可が必要です")
        return self


class Sync(StrictModel):
    interval_seconds: int = Field(default=300, ge=1)
    stable_scan_count: int = Field(default=2, ge=1, le=10)
    max_papers_per_run: int = Field(default=10, ge=1, le=1000)


class Config(StrictModel):
    source: Source
    destination: Destination
    storage: Storage = Field(default_factory=Storage)
    analysis: Analysis = Field(default_factory=Analysis)
    sync: Sync = Field(default_factory=Sync)

    def taxonomy(self) -> list[str]:
        data = yaml.safe_load(self.analysis.taxonomy_file.read_text(encoding="utf-8"))
        topics = data.get("topics") if isinstance(data, dict) else data
        if (
            not isinstance(topics, list)
            or not topics
            or any(
                not isinstance(t, str) or not re.fullmatch(r"[a-z][a-z0-9-]*", t) for t in topics
            )
            or len(topics) != len(set(topics))
        ):
            raise SettingsError("taxonomyは重複のないASCII slugの一覧にしてください")
        return topics


def inside(path: Path, root: Path) -> bool:
    return resolve_path(path).is_relative_to(resolve_path(root))


def load_config(path: Path) -> Config:
    try:
        config = Config.model_validate(yaml.safe_load(path.read_text(encoding="utf-8-sig")))
        base = resolve_path(path).parent
        fields = [
            (config.source, "paperpile_dir"),
            (config.source, "bibtex_file"),
            (config.destination, "vault_dir"),
            (config.storage, "data_dir"),
            (config.storage, "database"),
            (config.analysis, "taxonomy_file"),
        ]
        for obj, name in fields:
            value = getattr(obj, name)
            if value is None:
                continue
            if os.name != "nt" and PureWindowsPath(str(value)).drive:
                raise SettingsError(
                    "Windowsのドライブ設定はWindowsネイティブPythonで実行してください"
                )
            setattr(obj, name, resolve_path(base / value))
        source, vault = config.source.paperpile_dir, config.destination.vault_dir
        if inside(vault, source) or inside(source, vault):
            raise SettingsError("原本とVaultのパスは分離してください")
        for local in (config.storage.data_dir, config.storage.database):
            if inside(local, source) or inside(local, vault):
                raise SettingsError("DB・キャッシュはPaperpile/Vaultの外に置いてください")
            if os.name == "nt" and local.drive.upper() != "C:":
                raise SettingsError("WindowsのDB・キャッシュはC:に置いてください")
        if config.source.bibtex_file and inside(config.storage.data_dir, config.source.bibtex_file):
            raise SettingsError("キャッシュとBibTeXを分離してください")
        config.taxonomy()
        return config
    except (OSError, ValueError, yaml.YAMLError, TypeError) as exc:
        raise SettingsError(str(exc)) from exc
