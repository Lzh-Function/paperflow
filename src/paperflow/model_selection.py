"""モデル候補のキャッシュ、Provider別の選択記憶、日本語の対話UI。"""

import json
import os
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

import click

from paperflow.agents.base import ProviderError
from paperflow.agents.catalog import (
    ModelCatalog,
    ModelChoice,
    catalog_data,
    discover,
    display_text,
    model_id,
)
from paperflow.config import SettingsError
from paperflow.obsidian.writer import local_lock

PROVIDERS = ("claude", "codex", "fake", "agy")
LABELS = {
    "claude": "Claude Code CLI",
    "codex": "Codex CLI",
    "agy": "Antigravity CLI（agy）",
    "fake": "Fake（オフライン検証用）",
}


def read_json(path):
    if path.stat().st_size > 2097152:
        raise ValueError("モデル設定ファイルのサイズ上限を超えています")
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_json(path: Path, data):
    fd, name = tempfile.mkstemp(prefix=".models-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def preferences(config):
    path = config.storage.data_dir / "model-preferences.json"
    try:
        data = read_json(path)
        if not isinstance(data, dict) or data.get("version") != 1:
            raise ValueError("未対応の形式")
        if data.get("last_provider") not in PROVIDERS or not isinstance(data.get("models"), dict):
            raise ValueError("Provider設定が不正")
        for provider, value in data["models"].items():
            if provider not in ("claude", "codex", "agy"):
                raise ValueError("Provider設定が不正")
            model_id(value)
        return data
    except FileNotFoundError:
        return {"version": 1, "last_provider": None, "models": {}}
    except (OSError, ValueError, TypeError) as exc:
        raise SettingsError(f"{path} を確認してください（モデル選択設定を読めません）") from exc


def remember(config, provider, selected_model):
    with local_lock(config.storage.data_dir):
        data = preferences(config)
        data["last_provider"] = provider
        if selected_model:
            data["models"][provider] = model_id(selected_model)
        else:
            data["models"].pop(provider, None)
        atomic_json(config.storage.data_dir / "model-preferences.json", data)


def cached_catalog(config, provider):
    try:
        data = read_json(config.storage.data_dir / f"model-catalog-{provider}.json")
        if data["provider"] != provider or not isinstance(data["models"], list):
            return None
        choices = []
        for entry in data["models"]:
            choices.append(
                ModelChoice(
                    model_id(entry["model_id"]),
                    display_text(entry["display_name"]),
                    bool(entry.get("is_default")),
                )
            )
        if not choices or not isinstance(data["fetched_at"], str):
            return None
        return ModelCatalog(provider, choices, data["fetched_at"], "cache")
    except (OSError, ValueError, TypeError, KeyError):
        return None


def get_catalog(config, provider, *, cached_only=False):
    if provider not in ("claude", "codex", "agy"):
        raise SettingsError("モデル一覧はclaude・codex・agyを指定してください")
    if cached_only:
        catalog = cached_catalog(config, provider)
        if catalog is None:
            raise ProviderError("model_catalog", "保存済みのモデル候補がありません")
        return catalog
    try:
        catalog = discover(config, provider)
    except ProviderError as exc:
        catalog = cached_catalog(config, provider)
        if catalog is None:
            raise
        return replace(catalog, warning=f"CLI照会失敗のため保存済み候補を表示: {exc}")
    with local_lock(config.storage.data_dir):
        atomic_json(
            config.storage.data_dir / f"model-catalog-{provider}.json", catalog_data(catalog)
        )
    return catalog


def menu(prompt, labels, default=1):
    for index, label in enumerate(labels, 1):
        click.echo(f"  {index}. {label}", err=True)
    return click.prompt(prompt, type=click.IntRange(1, len(labels)), default=default, err=True) - 1


def choose_model(config, provider, previous, *, dry_run):
    if dry_run:
        click.echo("dry-run: 保存済み候補のみ使用します。CLI照会・設定保存は行いません。", err=True)
    try:
        catalog = get_catalog(config, provider, cached_only=dry_run)
        if catalog.warning:
            click.echo(catalog.warning, err=True)
        if catalog.source == "cache":
            click.echo(
                f"保存済みモデル候補（取得日時: {display_text(catalog.fetched_at)}）", err=True
            )
        choices = catalog.models
    except ProviderError as exc:
        click.echo(f"モデル一覧を取得できません: {exc}。モデルIDを手入力できます。", err=True)
        choices = []
    labels = [
        f"{c.display_name} — {c.model_id}" + ("（CLIの既定候補）" if c.is_default else "")
        for c in choices
    ]
    default = next((i + 1 for i, c in enumerate(choices) if c.model_id == previous), None)
    if previous and default is None:
        choices.append(ModelChoice(previous, "前回または設定のモデル"))
        labels.append(f"前回または設定のモデル — {previous}（現在の利用可否は未確認）")
        default = len(choices)
    labels.append("モデルIDを手入力")
    if default is None:
        default = next((i + 1 for i, c in enumerate(choices) if c.is_default), 1)
    index = menu("使用するモデルの番号", labels, default)
    if index < len(choices):
        return choices[index].model_id
    while True:
        value = click.prompt("モデルID", default=previous, type=str, err=True)
        try:
            return model_id(value)
        except ValueError as exc:
            click.echo(str(exc), err=True)


def configure(
    ctx, provider_name=None, model=None, interactive=None, *, dry_run=False, selection_only=False
):
    """選択完了後だけConfigへ反映。dry-runはキャッシュを含め一切書き込まない。"""
    config = ctx.obj["config"]
    if interactive and ctx.obj["json"]:
        raise click.UsageError("--interactiveと--jsonは同時に指定できません")
    if selection_only and ctx.obj["json"]:
        raise click.UsageError("select-modelは対話コマンドです。--jsonはmodelsを使用してください")
    if interactive is None:
        interactive = not ctx.obj["json"] and sys.stdin.isatty() and sys.stdout.isatty()
    if selection_only:
        interactive = True
    saved = preferences(config)
    provider = provider_name or saved["last_provider"] or config.analysis.default_provider
    if interactive and provider_name is None:
        provider = PROVIDERS[
            menu(
                "使用するProviderの番号",
                [LABELS[p] for p in PROVIDERS],
                PROVIDERS.index(provider) + 1,
            )
        ]
    configured = config.analysis.model if provider == config.analysis.default_provider else None
    selected = saved["models"].get(provider) or configured
    if selected is not None and model is None and provider != "fake":
        try:
            selected = model_id(selected)
        except ValueError as exc:
            raise SettingsError(str(exc)) from exc
    if provider == "fake":
        if model is not None:
            raise click.UsageError(
                "--modelには--provider claude・codex・agyのいずれかを指定してください"
            )
        selected = None
    elif model is not None:
        try:
            selected = model_id(model)
        except ValueError as exc:
            raise click.BadParameter(str(exc), param_hint="--model") from exc
    elif interactive:
        selected = choose_model(config, provider, selected, dry_run=dry_run)
    if interactive and not dry_run and model is None:
        remember(config, provider, selected)
    config.analysis.default_provider = provider
    config.analysis.model = selected
    info = {
        "provider": provider,
        "model": selected,
        "model_source": "pinned" if selected else ("fake" if provider == "fake" else "cli_default"),
    }
    ctx.obj["model_selection"] = info
    if interactive:
        click.echo(
            f"使用設定: {LABELS[provider]} / {selected or ('オフライン' if provider == 'fake' else 'CLIの既定モデル')}",
            err=True,
        )
    return info
