"""shell=False、stdin入力、出力量と実行時間を制限したCLI実行。"""

import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path

from paperflow.agents.base import ProviderError


def classify_failure(stdout: str, stderr: str, exit_code: int) -> ProviderError:
    """CLIのエラー欄だけを分類し、本文やトークン数をエラーコードと誤認しない。"""
    subtype, status, details = "", None, stdout
    try:
        try:
            envelope = json.loads(stdout)
        except json.JSONDecodeError:
            # PDF入力はstream-json。初期化イベントや本文を障害判定へ混ぜない。
            events = [json.loads(line) for line in stdout.splitlines() if line.strip()]
            envelope = next(
                (e for e in reversed(events) if isinstance(e, dict) and e.get("type") == "result"),
                {},
            )
        if isinstance(envelope, dict):
            subtype = envelope.get("subtype", "")
            status = envelope.get("api_error_status")
            details = str(envelope.get("result", "")) + " " + str(envelope.get("errors", ""))
            terminal = envelope.get("status")
            if isinstance(terminal, str) and terminal in {
                "ERROR",
                "CANCELED",
                "INTERRUPTED",
                "INVALID",
                "WAITING",
            }:
                details = str(envelope.get("error", ""))
    except json.JSONDecodeError:
        pass
    combined = (details + "\n" + stderr).lower()
    if (
        subtype == "error_max_budget_usd"
        or "max budget" in combined
        or "budget exceeded" in combined
    ):
        return ProviderError("budget_limit", "CLIの設定費用上限に達しました")
    if subtype == "error_max_turns":
        return ProviderError("turn_limit", "CLIのターン数上限に達しました")
    if (
        status == 401
        or any(
            s in combined
            for s in (
                "unauthenticated",
                "not logged in",
                "login required",
                "failed to authenticate",
                "oauth session expired",
                "authentication required",
            )
        )
        or re.search(r"(?:http|status|api error)\D{0,20}401\b", combined)
    ):
        return ProviderError(
            "authentication", "CLI認証に失敗しました。公式CLIでログインしてください"
        )
    if (
        status == 429
        or any(s in combined for s in ("rate limit", "overloaded", "temporarily unavailable"))
        or re.search(r"(?:http|status|api error)\D{0,20}429\b", combined)
    ):
        return ProviderError("rate_limit", "CLIの一時的な利用制限です", True)
    if any(s in combined for s in ("prompt is too long", "context length", "context window")):
        return ProviderError("context_limit", "CLIの入力コンテキスト上限を超えました")
    if isinstance(status, int) and 500 <= status < 600:
        return ProviderError(
            "service_error", f"CLIの接続先で一時的なエラーが発生しました（HTTP {status}）", True
        )
    if any(s in combined for s in ("connection error", "connection refused", "connection reset")):
        return ProviderError("connection", "CLIの接続に失敗しました", True)
    if isinstance(subtype, str) and re.fullmatch(r"error_[a-z_]{1,60}", subtype):
        return ProviderError("cli_exit", f"CLIが失敗しました（{subtype}、終了コード{exit_code}）")
    return ProviderError("cli_exit", f"CLIが終了コード{exit_code}で失敗しました")


def resolve_command(command: str) -> str:
    resolved = shutil.which(command)
    if (
        os.name == "nt"
        and command.lower() in {"agy", "agy.exe"}
        and (not resolved or Path(resolved).suffix.lower() in {".cmd", ".bat", ".ps1"})
    ):
        candidate = Path.home() / "AppData/Local/agy/bin/agy.exe"
        if candidate.is_file():
            resolved = str(candidate)
    if not resolved:
        raise ProviderError(
            "missing_cli", "CLIがありません。インストールまたはcommand設定を確認してください"
        )
    path = Path(resolved)
    if os.name == "nt" and path.suffix.lower() in {".cmd", ".bat", ".ps1"}:
        if path.stem == "codex":
            base = path.parent / "node_modules/@openai/codex/node_modules/@openai"
            binaries = list(base.glob("codex-win32-*/vendor/*/bin/codex.exe"))
            if len(binaries) == 1:
                return str(binaries[0])
        raise ProviderError(
            "cli_wrapper", "バッチファイルは実行しません。ネイティブexeのパスを設定してください"
        )
    return str(path)


def run_bounded(
    argv: list[str],
    *,
    cwd: Path,
    text="",
    timeout=240,
    max_bytes=2097152,
    env=None,
    include_stderr=False,
) -> str:
    with (
        tempfile.TemporaryFile() as out,
        tempfile.TemporaryFile() as err,
        tempfile.TemporaryFile() as inp,
    ):
        inp.write(text.encode("utf-8"))
        inp.seek(0)
        started = time.monotonic()
        try:
            proc = subprocess.Popen(
                argv,
                shell=False,
                cwd=cwd,
                stdin=inp,
                stdout=out,
                stderr=err,
                env=env,
                start_new_session=os.name != "nt",
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except OSError as exc:
            raise ProviderError("launch", "CLIの起動に失敗しました") from exc
        try:
            while proc.poll() is None:
                if time.monotonic() - started > timeout:
                    raise ProviderError("timeout", "CLIがタイムアウトしました", True)
                if os.fstat(out.fileno()).st_size + os.fstat(err.fileno()).st_size > max_bytes:
                    raise ProviderError("output_limit", "CLI出力量の上限を超えました")
                time.sleep(0.05)
            if os.fstat(out.fileno()).st_size + os.fstat(err.fileno()).st_size > max_bytes:
                raise ProviderError("output_limit", "CLI出力量の上限を超えました")
            out.seek(0)
            err.seek(0)
            stdout = out.read(max_bytes).decode("utf-8", errors="replace")
            stderr = err.read(max_bytes).decode("utf-8", errors="replace")
            if proc.returncode:
                error = classify_failure(stdout, stderr, proc.returncode)
                if "--json-schema" in argv or "--output-schema" in argv:
                    from paperflow.agents.usage import extract

                    error.usage = extract("claude" if "--json-schema" in argv else "codex", stdout)
                raise error
            return stdout + ("\n" + stderr if include_stderr and stderr else "")
        finally:
            stop_process(proc)


def stop_process(proc):
    """制御照会も解析も、終了時に子プロセスを残さない。"""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill.exe", "/PID", str(proc.pid), "/T", "/F"],
                shell=False,
                capture_output=True,
                timeout=10,
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=10)


def checked_help(command: str, args: list[str], required: list[str], cwd: Path):
    # agy（Goのflag）は正常な--helpもstderrへ出力する。
    text = run_bounded([command, *args, "--help"], cwd=cwd, timeout=20, include_stderr=True)
    if any(flag not in text for flag in required):
        raise ProviderError(
            "cli_version", "必要なCLIオプションがありません。doctorでバージョンを確認してください"
        )
    return text
