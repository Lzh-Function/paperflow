"""公式agy ACPで、作業コピーのPDFだけを読み取って共通JSONを返す。"""

import json
import os
import tempfile
import time
from pathlib import Path

from paperflow.agents.base import AnalysisResult, ProviderError, authorize_request
from paperflow.agents.catalog import JsonChannel
from paperflow.agents.process import checked_help, classify_failure, resolve_command
from paperflow.agents.schema import make_prompt, parse_response, schema_for, validate_response
from paperflow.agents.usage import number
from paperflow.wiki.bundle import sha


def agy_usage(value):
    """ACPが申告した非キャッシュ入力・キャッシュ入力を合算し、費用を推測しない。"""
    raw = value if isinstance(value, dict) else {}
    incoming = number(raw.get("inputTokens"))
    cached = number(raw.get("cachedReadTokens"))
    creation = number(raw.get("cachedWriteTokens"))
    return dict(
        input_tokens=incoming + (cached or 0) + (creation or 0) if incoming is not None else None,
        cached_input_tokens=cached,
        output_tokens=number(raw.get("outputTokens")),
        total_tokens=number(raw.get("totalTokens")),
        thinking_tokens=number(raw.get("thoughtTokens")),
        cost_usd=None,
        source="cli_reported",
    )


def session_settings(pdf=None):
    return dict(
        agentMode="",
        toolPermission="request-review",
        allowNonWorkspaceAccess=False,
        permissions=dict(
            allow=[f"read_file({pdf.as_posix()})"] if pdf else [],
            ask=[],
            deny=[
                f"{action}(*)"
                for action in (
                    "write_file",
                    "command",
                    "unsandboxed",
                    "read_url",
                    "execute_url",
                    "download_url",
                    "mcp",
                )
            ]
            + ([] if pdf else ["read_file(*)"]),
        ),
    )


class Transcript:
    """ACP通知を検査し、指定PDF以外のツール操作・権限要求で直ちに停止する。"""

    def __init__(self, pdf):
        self.pdf = pdf
        self.chunks = []
        self.reads = set()
        self.completed = set()

    def observe(self, event):
        method = event.get("method")
        if method and "id" in event:
            raise ProviderError(
                "tool_disabled", "agyが許可していない追加の権限・ツールを要求しました"
            )
        if method != "session/update":
            return
        update = event.get("params", {}).get("update", {})
        kind = update.get("sessionUpdate")
        if kind in {"tool_call", "tool_call_update"}:
            call = update.get("toolCallId")
            if kind == "tool_call":
                arguments = update.get("rawInput") or update.get("args") or {}
                if (
                    self.pdf is None
                    or update.get("name") != "read_file"
                    or update.get("kind") != "read"
                    or not isinstance(arguments, dict)
                    or set(arguments) != {"Path"}
                    or not isinstance(arguments["Path"], str)
                    or Path(arguments["Path"]).resolve() != self.pdf.resolve()
                    or not isinstance(call, str)
                    or not call
                ):
                    raise ProviderError(
                        "tool_disabled", "agyの読み取り対象が作業用PDFと一致しません"
                    )
                self.reads.add(call)
            elif call not in self.reads:
                raise ProviderError("tool_disabled", "agyが未許可のツール結果を返しました")
            elif update.get("rawInput") or update.get("args"):
                arguments = update.get("rawInput") or update.get("args")
                if (
                    not isinstance(arguments, dict)
                    or set(arguments) != {"Path"}
                    or not isinstance(arguments["Path"], str)
                    or Path(arguments["Path"]).resolve() != self.pdf.resolve()
                ):
                    raise ProviderError("tool_disabled", "agyが読み取り対象を変更しました")
            if update.get("status") == "completed":
                self.completed.add(call)
        elif kind == "agent_message_chunk":
            content = update.get("content", {})
            if content.get("type") == "text":
                if not isinstance(content.get("text"), str):
                    raise ProviderError("invalid_json", "agyの本文通知が不正です")
                self.chunks.append(content["text"])


