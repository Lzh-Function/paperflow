"""外部CLIへ合成した短文だけを送信する明示実行の疎通試験。実PDF・Vaultは参照しない。"""

import argparse
import json
import tempfile
from pathlib import Path

from paperflow.agents.agy_cli import AgyCliProvider
from paperflow.agents.base import AnalysisRequest, ProviderError
from paperflow.agents.claude_cli import ClaudeCliProvider
from paperflow.agents.codex_cli import CodexCliProvider
from paperflow.config import Analysis, Config, Destination, Source, Storage


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=["claude", "codex", "agy"], action="append")
    parser.add_argument("--model", help="疎通試験に使用するモデルID")
    parser.add_argument(
        "--visual", action="store_true", help="合成した2ページの図付きPDFで試験する"
    )
    parser.add_argument("--output", type=Path, default=Path(".data/provider-smoke.json"))
    args = parser.parse_args()
    results = []
    with tempfile.TemporaryDirectory(prefix="paperflow-smoke-") as name:
        root = Path(name)
        config = Config(
            source=Source(paperpile_dir=root / "unused-source"),
            destination=Destination(vault_dir=root / "unused-vault"),
            storage=Storage(data_dir=root / "local", database=root / "state.db"),
            analysis=Analysis(timeout_seconds=120, max_budget_usd=0.5, model=args.model),
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
        if args.visual:
            import pymupdf

            from paperflow.agents.base import PDFDocument
            from paperflow.wiki.bundle import sha

            pdf = root / "synthetic.pdf"
            with pymupdf.open() as doc:
                for i, color in enumerate([(1, 0, 0), (0, 0, 1)], 1):
                    page = doc.new_page(width=400, height=400)
                    page.draw_rect(pymupdf.Rect(60, 80, 300, 260), fill=color)
                    page.insert_text((60, 285), f"Fig. {i} Synthetic visual")
                doc.save(pdf)
            config.analysis.allow_external_fulltext = True
            config.analysis.policy = "fulltext_external"
            request.document = PDFDocument(pdf, sha(pdf), 2, 3)
            request.coverage = "fulltext"
            request.pages_available = [1, 2]
            request.text = "[page 1]\nFig. 1 Synthetic visual\n[page 2]\nFig. 2 Synthetic visual"
            request.research_context = "合成図表の視覚読解試験。各ページの四角の色を、ページ番号とともに主な結果へ記録する。抽出本文には色がないので、PDFを視覚読解する。"
        for provider in args.provider or ["claude", "codex", "agy"]:
            try:
                cls = {
                    "claude": ClaudeCliProvider,
                    "codex": CodexCliProvider,
                    "agy": AgyCliProvider,
                }[provider]
                result = cls(config).analyze(request)
                results.append(
                    dict(
                        provider=provider,
                        status="success",
                        coverage=result.data["coverage"],
                        elapsed_seconds=round(result.elapsed_seconds, 2),
                        model=result.model,
                        usage=result.usage,
                        findings=result.data["key_findings_ja"] if args.visual else None,
                        visual_review=result.data.get("visual_review"),
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
