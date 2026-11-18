import re
from datetime import UTC, datetime

from paperflow.obsidian.renderer import escape, frontmatter
from paperflow.subject_tags import subject_tags


def stamp():
    return datetime.now(UTC).isoformat()


def base(identifier, kind, title, source_id, date):
    return dict(
        id=identifier,
        type=kind,
        title=title,
        area="academic",
        status="review-pending",
        owner="agent",
        created=date,
        updated=date,
        aliases=[],
        tags=[
            "note/paper" if kind == "paper-note" else "note/output",
            "area/academic",
            "source/paper",
        ],
        topics=[],
        projects=[],
        source_ids=[source_id],
        related_concepts=[],
        related_claims=[],
        source_notes=[],
        review_status="unreviewed",
        agent_editable=True,
        evidence_status="unverified",
        paperflow_managed=True,
    )


def safe_name(title):
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f\[\]#^]', " ", title)
    return " ".join(text.split())[:85].rstrip(". ") or "論文"


def locator_reference(value):
    # locatorには長い原文引用が混ざり得る。Wikiには位置情報だけを表示する。
    positions = re.findall(
        r"(?:\b(?:Table|Fig(?:ure)?\.?|Section|Eqn?\.?)\s*\(?\d+(?:\.\d+)*\)?)|(?:\d+(?:\.\d+)*節)",
        value or "",
        flags=re.IGNORECASE,
    )
    return "、".join(dict.fromkeys(positions)) or "詳細位置は保存済み解析JSONを参照"


