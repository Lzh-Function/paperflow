"""外部CLIへ合成した短文だけを送信する明示実行の疎通試験。実PDF・Vaultは参照しない。"""

import argparse
import json
import tempfile
from pathlib import Path

from paperflow.agents.base import AnalysisRequest, ProviderError
from paperflow.agents.claude_cli import ClaudeCliProvider
from paperflow.agents.codex_cli import CodexCliProvider
from paperflow.config import Analysis, Config, Destination, Source, Storage


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=["claude", "codex"], action="append")
    parser.add_argument("--output", type=Path, default=Path(".data/provider-smoke.json"))
    args = parser.parse_args()
    results = []
    with tempfile.TemporaryDirectory(prefix="paperflow-smoke-") as name:
        root = Path(name)
        config = Config(
            source=Source(paperpile_dir=root / "unused-source"),
            destination=Destination(vault_dir=root / "unused-vault"),
            storage=Storage(data_dir=root / "local", database=root / "state.db"),
            analysis=Analysis(timeout_seconds=120, max_budget_usd=0.5),
        )
        request = AnalysisRequest(
            "synthetic",
            {"title": "合成ソフトウェア試験", "year": "2026"},
            "これはPaperFlowの疎通試験のために作成した架空の抄録です。"
            "手法は整数1と2を足すことであり、結果は3でした。実際の論文や研究成果ではありません。"
            "対象はソフトウェア試験だけで、研究上の有用性は評価していません。",
            "abstract",
            [],
            ["other"],
        )
        for provider in args.provider or ["claude", "codex"]:
            try:
                cls = ClaudeCliProvider if provider == "claude" else CodexCliProvider
                result = cls(config).analyze(request)
                results.append(
                    dict(
                        provider=provider,
                        status="success",
                        coverage=result.data["coverage"],
                        elapsed_seconds=round(result.elapsed_seconds, 2),
                        model=result.model,
                    )
                )
            except ProviderError as exc:
                results.append(
                    dict(
                        provider=provider, status="failed", category=exc.category, message=str(exc)
                    )
                )
            print(json.dumps(results[-1], ensure_ascii=False), flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return int(any(r["status"] != "success" for r in results))


if __name__ == "__main__":
    raise SystemExit(main())
