"""新規PCの設定ファイル作成と、ローカル実行環境の確認。"""

import argparse
import os
import sys
import tempfile
from pathlib import Path

from paperflow.agents.base import ProviderError
from paperflow.agents.process import resolve_command
from paperflow.config import SettingsError, load_config


def initialize(project):
    project = Path(project).resolve()
    if os.name == "nt" and project.drive.upper() != "C:":
        raise SettingsError("アプリはC:内に置いてください")
    if any(p.casefold() == ".obsidian" for p in project.parts):
        raise SettingsError(".obsidian内には設定を作成できません")
    for name in ("pyproject.toml", "uv.lock", "config.example.yaml", "taxonomy.yaml"):
        if not (project / name).is_file():
            raise SettingsError("ソース一式が不足しています：" + name)
    import tkinter  # noqa: F401

    path = project / "config.production.yaml"
    if path.is_symlink():
        raise SettingsError("設定ファイルへシンボリックリンクは使用できません")
    created = False
    if not path.exists():
        template = project / "config.example.yaml"
        candidate = load_config(template)
        if candidate.analysis.allow_external_fulltext or candidate.destination.allow_vault_write:
            raise SettingsError("見本の外部送信・Vault保存の許可をfalseへ戻してください")
        fd, name = tempfile.mkstemp(prefix=".pf-setup-", suffix=".yaml", dir=project)
        temp = Path(name)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(template.read_bytes())
                stream.flush()
                os.fsync(stream.fileno())
            try:
                if os.name == "nt":
                    os.rename(temp, path)
                else:
                    os.link(temp, path)
                created = True
            except FileExistsError:
                pass
        finally:
            temp.unlink(missing_ok=True)
    cfg = load_config(path)
    for protected in (cfg.source.paperpile_dir, cfg.destination.vault_dir):
        if project.is_relative_to(protected):
            raise SettingsError("アプリはPaperpile・Vaultの外に置いてください")
    providers = {}
    for provider in ("claude", "codex"):
        try:
            providers[provider] = resolve_command(getattr(cfg.analysis, provider + "_command"))
        except ProviderError:
            providers[provider] = None
    return dict(config=str(path), created=created, providers=providers)


def main():
    parser = argparse.ArgumentParser(description="PaperFlowの初期設定")
    parser.add_argument("--project", type=Path, default=Path.cwd())
    args = parser.parse_args()
    try:
        result = initialize(args.project)
    except (OSError, ValueError, ImportError) as exc:
        print("初期設定エラー：" + str(exc), file=sys.stderr)
        return 2
    print(
        "設定ファイルを作成しました。"
        if result["created"]
        else "既存の設定ファイルを保持しました。"
    )
    for provider, command in result["providers"].items():
        print(
            provider
            + "："
            + (
                "実行ファイルを検出"
                if command
                else "未検出。必要なCLIを導入し、GUIの設定でパスを指定してください。"
            )
        )
    print("PDF外部送信・Vault保存・定期実行は開始していません。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
