import json
import re
from copy import deepcopy
from importlib.resources import files

from jsonschema import Draft7Validator

from paperflow.agents.base import AnalysisRequest, ProviderError
from paperflow.agents.json_response import parse_response as parse_response
from paperflow.resources.wiki_extract_visuals import (
    NUMBER_PATTERN,
    canonical_number,
    number_from_title,
)

SCHEMA_VERSION = "v1"
SCHEMA = json.loads(
    files("paperflow.resources").joinpath("analysis.schema.json").read_text("utf-8")
)
PROMPT = files("paperflow.resources").joinpath("analyze_v1.md").read_text("utf-8")
VISUAL_PROMPT = files("paperflow.resources").joinpath("analyze_visual.md").read_text("utf-8")
VISUAL_SCHEMA = deepcopy(SCHEMA)
VISUAL_SCHEMA["required"].append("visual_review")
VISUAL_SCHEMA["properties"]["visual_review"] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "status": {"type": "string", "enum": ["completed", "unable"]},
        "reason_ja": {"type": "string"},
        "pages_viewed": {
            "type": "array",
            "items": {"type": "integer", "minimum": 1},
        },
        "selected_visuals": {
            "type": "array",
            "maxItems": 8,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "page": {"type": "integer", "minimum": 1},
                    "kind": {"type": "string", "enum": ["figure", "table", "page"]},
                    "number": {
                        "type": ["integer", "string", "null"],
                        "minimum": 1,
                        "minLength": 1,
                        "maxLength": 64,
                        "pattern": "^" + NUMBER_PATTERN + "$",
                    },
                    "title_ja": {"type": "string", "minLength": 1},
                    "explanation_ja": {"type": "string", "minLength": 1},
                    "finding_indices": {
                        "type": "array",
                        "items": {"type": "integer", "minimum": 1},
                    },
                    "uncertainty_ja": {"type": "string"},
                },
                "required": [
                    "page",
                    "kind",
                    "number",
                    "title_ja",
                    "explanation_ja",
                    "finding_indices",
                    "uncertainty_ja",
                ],
            },
        },
    },
    "required": ["status", "reason_ja", "pages_viewed", "selected_visuals"],
}


def schema_for(request):
    return VISUAL_SCHEMA if request.document is not None else SCHEMA


def normalize_response(data):
    """内容を作らず、位置番号の表記と単純な重複だけを正規化する。"""
    if not isinstance(data, dict):
        return data
    result = deepcopy(data)
    changes = set()

    def integer(value):
        if isinstance(value, str) and len(value) <= 12 and re.fullmatch(r"\s*0*[1-9]\d*\s*", value):
            changes.add("ページ・参照番号の数字表記")
            return int(value)
        return value

    def unique(values):
        if isinstance(values, list) and all(type(v) is int for v in values):
            cleaned = list(dict.fromkeys(values))
            if cleaned != values:
                changes.add("ページ・参照番号の重複")
            return cleaned
        return values

    if isinstance(result.get("evidence"), list):
        for entry in result["evidence"]:
            if isinstance(entry, dict) and "page" in entry:
                entry["page"] = integer(entry["page"])
    review = result.get("visual_review")
    if isinstance(review, dict):
        pages = review.get("pages_viewed")
        if isinstance(pages, list):
            review["pages_viewed"] = unique([integer(p) for p in pages])
        selections = review.get("selected_visuals")
        if isinstance(selections, list):
            cleaned = []
            for selection in selections:
                if not isinstance(selection, dict):
                    cleaned.append(selection)
                    continue
                if "page" in selection:
                    selection["page"] = integer(selection["page"])
                refs = selection.get("finding_indices")
                if isinstance(refs, list):
                    selection["finding_indices"] = unique([integer(i) for i in refs])
                number = selection.get("number")
                canonical = canonical_number(number)
                if number is None:
                    canonical = number_from_title(selection.get("title_ja"), selection.get("kind"))
                if canonical != number:
                    selection["number"] = canonical
                    changes.add("図表番号の表記")
                if selection in cleaned:
                    changes.add("同一内容の図表選択の重複")
                else:
                    cleaned.append(selection)
            review["selected_visuals"] = cleaned
    if changes and isinstance(result.get("uncertainties_ja"), list):
        note = (
            "PaperFlowの形式補正："
            + "、".join(sorted(changes))
            + "を整理した。本文の主張と読解範囲は補完していない。"
        )
        if note not in result["uncertainties_ja"]:
            result["uncertainties_ja"].append(note)
    return result