class AgyCliProvider:
    name = "agy"

    def __init__(self, config):
        self.config = config

    def analyze(self, request):
        config = self.config
        authorize_request(config, request)
        command = resolve_command(config.analysis.agy_command)
        staging = config.storage.data_dir / "staging"
        staging.mkdir(parents=True, exist_ok=True)
        usage = agy_usage(None)
        with tempfile.TemporaryDirectory(prefix="agy-", dir=staging) as directory:
            cwd = Path(directory)
            checked_help(command, [], ["--settings"], cwd)
            checked_help(command, ["acp"], ["--include-default-customizations", "--stdio"], cwd)
            pdf = None
            if request.document:
                from paperflow.agents.document import snapshot

                pdf = snapshot(request.document, cwd, config)
            settings = cwd / "session.json"
            settings.write_text(json.dumps(session_settings(pdf)), encoding="utf-8")
            transcript = Transcript(pdf)
            argv = [
                command,
                "--settings",
                str(settings),
                "acp",
                "--include-default-customizations=false",
                "--log-file",
                str(cwd / "agy.log"),
            ]
            env = os.environ.copy()
            env["AGY_CLI_DISABLE_AUTO_UPDATE"] = "1"
            started = time.monotonic()
            try:
                with JsonChannel(
                    argv,
                    cwd,
                    timeout=config.analysis.timeout_seconds,
                    max_bytes=config.analysis.max_output_bytes,
                    env=env,
                    on_event=transcript.observe,
                ) as channel:
                    sequence = 0

                    def rpc(method, params):
                        nonlocal sequence
                        sequence += 1
                        channel.send(dict(jsonrpc="2.0", id=sequence, method=method, params=params))
                        event = channel.receive(lambda e: e.get("id") == sequence)
                        if event.get("error"):
                            detail = event["error"]
                            message = str(
                                detail.get("message", "") if isinstance(detail, dict) else detail
                            )
                            error = classify_failure(json.dumps(dict(result=message)), "", 1)
                            if error.category == "cli_exit":
                                error = ProviderError(
                                    "cli_version",
                                    "agy ACPの要求に失敗しました。CLIのバージョン・ログイン・モデルを確認してください",
                                )
                            raise error
                        if not isinstance(event.get("result"), dict):
                            raise ProviderError("cli_version", "agy ACPの応答形式が不正です")
                        return event["result"]

                    rpc(
                        "initialize",
                        dict(
                            protocolVersion=1,
                            clientCapabilities=dict(
                                fs=dict(readTextFile=False, writeTextFile=False), terminal=False
                            ),
                            clientInfo=dict(name="paperflow", version="0.1.0"),
                        ),
                    )
                    created = rpc("session/new", dict(cwd=str(cwd), mcpServers=[]))
                    session = created.get("sessionId")
                    if not isinstance(session, str) or not session:
                        raise ProviderError("cli_version", "agyの解析セッションを作成できません")
                    models = created.get("models", {})
                    if (
                        not isinstance(models, dict)
                        or not isinstance(models.get("availableModels", []), list)
                        or any(not isinstance(m, dict) for m in models.get("availableModels", []))
                    ):
                        raise ProviderError("cli_version", "agyのモデル情報が未対応の形式です")
                    model = models.get("currentModelId")
                    if config.analysis.model:
                        if config.analysis.model not in {
                            m.get("modelId") for m in models.get("availableModels", [])
                        }:
                            raise ProviderError(
                                "cli_version",
                                "指定モデルがagyの利用可能一覧にありません。モデル一覧を更新してください",
                            )
                        rpc(
                            "session/set_model",
                            dict(sessionId=session, modelId=config.analysis.model),
                        )
                        model = config.analysis.model
                    prompt = make_prompt(request, text_in_pdf=bool(pdf))
                    if pdf:
                        prompt += (
                            f"\n\n添付PDFの代わりに、作業用PDFをread_fileで直接開いて全ページの本文と図表を読んでください：{pdf}\n"
                            "読み取りツールの引数はPathのみを使い、ページや行を限定しないでください。"
                        )
                    prompt += (
                        "\n\n許可する操作は上記PDFの読み取りだけです。他のファイル・コマンド・Web・MCP・委譲は使用禁止です。"
                        "回答は次のJSON Schemaに適合するJSONオブジェクト1個だけにしてください。Markdownの囲みと前後の説明は禁止です。\n"
                        + json.dumps(schema_for(request), ensure_ascii=False)
                    )
                    result = rpc(
                        "session/prompt",
                        dict(sessionId=session, prompt=[dict(type="text", text=prompt)]),
                    )
                    usage = agy_usage(result.get("usage"))
                    if result.get("stopReason") != "end_turn":
                        raise ProviderError("cli_exit", "agyが解析を完了する前に停止しました")
                text = "".join(transcript.chunks).strip()
                try:
                    data = parse_response(text)
                except ValueError as exc:
                    raise ProviderError("invalid_json", "agyの要約JSONを取得できません") from exc
                receipt = dict(
                    provider=self.name,
                    model=model,
                    input_kind="native_pdf_readonly" if pdf else "text",
                    pdf_sha256=request.document.sha256 if pdf else None,
                    page_count=request.document.page_count if pdf else 0,
                    pdf_read_completed=bool(transcript.completed),
                    duplicate_fulltext=False,
                )
                if pdf and (not transcript.completed or sha(pdf) != request.document.sha256):
                    error = ProviderError(
                        "visual_reading",
                        "agyによるPDFの読み取り完了を確認できません。回答を採用しません",
                    )
                    error.rejected_response, error.input_receipt = data, receipt
                    raise error
                elapsed = time.monotonic() - started
                return AnalysisResult(
                    validate_response(
                        data, request, usage=usage, elapsed_seconds=elapsed, input_receipt=receipt
                    ),
                    self.name,
                    model,
                    elapsed_seconds=elapsed,
                    usage=usage,
                )
            except ProviderError as exc:
                exc.usage = getattr(exc, "usage", usage)
                exc.elapsed_seconds = time.monotonic() - started
                if exc.category == "timeout":
                    exc.args = ("agyの解析がタイムアウトしました",)
                raise
