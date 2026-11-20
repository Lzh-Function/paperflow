"""論文PDFの図・表を元の画面から切り出す。OCRや外部LLMは使わない。"""

import argparse
import hashlib
import json
import re
import unicodedata
from pathlib import Path

import pymupdf

VERSION = "paperflow-visuals-v1"
NUMBER_PATTERN = r"(?:\d+[A-Za-z]?|[A-Za-z]{1,3}\d+|[A-Za-z]|[IVXLCDMivxlcdm]+)(?:[.-][A-Za-z0-9]+)*(?:\([A-Za-z0-9]+\))?"
CAPTION = re.compile(rf"^(Fig(?:ure)?\.?|Table|図|表)\s*({NUMBER_PATTERN})(?=[.:\s]|$)", re.I)


def canonical_number(value):
    if not isinstance(value, str):
        return value
    if len(value) > 80:
        return value
    text = unicodedata.normalize("NFKC", value).strip()
    text = re.sub(r"^(?:Fig(?:ure)?\.?|Table|図|表)\s*", "", text, flags=re.I).rstrip(".:").strip()
    if re.fullmatch(NUMBER_PATTERN, text):
        return int(text) if text.isdecimal() else text
    return value


def number_from_title(title, kind):
    if not isinstance(title, str) or kind not in {"figure", "table"}:
        return None
    match = CAPTION.match(unicodedata.normalize("NFKC", title).strip())
    if match and ("table" if match[1].lower() in {"table", "表"} else "figure") == kind:
        return canonical_number(match[2])
    return None


def number_key(value):
    return str(canonical_number(value)).casefold() if value is not None else None


def filename_number(value):
    # 表示番号はそのまま記録し、ファイル名は従来と同じ数字だけにする。
    # 小数・付録番号も同じ数字へ丸めず、安定した別名にする。
    return (
        value
        if type(value) is int
        else int.from_bytes(hashlib.sha256(str(value).encode("utf-8")).digest()[:12], "big")
    )


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def captions(page):
    result = []
    for block in page.get_text("dict")["blocks"]:
        if "lines" not in block:
            continue
        lines = ["".join(s["text"] for s in line["spans"]).strip() for line in block["lines"]]
        text = " ".join(lines)
        found = CAPTION.match(text)
        if not found or re.match(
            r"\s*(shows|summarizes|illustrates|presents|lists|reports|compares)\b",
            text[found.end() :],
            re.I,
        ):
            continue
        result.append(
            dict(
                kind="table" if found[1].lower() in {"table", "表"} else "figure",
                number=canonical_number(found[2]),
                caption=text[:400],
                rect=list(block["bbox"]),
            )
        )
    return result


def crop(page, caption):
    box = pymupdf.Rect(caption["rect"])
    if caption["kind"] == "figure":
        regions = [pymupdf.Rect(i["bbox"]) for i in page.get_image_info()]
        try:
            regions += [pymupdf.Rect(r) for r in page.cluster_drawings()]
        except (AttributeError, ValueError):
            pass
        candidates = [
            r
            for r in regions
            if r.width >= 50
            and r.height >= 30
            and r.y1 <= box.y0 + 3
            and box.y0 - r.y1 <= 70
            and min(r.x1, box.x1) - max(r.x0, box.x0) > min(r.width, box.width) * 0.4
        ]
        if candidates:
            candidate = min(candidates, key=lambda r: abs(box.y0 - r.y1))
            # 複数panelのvector図は近い同一帯域をまとめる。
            for other in candidates:
                if abs(other.y1 - candidate.y1) < 12 and abs(other.y0 - candidate.y0) < 20:
                    candidate |= other
            # vectorの境界には軸名・入力ラベル等の文字が含まれない。近傍の短い文字欄も
            # 全体を含め、文字を端で切らない。図表captionや長い本文段落は含めない。
            vicinity = candidate + (-20, -20, 20, 12)
            for block in page.get_text("blocks"):
                label = pymupdf.Rect(block[:4])
                if (
                    label.height <= 40
                    and label.width <= candidate.width + 40
                    and label.y1 < box.y0
                    and label.intersects(vicinity)
                ):
                    candidate |= label
            return (candidate + (-3, -3, 3, 3)) & page.rect, "crop"
    else:
        rules = []
        for drawing in page.get_drawings():
            r = drawing["rect"]
            if (
                r.height <= 3
                and box.width * 0.7 <= r.width <= box.width * 1.25
                and box.y1 <= r.y0 <= box.y1 + 300
            ):
                if abs(r.x0 - box.x0) < 12:
                    rules.append(r)
        if len(rules) >= 2:
            top = min(r.y0 for r in rules)
            bottom = max(r.y1 for r in rules)
            if 20 <= bottom - top <= 300:
                return pymupdf.Rect(
                    min(r.x0 for r in rules) - 3, top - 4, max(r.x1 for r in rules) + 3, bottom + 4
                ) & page.rect, "crop"
    # 境界を推測して内容を落とすより、原ページを明示して添える。
    return page.rect, "page-context"


def choose(candidates, maximum):
    selected = []
    figures = [c for c in candidates if c["kind"] == "figure"]
    tables = [c for c in candidates if c["kind"] == "table"]
    architecture = [
        c
        for c in figures
        if re.search(r"architecture|framework|overview|pipeline", c["caption"], re.I)
    ]
    if architecture or figures:
        selected.append((architecture or figures)[0])
    if tables:
        selected.append(tables[0])
    for c in candidates:
        if len(selected) >= maximum:
            break
        if c in selected:
            continue
        # 同じページだけで埋めず、結果図などを別ページから優先する。
        figure_pages = [s["page"] for s in selected if s["kind"] == "figure"]
        if (
            c["kind"] == "figure"
            and c["page"] not in {s["page"] for s in selected}
            and (not figure_pages or c["page"] >= min(figure_pages))
        ):
            selected.append(c)
    for c in candidates:
        if len(selected) >= maximum:
            break
        if c not in selected:
            selected.append(c)
    return selected[:maximum]