def validate(data: dict, request: AnalysisRequest) -> dict:
    data = normalize_response(data)
    if not isinstance(data, dict):
        raise ProviderError("schema", "解析JSONの最上位はオブジェクトである必要があります")
    schema = VISUAL_SCHEMA if request.document is not None or "visual_review" in data else SCHEMA
    errors = list(Draft7Validator(schema).iter_errors(data))
    if errors:
        detail = " / ".join(
            (".".join(map(str, e.absolute_path)) or "回答全体") + ": " + e.validator
            for e in errors[:3]
        )
        raise ProviderError("schema", "解析JSONが共通Schemaに適合しません: " + detail)
    if not set(data["tags"]) <= set(request.taxonomy):
        raise ProviderError("taxonomy", "管理語彙外のタグが返されました")
    if data["coverage"] != request.coverage:
        raise ProviderError("coverage", "入力と出力のcoverageが一致しません")
    if any(
        e["page"] is not None and e["page"] not in request.pages_available for e in data["evidence"]
    ):
        raise ProviderError("evidence", "入力にないページ番号が返されました")
    if "visual_review" in data:
        review = data["visual_review"]
        seen = set(review["pages_viewed"])
        if len(seen) != len(review["pages_viewed"]):
            raise ProviderError("visual_reading", "視覚読解ページが重複しています")
        expected = (
            set(range(1, request.document.page_count + 1))
            if request.document
            else set(request.pages_available)
        )
        if review["status"] != "completed" or not seen or not seen <= expected:
            reason = " ".join(review["reason_ja"].split())[:600]
            detail = (
                "AIが視覚読解を完了できないと回答しました"
                if review["status"] != "completed"
                else "視覚読解のページ記録が空、または添付範囲外です"
            )
            raise ProviderError(
                "visual_reading",
                detail
                + ("：" + reason if reason else "。AIから理由の記載がありません")
                + "（文字だけの結果は採用しません）",
            )
        if seen != expected:
            missing = sorted(expected - seen)
            raise ProviderError(
                "visual_reading",
                f"全{len(expected)}ページ中{len(seen)}ページの読解記録です。"
                + "未確認ページ："
                + ", ".join(map(str, missing[:20]))
                + (" ほか" if len(missing) > 20 else ""),
            )
        selections = review["selected_visuals"]
        if request.document and len(selections) > request.document.max_selected_visuals:
            raise ProviderError("visual_reading", "選択した図表が指定件数を超えています")
        # 番号不明の別図を同じ図と決めつけない。同じ番号の異なる説明も保持する。
        for index, visual in enumerate(selections, 1):
            if visual["page"] not in seen:
                raise ProviderError(
                    "visual_reading", f"図表{index}のPDFページ{visual['page']}が読解範囲外です"
                )
            invalid = [i for i in visual["finding_indices"] if i > len(data["key_findings_ja"])]
            if invalid:
                raise ProviderError(
                    "visual_reading",
                    f"図表{index}の関連所見番号{invalid}が所見数{len(data['key_findings_ja'])}を超えています",
                )
    return data


def validate_response(
    data, request, *, usage, elapsed_seconds, input_receipt=None, raw_response=None
):
    """採用できない回答でも、取得済みの消費量と所要時間を失わない。"""
    try:
        return validate(data, request)
    except ProviderError as exc:
        exc.usage = usage
        exc.elapsed_seconds = elapsed_seconds
        exc.rejected_response = data
        exc.input_receipt = input_receipt
        if raw_response is not None:
            exc.raw_response = raw_response
        raise


def make_prompt(request: AnalysisRequest, *, text_in_pdf=False) -> str:
    from dataclasses import asdict

    payload = asdict(request)
    payload.pop("document")
    if text_in_pdf:
        if request.document is None:
            raise ValueError("PDFなしでは補助本文を省略できません")
        # PDFは本文と各ページの視覚情報を含む。同じ全文を二重に投入しない。
        payload["text"] = "本文はこのメッセージに直接添付したPDFを参照してください。"
        payload["text_source"] = "attached_pdf"
    extra = ""
    if request.document:
        extra = VISUAL_PROMPT
        payload["visual_input"] = dict(
            page_count=request.document.page_count,
            max_selected_visuals=request.document.max_selected_visuals,
        )
    prompt = (
        PROMPT.replace("メタデータとテキストだけ", "メタデータ、テキストと添付PDF・画像だけ")
        if request.document
        else PROMPT
    )
    return prompt + extra + "\n\n非信頼の資料JSON:\n" + json.dumps(payload, ensure_ascii=False)
