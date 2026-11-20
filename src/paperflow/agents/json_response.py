"""JSONの構造を推測せず、回答文字列の明確な表記不備だけを正規化する。"""

import json
import math
import re

from paperflow.agents.base import ProviderError


def _unwrap(text):
    text = text.lstrip("\ufeff").strip()
    fenced = re.fullmatch(r"```(?:json)?\s*\n(.*?)\n```", text, flags=re.S | re.I)
    return fenced[1] if fenced else text


def _decode(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"JSONのキーが重複しています: {key}")
            result[key] = value
        return result

    def invalid(value):
        raise ValueError(f"JSONに非有限数があります: {value}")

    def floating(value):
        result = float(value)
        return result if math.isfinite(result) else invalid(value)

    def unicode_strings(value):
        if isinstance(value, str):
            try:
                value.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise ValueError("JSON文字列に単独のサロゲートがあります") from exc
        elif isinstance(value, dict):
            for k, v in value.items():
                unicode_strings(k)
                unicode_strings(v)
        elif isinstance(value, list):
            for v in value:
                unicode_strings(v)

    try:
        data = json.loads(
            text, object_pairs_hook=pairs, parse_constant=invalid, parse_float=floating
        )
        unicode_strings(data)
        return data
    except RecursionError as exc:
        raise ValueError("JSONの入れ子が深すぎます") from exc


def parse_response(text):
    """CLIの外枠は厳密に読む。BOMと単一JSONフェンス以外は変更しない。"""
    return _decode(_unwrap(text))


def _string_encoding(text):
    output, counts = [], dict(backslash=0, control=0)
    i = 0
    while i < len(text):
        char = text[i]
        output.append(char)
        i += 1
        if char != '"':
            continue
        changed = False
        while i < len(text):
            char = text[i]
            if char == '"':
                output.append(char)
                i += 1
                # 機械用キーは補正しない。キー衝突の検出も後段の厳密なJSON読込に任せる。
                following = i
                while following < len(text) and text[following].isspace():
                    following += 1
                if changed and text[following : following + 1] == ":":
                    raise ValueError("JSONキーの特殊記号は自動補正しません")
                break
            if char == "\\":
                if i + 1 >= len(text):
                    raise ValueError("JSON文字列が途中で終了しています")
                following = text[i + 1]
                if following in '"\\/bfnrtu':
                    # 正しいエスケープと、解釈が曖昧な不完全な\\uXXXXには触れない。
                    output.append(text[i : i + 2])
                    i += 2
                    continue
                output.append("\\\\")
                counts["backslash"] += 1
                changed = True
            elif ord(char) < 32:
                output.append(json.dumps(char)[1:-1])
                counts["control"] += 1
                changed = True
            else:
                output.append(char)
            i += 1
        else:
            raise ValueError("JSON文字列が途中で終了しています")
    return "".join(output), counts


def parse_analysis_response(text):
    """要約本文だけに適用する。補正後も厳密な読込と通常の全検証が必要。"""
    text = _unwrap(text)
    try:
        return _decode(text)
    except json.JSONDecodeError as exc:
        if not exc.msg.startswith(("Invalid \\escape", "Invalid control character")):
            raise
    normalized, counts = _string_encoding(text)
    try:
        data = _decode(normalized)
    except ValueError as exc:
        # 補正後も不採用なら、エラー位置がどちらの文字列を指すかを診断に残す。
        exc.normalized_response = normalized
        raise
    if isinstance(data, dict) and isinstance(data.get("uncertainties_ja"), list):
        parts = []
        if counts["backslash"]:
            parts.append(f"バックスラッシュ{counts['backslash']}箇所")
        if counts["control"]:
            parts.append(f"改行・タブ等の制御文字{counts['control']}箇所")
        note = (
            "PaperFlowの形式補正：JSON文字列中の"
            + "、".join(parts)
            + "をエスケープした。文字・記号は削除せず、本文の主張と読解範囲は補完していない。"
        )
        if note not in data["uncertainties_ja"]:
            data["uncertainties_ja"].append(note)
    return data


def response_error(message, cause, *, raw=None, receipt=None):
    detail = dict(type=type(cause).__name__)
    normalized = getattr(cause, "normalized_response", None)
    if isinstance(cause, json.JSONDecodeError):
        # BOM・コードフェンスやCLIの外枠を除いたJSON断片の位置も取り違えない。
        if normalized is None and raw is not None and cause.doc != raw:
            normalized = cause.doc
        detail.update(message=cause.msg, line=cause.lineno, column=cause.colno, offset=cause.pos)
        detail["position_source"] = (
            "normalized_response" if normalized is not None else "raw_response"
        )
        reasons = {
            "Invalid \\escape": "バックスラッシュの表記が不正です",
            "Invalid \\uXXXX escape": "Unicodeエスケープが不完全です",
            "Extra data": "JSONの外に追加の内容があります",
            "Expecting ',' delimiter": "区切り記号が不足しています",
            "Unterminated string starting at": "文字列が途中で終了しています",
        }
        reason = reasons.get(cause.msg, "JSONの構造を確認してください")
        detail["reason_ja"] = reason
        message += f"（JSON構文エラー: {cause.lineno}行{cause.colno}列。{reason}）"
    elif isinstance(cause, ValueError):
        detail["message"] = str(cause)
        safe = str(cause).encode("utf-8", errors="backslashreplace").decode("utf-8")[:400]
        message += "（" + safe + "）"
    error = ProviderError("invalid_json", message)
    if raw is not None:
        error.raw_response = raw
    if normalized is not None:
        error.normalized_response = normalized
    error.parse_error = detail
    error.input_receipt = receipt
    return error
