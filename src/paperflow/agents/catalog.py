"""CLIの制御プロトコルでモデル候補だけを取得。推論・turn/startは行わない。"""

import json
import os
import queue
import subprocess
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

from paperflow.agents.base import ProviderError
from paperflow.agents.process import classify_failure, resolve_command, stop_process


@dataclass(frozen=True)
class ModelChoice:
    model_id: str
    display_name: str
    is_default: bool = False
    aliases: list[str] = field(default_factory=list)


@dataclass
class ModelCatalog:
    provider: str
    models: list[ModelChoice]
    fetched_at: str
    source: str = "cli"
    warning: str | None = None


def model_id(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 256
        or value.startswith("-")
        or any(ord(c) < 33 or ord(c) == 127 for c in value)
    ):
        raise ValueError("モデルIDは空白・制御文字を含まない256文字以内の値にしてください")
    return value


def display_text(value) -> str:
    return " ".join("".join(c for c in str(value) if ord(c) >= 32 and ord(c) != 127).split())[:160]


def response_object(event, name):
    value = event.get(name)
    return value if isinstance(value, dict) else {}


class JsonChannel:
    """期限・出力量を制限したJSONL制御チャネル。本文は渡さない。"""

    def __init__(self, argv, cwd, *, timeout=20, max_bytes=2097152, env=None, on_event=None):
        self.deadline = time.monotonic() + timeout
        self.max_bytes = max_bytes
        self.on_event = on_event
        self.events = queue.Queue(maxsize=128)
        self.closed = threading.Event()
        self.stderr = tempfile.TemporaryFile()
        try:
            self.process = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self.stderr,
                cwd=cwd,
                env=env,
                shell=False,
                start_new_session=os.name != "nt",
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except OSError as exc:
            self.stderr.close()
            raise ProviderError("launch", "モデル一覧照会用CLIを起動できません") from exc
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _put(self, event):
        while not self.closed.is_set():
            try:
                self.events.put(event, timeout=0.1)
                return
            except queue.Full:
                continue

    def _read(self):
        total = 0
        try:
            while not self.closed.is_set():
                line = self.process.stdout.readline(self.max_bytes + 1)
                if not line:
                    break
                total += len(line)
                if total > self.max_bytes:
                    self._put(ProviderError("output_limit", "モデル一覧の出力量上限を超えました"))
                    return
                try:
                    data = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if isinstance(data, dict):
                    self._put(data)
        except (OSError, ValueError):
            pass
        finally:
            self._put(None)

    def send(self, message):
        try:
            self.process.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
            self.process.stdin.flush()
        except (OSError, ValueError) as exc:
            raise ProviderError("model_catalog", "モデル一覧照会中にCLIが切断されました") from exc

    def receive(self, predicate):
        while True:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise ProviderError("timeout", "モデル一覧の取得がタイムアウトしました")
            if os.fstat(self.stderr.fileno()).st_size > self.max_bytes:
                raise ProviderError("output_limit", "モデル一覧の診断出力量上限を超えました")
            try:
                event = self.events.get(timeout=min(0.1, remaining))
            except queue.Empty:
                continue
            if isinstance(event, ProviderError):
                raise event
            if event is None:
                self.stderr.seek(0)
                details = self.stderr.read(self.max_bytes).decode("utf-8", errors="replace")
                raise classify_failure("", details, self.process.poll() or 1)
            if self.on_event:
                self.on_event(event)
            # A catalog lookup never grants server-initiated tool or credential requests.
            if "method" in event and "id" in event:
                self.send(
                    {
                        "id": event["id"],
                        "error": {
                            "code": -32601,
                            "message": "PaperFlow model catalog supports no client methods",
                        },
                    }
                )
                continue
            if predicate(event):
                return event

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed.set()
        try:
            stop_process(self.process)
        finally:
            self.thread.join(timeout=2)
            self.process.stdin.close()
            self.process.stdout.close()
            self.stderr.close()


def normalize_models(provider: str, entries: list[dict]) -> list[ModelChoice]:
    if not isinstance(entries, list):
        raise ProviderError("model_catalog", "CLIのモデル一覧が不正な形式です")
    result = {}
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("hidden"):
            continue
        if provider == "claude":
            alias = entry.get("value")
            resolved = entry.get("resolvedModel")
            if not resolved and alias == "default":
                continue
            identifier = resolved or alias
            if (
                isinstance(alias, str)
                and alias.endswith("[1m]")
                and isinstance(identifier, str)
                and not identifier.endswith("[1m]")
            ):
                identifier += "[1m]"
            default = alias == "default"
            aliases = [alias] if isinstance(alias, str) else []
        else:
            identifier = entry.get("model") or entry.get("id")
            default = bool(entry.get("isDefault"))
            aliases = []
        try:
            identifier = model_id(identifier)
        except ValueError:
            continue
        name = display_text(entry.get("displayName") or entry.get("label") or identifier)
        name = name.replace(" (1M context)", "（100万トークンのコンテキスト）")
        if provider == "claude" and alias == "default":
            name = identifier
        choice = ModelChoice(identifier, name, default, aliases)
        if identifier in result:
            previous = result[identifier]
            choice = replace(
                choice,
                is_default=default or previous.is_default,
                aliases=sorted(set(aliases + previous.aliases)),
            )
        result[identifier] = choice
    return list(result.values())


