import os
import platform
from pathlib import Path

from paperflow.agents.base import ProviderError
from paperflow.agents.process import checked_help, resolve_command, run_bounded


def doctor(config):
    checks = []
    for label, path in (
        ("paperpile", config.source.paperpile_dir),
        ("vault", config.destination.vault_dir),
    ):
        checks.append(dict(check=label, ok=path.is_dir(), path=str(path)))
    # .obsidian is application-owned: only the vault root marker is inspected.
    marker = config.destination.vault_dir / ".llm-wiki-root"
    checks.append(dict(check="wiki_marker", ok=marker.is_file(), path=str(marker)))
    bib = config.source.bibtex_file
    checks.append(
        dict(
            check="bibtex",
            ok=bool(bib and bib.is_file()),
            path=str(bib) if bib else None,
            message="未設定の場合はPDF候補検出のみ。READMEのBibTeX設定を参照。",
        )
    )
    root = config.destination.vault_dir
    output = root / "PaperFlow"
    existing = output if output.exists() else root
    checks.append(
        dict(
            check="output_access_hint",
            ok=existing.is_dir() and os.access(existing, os.W_OK),
            message="読み取り専用確認。Driveへの実書き込み可否は未検証。",
        )
    )
    checks.append(
        dict(
            check="vault_write_enabled",
            ok=config.destination.allow_vault_write,
            message="未承認の初回書き込みは設定で停止します。",
        )
    )
    checks.append(
        dict(
            check="wiki_lock_absent",
            ok=not (root / "00_System/Locks/agent-write-lock.yml").exists(),
        )
    )
    for name in ("claude", "codex"):
        try:
            command = resolve_command(getattr(config.analysis, name + "_command"))
            cwd = Path.cwd()
            version = run_bounded(
                [command, "--version"], cwd=cwd, timeout=20, max_bytes=65536
            ).strip()
            required = (
                ["--json-schema", "--tools", "--strict-mcp-config"]
                if name == "claude"
                else ["--output-schema", "--sandbox", "--ephemeral", "--ignore-user-config"]
            )
            checked_help(command, [] if name == "claude" else ["exec"], required, cwd)
            checks.append(
                dict(
                    check=name,
                    ok=True,
                    version=version,
                    executable=command,
                    message="認証・推論・ツール遮断のE2Eは別途確認が必要です。",
                )
            )
        except ProviderError as exc:
            checks.append(dict(check=name, ok=False, category=exc.category, message=str(exc)))
    return dict(
        platform=platform.platform(),
        python=platform.python_version(),
        checks=checks,
        read_only=True,
    )
