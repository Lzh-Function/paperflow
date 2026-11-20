import json
import tempfile
import time
from pathlib import Path

from paperflow.agents.base import AnalysisResult, ProviderError, authorize_request
from paperflow.agents.json_response import parse_analysis_response, response_error
from paperflow.agents.process import checked_help, resolve_command, run_bounded
from paperflow.agents.schema import make_prompt, schema_for, validate_response
from paperflow.agents.usage import extract


class CodexCliProvider:
    name = "codex"

    def __init__(self, config):
        self.config = config

    def analyze(self, request):
        config = self.config
        authorize_request(config, request)
        command = resolve_command(config.analysis.codex_command)
        staging = config.storage.data_dir / "staging"
        staging.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="codex-", dir=staging) as name:
            cwd = Path(name)
            checked_help(
                command,
                ["exec"],
                [
                    "--sandbox",
                    "--output-schema",
                    "--output-last-message",
                    "--skip-git-repo-check",
                    "--ephemeral",
                    "--ignore-user-config",
                    "--ignore-rules",
                    "--json",
                ],
                cwd,
            )
            schema_path, output = cwd / "schema.json", cwd / "analysis.json"
            schema_path.write_text(json.dumps(schema_for(request)), encoding="utf-8")
            # Ignore user MCP/plugins/hooks; the isolated cwd contains no project config.
            argv = [
                command,
                "-a",
                "never",
                "exec",
                "--json",
                "--sandbox",
                "read-only",
                "--ignore-user-config",
                "--ignore-rules",
                "--skip-git-repo-check",
                "--ephemeral",
                "--output-schema",
                str(schema_path),
                "-o",
                str(output),
                "-c",
                'web_search="disabled"',
                "-c",
                "features.shell_tool=false",
                "-c",
                "features.apply_patch_freeform=false",
                "-c",
                "features.apps=false",
                "-c",
                "features.js_repl=false",
                "-c",
                "features.multi_agent=false",
                "-c",
                "mcp_servers={}",
                "-c",
                "plugins={}",
                "-c",
                "hooks={}",
                "-c",
                "tools.view_image=false",
            ]
            if config.analysis.model:
                argv += ["--model", config.analysis.model]
            if request.document is not None:
                from paperflow.agents.document import page_images, snapshot

                checked_help(command, ["exec"], ["--image"], cwd)
                pdf = snapshot(request.document, cwd, config)
                for path in page_images(
                    pdf, cwd, max_bytes=config.analysis.visual_max_image_mb * 1024 * 1024
                ):
                    argv += ["--image", str(path)]
            argv.append("-")
            started = time.monotonic()
            raw = run_bounded(
                argv,
                cwd=cwd,
                text=make_prompt(request),
                timeout=config.analysis.timeout_seconds,
                max_bytes=config.analysis.max_output_bytes,
            )
            usage = extract(self.name, raw)
            elapsed = time.monotonic() - started
            receipt = dict(
                provider=self.name,
                model=config.analysis.model,
                input_kind="page_images" if request.document else "text",
                pdf_sha256=request.document.sha256 if request.document else None,
                page_count=request.document.page_count if request.document else 0,
            )
            response = None
            try:
                if output.stat().st_size > config.analysis.max_output_bytes:
                    raise ProviderError("output_limit", "Codex最終JSONが出力量上限を超えました")
                response = output.read_text(encoding="utf-8")
                data = parse_analysis_response(response)
            except (OSError, ValueError) as exc:
                error = response_error(
                    "Codexの最終JSONファイルを読み取れません", exc, raw=response, receipt=receipt
                )
                error.usage, error.elapsed_seconds = usage, elapsed
                raise error from exc
            return AnalysisResult(
                validate_response(
                    data,
                    request,
                    usage=usage,
                    elapsed_seconds=elapsed,
                    input_receipt=receipt,
                    raw_response=response,
                ),
                self.name,
                config.analysis.model,
                elapsed_seconds=elapsed,
                usage=usage,
            )
