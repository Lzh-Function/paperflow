import base64
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path

from paperflow.agents.base import AnalysisResult, ProviderError, authorize_request
from paperflow.agents.process import checked_help, classify_failure, resolve_command, run_bounded
from paperflow.agents.schema import make_prompt, parse_response, schema_for, validate_response
from paperflow.agents.usage import claude_usage


class ClaudeCliProvider:
    name = "claude"

    def __init__(self, config):
        self.config = config

    def analyze(self, request):
        config = self.config
        authorize_request(config, request)
        command = resolve_command(config.analysis.claude_command)
        staging = config.storage.data_dir / "staging"
        staging.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="claude-", dir=staging) as name:
            cwd = Path(name)
            help_text = checked_help(
                command,
                [],
                [
                    "--json-schema",
                    "--output-format",
                    "--tools",
                    "--disallowedTools",
                    "--strict-mcp-config",
                    "--setting-sources",
                ],
                cwd,
            )
            argv = [
                command,
                "-p",
                "--output-format",
                "json",
                "--json-schema",
                json.dumps(schema_for(request)),
                "--tools",
                "",
                "--disallowedTools",
                "mcp__*",
                "--strict-mcp-config",
                "--setting-sources",
                "",
            ]
            for option in ("--no-chrome", "--no-session-persistence", "--disable-slash-commands"):
                if option in help_text:
                    argv.append(option)
            if "--safe-mode" in help_text:
                argv.append("--safe-mode")
            if "--max-budget-usd" in help_text:
                argv += ["--max-budget-usd", str(config.analysis.max_budget_usd)]
            if "--max-turns" in help_text:
                argv += ["--max-turns", "2"]
            if config.analysis.model:
                argv += ["--model", config.analysis.model]
            prompt = make_prompt(request, text_in_pdf=request.document is not None)
            input_parts = []
            preparation = "text"
            if request.document is not None:
                from paperflow.agents.document import prepare_claude_pdf, snapshot

                if "--input-format" not in help_text:
                    raise ProviderError("cli_version", "Claude CLIがPDF添付入力に対応していません")
                pdf = snapshot(request.document, cwd, config)
                parts, preparation = prepare_claude_pdf(pdf, cwd)
                content = []
                for part in parts:
                    payload = part.path.read_bytes()
                    input_parts.append(
                        dict(
                            first_page=part.first_page,
                            last_page=part.last_page,
                            pdf_sha256=hashlib.sha256(payload).hexdigest(),
                            pdf_bytes=len(payload),
                        )
                    )
                    content.append(
                        dict(
                            type="document",
                            source=dict(
                                type="base64",
                                media_type="application/pdf",
                                data=base64.b64encode(payload).decode("ascii"),
                            ),
                        )
                    )
                if len(parts) > 1:
                    prompt += (
                        "\n\n添付PDFは元論文のページ順に分割したものです。出力のページ番号は元PDFの通し番号を使ってください。対応："
                        + json.dumps(
                            [
                                {k: part[k] for k in ("first_page", "last_page")}
                                for part in input_parts
                            ]
                        )
                    )
                if preparation == "images_150dpi_jpeg85":
                    prompt += "\n\n送信用コピーでは大きな埋め込み画像を150dpi・JPEG品質85へ調整しています。文字・ベクトル・全ページは保持しています。図表の細字や微細な数値が判読できない場合は推測せず、不確実性へ記録してください。"
                content.append(dict(type="text", text=prompt))
                argv[argv.index("--output-format") + 1] = "stream-json"
                argv += ["--input-format", "stream-json", "--verbose"]
                prompt = (
                    json.dumps(
                        dict(
                            type="user",
                            message=dict(
                                role="user",
                                content=content,
                            ),
                        ),
                        ensure_ascii=False,
                    )
                    + "\n"
                )
            env = os.environ.copy()
            env.pop("CLAUDECODE", None)
            started = time.monotonic()
            raw = run_bounded(
                argv,
                cwd=cwd,
                text=prompt,
                timeout=config.analysis.timeout_seconds,
                max_bytes=config.analysis.max_output_bytes,
                env=env,
            )
            try:
                if request.document is not None:
                    results = [parse_response(line) for line in raw.splitlines() if line.strip()]
                    envelope = next(r for r in reversed(results) if r.get("type") == "result")
                else:
                    envelope = parse_response(raw)
                if envelope.get("is_error"):
                    error = classify_failure(json.dumps(envelope), "", 0)
                    error.usage = claude_usage(envelope)
                    error.elapsed_seconds = time.monotonic() - started
                    raise error
                data = envelope["structured_output"]
                if isinstance(data, str):
                    data = parse_response(data)
            except (
                ValueError,
                KeyError,
                TypeError,
                AttributeError,
                StopIteration,
            ) as exc:
                raise ProviderError(
                    "invalid_json", "Claudeのstructured_outputを取得できません"
                ) from exc
            models = envelope.get("modelUsage")
            model = config.analysis.model
            if model is None and isinstance(models, dict) and len(models) == 1:
                model = next(iter(models))
            usage = claude_usage(envelope)
            elapsed = time.monotonic() - started
            receipt = dict(
                provider=self.name,
                model=model,
                input_kind="native_pdf" if request.document else "text",
                pdf_sha256=request.document.sha256 if request.document else None,
                page_count=request.document.page_count if request.document else 0,
                serialized_input_bytes=len(prompt.encode("utf-8")),
                duplicate_fulltext=False,
                preparation=preparation,
                input_parts=input_parts,
            )
            data = validate_response(
                data, request, usage=usage, elapsed_seconds=elapsed, input_receipt=receipt
            )
            if preparation == "images_150dpi_jpeg85":
                data["uncertainties_ja"].append(
                    "PaperFlowの入力処理：送信用コピーの大きな埋め込み画像を150dpi・JPEG品質85へ調整した。全ページと文字・ベクトル情報は保持し、原PDFは変更していない。図表の細字や微細な数値は原PDFで照合する。"
                )
            return AnalysisResult(
                data,
                self.name,
                model,
                elapsed_seconds=elapsed,
                usage=usage,
            )
