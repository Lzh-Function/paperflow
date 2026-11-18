from paperflow.agents.base import AnalysisRequest, AnalysisResult
from paperflow.agents.schema import validate


class FakeProvider:
    name = "fake"

    def analyze(self, request: AnalysisRequest) -> AnalysisResult:
        data = dict(
            tldr_ja="オフライン動作確認用の出力です。実際の論文要約・評価は行っていません。",
            research_question_ja="未評価（FakeProvider）",
            approach_ja="未評価（FakeProvider）",
            key_findings_ja=[],
            limitations_ja=["動作確認用であり、研究上の根拠には使えません。"],
            research_relevance_ja="未評価",
            tags=["other"] if "other" in request.taxonomy else [],
            tag_suggestions=[],
            priority=1,
            priority_reason_ja="試験用の固定値。実評価ではありません。",
            evidence=[],
            coverage=request.coverage,
            uncertainties_ja=["FakeProviderによる合成結果です。"],
        )
        return AnalysisResult(validate(data, request), self.name, "offline-fixture")
