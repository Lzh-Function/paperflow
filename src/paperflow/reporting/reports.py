import json
from pathlib import Path

from paperflow.ingestion.matcher import MATCH_REASONS
from paperflow.obsidian.renderer import escape, frontmatter


def report(store, min_priority=5, config=None):
    lines = [
        "# PaperFlow 読書候補",
        "",
        "読書優先度です。原典照合・Wikiへの正式統合は未実施です。",
        "",
    ]
    for paper in store.rows("SELECT * FROM papers WHERE source_present=1 ORDER BY title"):
        if not paper["source_hash"]:
            continue
        run = store.latest_analysis(paper["paper_id"], paper["source_hash"])
        if not run:
            continue
        result = json.loads(run["output_json"])
        if result["priority"] >= min_priority:
            link = f"PaperFlow/Analysis/{paper['paper_id']}"
            if config is not None and config.destination.layout == "llm_wiki":
                rows = store.rows(
                    "SELECT target_path FROM exports WHERE paper_id=? AND export_kind='WikiSource'",
                    (paper["paper_id"],),
                )
                if rows:
                    link = (
                        Path(rows[0]["target_path"])
                        .relative_to(config.destination.vault_dir)
                        .with_suffix("")
                        .as_posix()
                    )
                else:
                    lines.append(
                        f"- ★{result['priority']} {escape(paper['title'])} — 正式Wiki登録待ち"
                    )
                    continue
            lines.append(
                f"- ★{result['priority']} [[{link}|{escape(paper['title'])}]]"
                f" — {escape(result['priority_reason_ja'])}"
            )
    return managed_front("top-rated", "読書候補") + "\n".join(lines) + "\n"


def review_report(store):
    lines = ["# PaperFlow 要確認", ""]
    for paper in store.rows(
        "SELECT * FROM papers WHERE source_present=0 OR state IN "
        "('needs_review','retry_wait','failed','blocked','conflict') ORDER BY title"
    ):
        reason = "missing_from_source" if not paper["source_present"] else paper["state"]
        match = json.loads(paper["match_json"] or "{}")
        detail = MATCH_REASONS.get(match.get("reason"), "") if reason == "needs_review" else ""
        lines.append(
            f"- {escape(paper['title'])} (`{paper['paper_id']}`): {reason}"
            f" {escape(detail or paper['error_message'] or '')}"
        )
    return managed_front("review-needed", "要確認") + "\n".join(lines) + "\n"


def managed_front(slug, title):
    return frontmatter(
        dict(
            id=f"paperflow-report:{slug}",
            type="output",
            title="PaperFlow " + title,
            area="academic",
            status="review-pending",
            owner="agent",
            tags=["note/output", "area/academic"],
            topics=[],
            source_ids=[],
            source_notes=[],
            review_status="unreviewed",
            agent_editable=True,
            paperflow_managed=True,
        )
    )