def render(
    item,
    source_id,
    note_path,
    proposal_path,
    blocks,
    candidates,
    visuals=None,
    *,
    visual_subdir="figures/paperflow-visuals-v1",
):
    paper, meta, run = item["paper"], item["metadata"], item["analysis"]
    data = run["data"]
    date = run["started_at"][:10]
    slug = source_id.split(":", 1)[1]
    source_dir = f"10_Papers/10_Sources/{slug}"
    title = meta["title"]
    proposal_link = f"[[{proposal_path[:-3]}]]"
    fm = base(source_id, "paper-note", title, source_id, date)
    fm["tags"] += subject_tags(data["tags"])
    fm.update(
        authors=meta["authors"],
        year=int(meta["year"]) if str(meta["year"]).isdigit() else None,
        doi=meta.get("doi"),
        venue=meta.get("venue"),
        source_id=source_id,
        source_file=f"[[{source_dir}/source.pdf]]",
        topics=data["tags"],
        priority=data["priority"],
        coverage=data["coverage"],
        provider=run["provider"],
        model=run.get("model"),
        analysis_run_id=run["run_id"],
        paperflow_id=paper["paper_id"],
        source_hash=item["pdf_hash"],
        integration_status="proposal-pending",
        integration_proposal=proposal_link,
        text_truncated=item.get("text_truncated", data["coverage"] == "partial"),
    )
    text = frontmatter(fm) + f"# {escape(title)}\n\n"
    text += "> [!warning] 根拠の照合とWiki統合はレビュー待ち\n> 出典は凍結登録済みです。以下は保存済みAI解析の再利用で、原典照合済みの主張としては扱いません。\n\n"
    text += f"- 凍結PDF: [[{source_dir}/source.pdf|原本の保存版]]\n"
    text += f"- ページ付き抽出: [[{source_dir}/extracted.md|抽出本文]]\n"
    text += f"- Wikiへの統合提案: {proposal_link}\n\n"
    for heading, key in [
        ("一文要約", "tldr_ja"),
        ("研究課題", "research_question_ja"),
        ("手法・実験設定", "approach_ja"),
    ]:
        if key == "tldr_ja":
            points = re.findall(r"[^。]+。?", data[key])
            text += (
                "## 要点\n\n"
                + "\n".join("- " + escape(p.strip()) for p in points if p.strip())
                + "\n\n"
            )
            if visuals:
                if "visual_review" in data:
                    text += "## 主要な図表\n\nAIがPDFの視覚入力から選んだ図表です。画像の切り出しは自動処理で、人間による原典照合は未実施です。\n\n"
                else:
                    text += "## 主要な図表\n\n原図の自動切り抜きです。見え方と内容の照合は未実施で、図表の添付だけで根拠を検証済みとは扱いません。\n\n"
                for visual in visuals:
                    image = f"{source_dir}/{visual_subdir}/{visual['file']}"
                    mode = (
                        "原図の切り抜き"
                        if visual["mode"] == "crop"
                        else "境界を確定できないため原ページを表示"
                    )
                    selection = visual.get("selection")
                    visual_title = escape(selection["title_ja"]) if selection else visual["label"]
                    text += f"### {visual_title} — PDF {visual['page']}ページ\n\n![[{image}|700]]\n\n{mode}。[[{source_dir}/source.pdf#page={visual['page']}|PDFで確認]]\n\n"
                    if selection:
                        text += escape(selection["explanation_ja"]) + "\n\n"
                        if selection["finding_indices"]:
                            text += (
                                "対応する所見: "
                                + "、".join(f"F{i}" for i in selection["finding_indices"])
                                + "\n\n"
                            )
                        if selection["uncertainty_ja"]:
                            text += "判読上の注意: " + escape(selection["uncertainty_ja"]) + "\n\n"
        else:
            text += f"## {heading}\n\n{escape(data[key])}\n\n"
    text += "## 主な結果\n\n| ID | 所見 | 根拠 | 適用範囲 | 状態 |\n|---|---|---|---|---|\n"
    for index, finding in enumerate(data["key_findings_ja"], 1):
        evidence = [e for e in data["evidence"] if e["claim"] == finding]
        locators = []
        for e in evidence:
            page = e["page"]
            if page is not None:
                present = any(b["page"] == page for b in blocks)
                if not present:
                    raise ValueError("保存済み解析の根拠ページが凍結PDFに存在しません")
                locators.append(f"PDF p. {page}; {escape(locator_reference(e['locator']))}")
        fallback = (
            "所見との対応は未照合（下記の根拠候補を参照）" if data["evidence"] else "根拠の記録なし"
        )
        text += f"| F{index} | {escape(finding)} | {' / '.join(locators) or fallback} | 論文の報告条件内・原典未照合 | 未照合 |\n"
    text += "\n## 解析が挙げた根拠候補\n\n"
    text += "所見との対応と原典の裏付けは未照合です。文章が一致しない候補も保持します。\n\n"
    for entry in data["evidence"]:
        page = entry["page"]
        if page is not None and not any(b["page"] == page for b in blocks):
            raise ValueError("保存済み解析の根拠ページが凍結PDFに存在しません")
        position = f"PDF p. {page}" if page is not None else "ページ未特定"
        text += f"- {escape(entry['claim'])} — {position}; {escape(locator_reference(entry['locator']))}（未照合）\n"
    if not data["evidence"]:
        text += "- 根拠候補の記録なし\n"
    text += "\n## 著者の解釈\n\n保存済み解析では報告結果と独立した欄がありません。追加の断定をせず、原典確認時に補完します。\n\n"
    text += (
        "## エージェントの推論・自分の研究との関連\n\n"
        + escape(data["research_relevance_ja"])
        + "\n\n"
    )
    for heading, key in [("限界・注意点", "limitations_ja"), ("未確認事項", "uncertainties_ja")]:
        text += f"## {heading}\n\n" + "\n".join("- " + escape(s) for s in data[key]) + "\n\n"
    text += "## 関連候補と統合提案\n\n" + proposal_link + "\n\n"
    text += "既存Wikiへの関連は提案として記録します。主題語の一致だけで知識関係や主張の支持を確定しません。\n\n"
    text += "## 処理記録\n\n"
    text += f"- 解析エンジン: {run['provider']}\n- モデル: {run.get('model') or '当時のCLI既定・モデル不明'}\n"
    text += f"- 保存済み解析ID: `{run['run_id']}`\n- PDF SHA-256: `{item['pdf_hash']}`\n"
    text += f"- 解析範囲: {data['coverage']}\n- 根拠のblock ID対応: 未照合。抽出物にあるだけのblockを主張へ割り当てません。\n"
    if "visual_review" in data:
        text += f"- AIの視覚読解: {len(data['visual_review']['pages_viewed'])}ページ。人間照合済みを意味しません。\n"
    text += "- 人間レビュー: 未実施\n- Canonical Wikiへの昇格: 未実施\n"
    # 位置候補や技術情報は保持したまま、通常の読書画面では折りたたむ。
    for heading in ("解析が挙げた根拠候補", "処理記録"):
        pattern = re.compile(rf"(## {heading}\n\n)(.*?)(?=\n## |\Z)", re.S)
        text = pattern.sub(
            lambda match: (
                match[1]
                + f"> [!info]- {heading}を表示\n"
                + "\n".join("> " + line if line else ">" for line in match[2].strip().splitlines())
                + "\n"
            ),
            text,
        )
    ptitle = title + " — Wiki統合提案"
    pf = base(f"paperflow-proposal:{slug}", "output", ptitle, source_id, date)
    pf.update(source_notes=[f"[[{note_path[:-3]}]]"], integration_status="proposal-pending")
    proposal = frontmatter(pf) + f"# {escape(ptitle)}\n\n"
    proposal += f"## 対象出典\n\n[[{note_path[:-3]}]]\n\n"
    proposal += "## レビューする内容\n\n- 原典と根拠ページを照合する。\n- 既存ページ・別名・条件を確認し、追加・反証・条件限定を区別する。\n- 不明な点を維持し、wiki-promoteで承認した変更だけを反映する。\n\n"
    proposal += "## 既存Wikiの関連候補\n\n"
    if candidates:
        for candidate in candidates:
            proposal += f"- [[{candidate['path'][:-3]}]] — 主題語の一致: {', '.join(candidate['topics'])}（意味関係は未確認）\n"
    else:
        proposal += "既存ページ・aliases・主題語を照合しましたが、機械的に選べる一致候補はありません。レビューで本文と別名を確認し、必要な粒度で新規作成を検討してください。\n"
    proposal += "\n## 主張候補\n\n" + "\n".join("- " + escape(f) for f in data["key_findings_ja"])
    proposal += "\n\n## 状態\n\n未承認。50_Wikiのページは追加・変更していません。\n"
    return text, proposal
