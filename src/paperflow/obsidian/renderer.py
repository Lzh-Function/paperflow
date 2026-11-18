import html
import re
from pathlib import Path
from urllib.parse import quote

import yaml

from paperflow.subject_tags import subject_tags


def escape(value) -> str:
    text = html.escape(str(value), quote=False).replace("\n", " ").replace("\r", " ")
    return re.sub(r"([\\`*_{}\[\]()#+!|])", r"\\\1", text)


def frontmatter(data: dict) -> str:
    return "---\n" + yaml.safe_dump(data, allow_unicode=True, sort_keys=False) + "---\n\n"


def common(paper, kind, title, managed=False):
    date = paper["created_at"][:10]
    return dict(
        id=f"paperflow-{kind}:{paper['paper_id']}",
        type="output" if managed else "personal-note",
        title=title,
        area="academic",
        status="review-pending",
        owner="agent" if managed else "human",
        created=date,
        updated=date,
        aliases=[],
        tags=["note/output" if managed else "note/personal", "area/academic", "source/paper"],
        topics=[],
        projects=[],
        source_ids=[],
        related_concepts=[],
        related_claims=[],
        source_notes=[],
        review_status="unreviewed",
        agent_editable=managed,
        external_source_id=f"paperpile:{paper['paper_id']}",
        paper_id=paper["paper_id"],
        source="paperpile",
        paperflow_managed=managed,
        registration_status="external-unregistered",
    )


def source_links(metadata, pdf: str | None):
    links = []
    if metadata.get("doi"):
        links.append("- 出典: [DOI](https://doi.org/" + quote(metadata["doi"], safe="/") + ")")
    if pdf:
        links.append("- 原本PDF（このPCのみ）: [PDF](" + Path(pdf).as_uri() + ")")
    return "\n".join(links)


def render_paper(paper, metadata, namespace="PaperFlow", pdf=None):
    data = common(paper, "note", metadata["title"])
    data.update(
        title=metadata["title"],
        authors=metadata["authors"],
        year=metadata["year"],
        doi=metadata["doi"],
        citekey=metadata["citekey"],
        reading_status="unread",
    )
    return (
        frontmatter(data) + f"# {escape(metadata['title'])}\n\n"
        f"- AI解析: [[{namespace}/Analysis/{paper['paper_id']}|解析結果]]\n"
        + source_links(metadata, pdf)
        + "\n\n## 自分のメモ\n\n<!-- 人間管理。PaperFlowはこのノートを上書きしません。 -->\n"
    )


def render_analysis(paper, metadata, run, source_hash, pdf=None, extraction=None):
    result = run["data"] if run else None
    data = common(paper, "analysis", metadata["title"] + " — 解析候補", managed=True)
    if result:
        data["tags"] += subject_tags(result["tags"])
    data.update(
        evidence_status="unverified",
        source_hash=source_hash,
        prompt_version="v1",
        bibliography_hash=paper["bib_hash"],
        coverage=result["coverage"] if result else "metadata_only",
        provider=run["provider"] if run else "none",
        model=run["model"] if run else None,
        analysis_run_id=run["run_id"] if run else None,
        topics=result["tags"] if result else [],
        priority=result["priority"] if result else None,
        text_truncated=extraction.text_truncated if extraction else False,
        extraction_quality=extraction.quality if extraction else "not_extracted",
    )
    if run:
        data["updated"] = run["started_at"][:10]
    text = frontmatter(data) + f"# {escape(metadata['title'])}\n\n"
    text += "> [!warning] 出典登録・根拠の確認は未実施\n> 未登録の外部資料に基づく生成候補です。Wikiへの正式統合・根拠照合は未実施です。\n\n"
    text += source_links(metadata, pdf) + "\n\n"
    if not result:
        return text + "## 要約\n\n書誌情報のみ。本文・抄録の解析は未実施です。\n"
    sections = [
        ("要約", "tldr_ja"),
        ("研究課題", "research_question_ja"),
        ("手法・実験設定", "approach_ja"),
        ("主な結果", "key_findings_ja"),
        ("限界・注意点", "limitations_ja"),
        ("自分の研究・関心との関連", "research_relevance_ja"),
        ("不確実な点・未確認事項", "uncertainties_ja"),
    ]
    for heading, field in sections:
        value = result[field]
        rendered = (
            "\n".join("- " + escape(v) for v in value) if isinstance(value, list) else escape(value)
        )
        text += f"## {heading}\n\n{rendered or '未確認'}\n\n"
    text += "## 根拠・参照ページ\n\n"
    for item in result["evidence"]:
        locator = (
            f"PDF {item['page']}ページ" if item["page"] is not None else "根拠の位置を特定できず"
        )
        text += (
            f"- {escape(item['claim'])} — {locator}; {escape(item['locator'] or '')}（未照合）\n"
        )
    if not result["evidence"]:
        text += "- 根拠の位置を特定できず（根拠未照合）\n"
    text += f"\n## 読書優先度\n\n★{result['priority']} — {escape(result['priority_reason_ja'])}\n"
    text += "\n## 主題語の追加候補\n\n" + "\n".join(
        "- " + escape(t) for t in result["tag_suggestions"]
    )
    text += (
        "\n\n## 処理記録\n\n"
        f"- 解析エンジン: {provider_label(run['provider'])}\n- 解析元ハッシュ: `{source_hash}`\n"
        f"- 解析範囲: {coverage_label(result['coverage'])}\n- 人間レビュー: 未実施\n"
    )
    return text


def coverage_label(coverage: str) -> str:
    return {"fulltext": "本文全体", "partial": "本文の一部", "abstract": "抄録のみ"}.get(
        coverage, "書誌情報のみ"
    )


def provider_label(provider: str) -> str:
    return {
        "claude": "Claude Code CLI",
        "codex": "Codex CLI",
        "fake": "動作確認用（FakeProvider）",
    }.get(provider, escape(provider))