def extract_visuals(pdf, output, *, expected_sha256, source_id, maximum=3, selections=None):
    pdf = Path(pdf)
    output = Path(output)
    if not 1 <= maximum <= 8:
        raise ValueError("図表は1〜8件を指定してください")
    if sha(pdf) != expected_sha256:
        raise ValueError("PDFが記録したハッシュと一致しません")
    record = output / "visuals.json"
    if record.exists():
        data = json.loads(record.read_text(encoding="utf-8"))
        if (
            data.get("pdf_sha256") != expected_sha256
            or data.get("source_id") != source_id
            or data.get("version") != VERSION
            or data.get("maximum") != maximum
            or data.get("selections") != selections
        ):
            raise ValueError(
                "既存の図表は別の入力・設定です。新しい出力ディレクトリを指定してください"
            )
        for item in data["items"]:
            path = output / item["file"]
            if (
                path.parent.resolve() != output.resolve()
                or not path.is_file()
                or sha(path) != item["sha256"]
            ):
                raise ValueError("保存済み図表が変更または欠落しています")
        return data
    if output.exists() and any(output.iterdir()):
        raise ValueError("管理外の図表ファイルを上書きしません")
    output.mkdir(parents=True, exist_ok=True)
    candidates = []
    with pymupdf.open(pdf) as doc:
        if doc.needs_pass:
            raise ValueError("暗号化PDFは図表を自動抽出できません")
        for number, page in enumerate(doc, 1):
            for candidate in captions(page):
                candidate["page"] = number
                candidates.append(candidate)
        if selections is not None:
            if len(selections) > maximum:
                raise ValueError("AIの図表選択が指定件数を超えています")
            picks = []
            for selection in selections:
                number = selection["page"]
                if not 1 <= number <= len(doc):
                    raise ValueError("AIが選んだ図表ページがPDFに存在しません")
                matches = [
                    c
                    for c in candidates
                    if c["page"] == number
                    and c["kind"] == selection["kind"]
                    and selection["number"] is not None
                    and number_key(c["number"]) == number_key(selection["number"])
                ]
                if len(matches) == 1:
                    candidate = dict(matches[0])
                else:
                    candidate = dict(
                        kind="page",
                        number=number,
                        page=number,
                        caption="",
                        rect=list(doc[number - 1].rect),
                    )
                candidate["selection"] = selection
                picks.append(candidate)
        else:
            picks = choose(candidates, maximum)
        if not picks and len(doc) and selections is None:
            picks = [dict(kind="page", number=1, page=1, caption="", rect=list(doc[0].rect))]
        items = []
        filenames = set()
        for index, candidate in enumerate(picks):
            page = doc[candidate["page"] - 1]
            if candidate["kind"] == "page":
                rect, mode = page.rect, "page-context"
            else:
                rect, mode = crop(page, candidate)
            token = filename_number(candidate["number"])
            filename = f"{candidate['kind']}-{token}-p{candidate['page']:03d}.png"
            if filename in filenames:
                token = filename_number(f"{candidate['number']}:{index}")
                filename = f"{candidate['kind']}-{token}-p{candidate['page']:03d}.png"
            filenames.add(filename)
            path = output / filename
            page.get_pixmap(matrix=pymupdf.Matrix(2, 2), clip=rect, alpha=False).save(path)
            label = {"figure": "図", "table": "表", "page": "ページ"}[candidate["kind"]] + str(
                candidate["number"]
            )
            items.append(
                dict(
                    kind=candidate["kind"],
                    number=candidate["number"],
                    page=candidate["page"],
                    label=label,
                    file=filename,
                    bbox=[round(v, 2) for v in rect],
                    mode=mode,
                    sha256=sha(path),
                    review_status="unreviewed",
                )
            )
            if "selection" in candidate:
                items[-1]["selection"] = candidate["selection"]
    if sha(pdf) != expected_sha256:
        raise ValueError("抽出中にPDFが変わりました")
    data = dict(
        version=VERSION,
        pdf_sha256=expected_sha256,
        source_id=source_id,
        maximum=maximum,
        selections=selections,
        selection_method="agent" if selections is not None else "caption_heuristic",
        items=items,
    )
    record.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return data


def main():
    import yaml

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--max-items", type=int, default=3)
    parser.add_argument(
        "--analysis-json", type=Path, help="視覚読解済み解析JSONの図表選択を利用する"
    )
    args = parser.parse_args()
    source = args.source_dir.resolve()
    manifest = yaml.safe_load((source / "manifest.yml").read_text(encoding="utf-8"))
    if manifest.get("source_kind") != "paper" or manifest.get("source_status") != "frozen":
        parser.error("登録済みの凍結論文を指定してください")
    selections = None
    if args.analysis_json:
        analysis = json.loads(args.analysis_json.read_text(encoding="utf-8"))
        review = analysis.get("visual_review", {})
        if review.get("status") != "completed" or not isinstance(
            review.get("selected_visuals"), list
        ):
            parser.error("視覚読解完了の図表選択がありません")
        selections = review["selected_visuals"]
    data = extract_visuals(
        source / "source.pdf",
        source / "figures" / VERSION,
        expected_sha256=manifest["sha256"],
        source_id=manifest["source_id"],
        maximum=args.max_items,
        selections=selections,
    )
    print(
        json.dumps(
            dict(items=len(data["items"]), output=str(source / "figures" / VERSION)),
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