def discover(config, provider: str, *, timeout=20) -> ModelCatalog:
    if provider not in ("claude", "codex", "agy"):
        raise ValueError("モデル一覧はclaude・codex・agyを指定してください")
    command = resolve_command(getattr(config.analysis, provider + "_command"))
    env = os.environ.copy()
    env.pop("CLAUDECODE", None)
    with tempfile.TemporaryDirectory(prefix="paperflow-models-") as directory:
        cwd = Path(directory)
        if provider == "agy":
            from paperflow.agents.process import run_bounded

            raw = run_bounded(
                [command, "--output-format", "json", "models"], cwd=cwd, timeout=timeout
            )
            try:
                response = json.loads(raw)
                if response.get("status") != "SUCCESS":
                    raise ProviderError("model_catalog", "agyのモデル一覧照会に失敗しました")
                entries = response["command"]["data"]["models"]
            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                raise ProviderError("model_catalog", "agyのモデル一覧が未対応の形式です") from exc
        elif provider == "claude":
            argv = [
                command,
                "-p",
                "--input-format",
                "stream-json",
                "--output-format",
                "stream-json",
                "--verbose",
                "--tools",
                "",
                "--strict-mcp-config",
                "--setting-sources",
                "",
                "--safe-mode",
                "--no-session-persistence",
                "--no-chrome",
            ]
            with JsonChannel(argv, cwd, timeout=timeout, env=env) as channel:
                channel.send(
                    {
                        "type": "control_request",
                        "request_id": "paperflow-models",
                        "request": {"subtype": "initialize", "hooks": {}, "sdkMcpServers": []},
                    }
                )
                event = channel.receive(
                    lambda e: (
                        e.get("type") == "control_response"
                        and response_object(e, "response").get("request_id") == "paperflow-models"
                    )
                )
                response = event.get("response", {})
                if response.get("subtype") != "success":
                    raise ProviderError("model_catalog", "Claude CLIのモデル一覧を取得できません")
                entries = response_object(response, "response").get("models", [])
        elif provider == "codex":
            argv = [
                command,
                "app-server",
                "--listen",
                "stdio://",
                "-c",
                "mcp_servers={}",
                "-c",
                "plugins={}",
                "-c",
                "hooks={}",
            ]
            with JsonChannel(argv, cwd, timeout=timeout, env=env) as channel:
                channel.send(
                    {
                        "id": 0,
                        "method": "initialize",
                        "params": {"clientInfo": {"name": "paperflow", "version": "0.1.0"}},
                    }
                )
                initialized = channel.receive(lambda e: e.get("id") == 0)
                if initialized.get("error"):
                    raise ProviderError("model_catalog", "Codexのモデル照会を初期化できません")
                channel.send({"method": "initialized"})
                entries, cursor, cursors = [], None, set()
                for number in range(1, 21):
                    params = {"limit": 100, "includeHidden": False}
                    if cursor:
                        params["cursor"] = cursor
                    channel.send({"id": number, "method": "model/list", "params": params})
                    event = channel.receive(lambda e: e.get("id") == number)
                    if event.get("error"):
                        raise ProviderError("model_catalog", "Codexのモデル一覧を取得できません")
                    result = event.get("result", {})
                    if not isinstance(result, dict) or not isinstance(result.get("data"), list):
                        raise ProviderError("model_catalog", "Codexのモデル一覧が不正な形式です")
                    entries.extend(result.get("data", []))
                    cursor = result.get("nextCursor")
                    if not cursor:
                        break
                    if not isinstance(cursor, str):
                        raise ProviderError("model_catalog", "モデル一覧のページ応答が不正です")
                    if cursor in cursors:
                        raise ProviderError(
                            "model_catalog", "モデル一覧のページ応答が循環しています"
                        )
                    cursors.add(cursor)
                else:
                    raise ProviderError("model_catalog", "モデル一覧のページ数上限に達しました")
        else:
            raise ValueError("モデル一覧はclaudeかcodexを指定してください")
    models = normalize_models(provider, entries)
    if not models:
        raise ProviderError("model_catalog", "CLIが選択可能なモデルを返しませんでした")
    return ModelCatalog(provider, models, datetime.now(UTC).isoformat())


def catalog_data(catalog: ModelCatalog) -> dict:
    return asdict(catalog)
