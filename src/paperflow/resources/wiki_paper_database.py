"""登録済み論文の参照用BasesとMarkdown一覧。原本とSource Noteは変更しない。"""

import argparse
import html
import json
import os
import re
from copy import deepcopy
from datetime import datetime
from pathlib import Path

import yaml

PAGE = "00_System/Index/論文データベース.md"
BEGIN = "<!-- BEGIN:paper-database -->"
END = "<!-- END:paper-database -->"
VERSION = "paper-database-v1"


def text(value):
    return "" if value is None else " ".join(str(value).split())


def values(value):
    return [text(v) for v in (value if isinstance(value, list) else [value]) if text(v)]


def frontmatter(path):
    parts = path.read_text(encoding="utf-8-sig").split("---", 2)
    data = yaml.safe_load(parts[1]) if len(parts) == 3 and not parts[0].strip() else {}
    return data if isinstance(data, dict) else {}


def collect(root, notes=None):
    manifests = {}
    source_root = root / "10_Papers/10_Sources"
    directories = sorted(source_root.iterdir()) if source_root.is_dir() else []
    for directory in directories:
        if directory.name.startswith("."):
            continue
        path = directory / "manifest.yml"
        if not path.is_file():
            continue
        if path.is_symlink() or path.parent.is_symlink():
            raise ValueError("リンク経由の凍結出典は索引化しません")
        record = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
        if isinstance(record, dict) and record.get("source_status") == "frozen":
            sid = record.get("source_id")
            if (
                isinstance(sid, str)
                and sid.startswith("paper:")
                and re.fullmatch(r"[a-f0-9]{64}", str(record.get("sha256", "")))
            ):
                if sid in manifests:
                    raise ValueError("凍結出典IDが重複しています: " + sid)
                manifests[sid] = record
    if notes is None:
        sources = []
        for current, dirs, names in os.walk(root / "10_Papers/20_Source_Notes", followlinks=False):
            dirs[:] = [
                n for n in dirs if not n.startswith(".") and not (Path(current) / n).is_symlink()
            ]
            for name in sorted(names):
                if not name.endswith(".md"):
                    continue
                path = Path(current) / name
                if path.is_symlink():
                    raise ValueError("リンク経由のSource Noteは索引化しません")
                sources.append((path.relative_to(root).as_posix(), frontmatter(path)))
    else:
        sources = [
            (n.relpath, n.frontmatter)
            for n in notes
            if n.relpath.startswith("10_Papers/20_Source_Notes/")
        ]
    rows = []
    for name, fm in sources:
        sid = fm.get("source_id") or fm.get("id")
        if fm.get("type") != "paper-note" or not isinstance(sid, str) or sid not in manifests:
            continue
        rows.append(
            dict(
                path=name,
                source_id=sid,
                title=text(fm.get("title")) or Path(name).stem,
                year=fm.get("year"),
                authors=values(fm.get("authors")),
                topics=values(fm.get("topics")),
                tags=values(fm.get("tags")),
                venue=text(fm.get("venue")),
                doi=text(fm.get("doi")),
                review=text(fm.get("review_status")),
                integration=text(fm.get("integration_status"))
                or ("integrated" if fm.get("status") == "integrated" else ""),
                model=text(fm.get("model")),
            )
        )
    return sorted(
        rows,
        key=lambda r: (
            -(int(r["year"]) if re.fullmatch(r"\d{4}", str(r["year"])) else 0),
            r["title"].casefold(),
            r["path"],
        ),
    )


def bases(rows):
    # 表示列をすべて数式にし、管理ノートのPropertiesを表から直接編集させない。
    formulas = {
        "paper": "file.asLink(if(note.title, note.title, file.basename))",
        "year": 'if(note.year, note.year, "未記載")',
        "authors": 'if(note.authors, list(note.authors).join("、"), "未記載")',
        "topics": "if(note.topics, note.topics, [])",
        "tags": "if(note.tags, note.tags, [])",
        "venue": 'if(note.venue, note.venue, "未記載")',
        "doi": 'if(note.doi, link(if(note.doi.startsWith("http"), note.doi, "https://doi.org/" + note.doi), note.doi), "未記載")',
        "review": 'if(note.review_status == "unreviewed", "未レビュー", if(note.review_status, note.review_status, "未記載"))',
        "integration": 'if(note.integration_status, if(note.integration_status == "integrated", "Wiki統合済み", if(note.integration_status == "proposal-pending", "統合提案あり", note.integration_status)), if(note.status == "integrated", "Wiki統合済み", "未記載"))',
        "model": 'if(note.model, note.model, "未記載")',
        "updated": "note.updated",
    }
    names = dict(
        paper="論文・要約",
        year="発行年",
        authors="著者",
        topics="主題",
        tags="Obsidianタグ",
        venue="掲載先",
        doi="DOI",
        review="レビュー",
        integration="Wiki統合",
        model="要約モデル",
        updated="ノート更新日",
    )
    order = [
        "formula." + p for p in ("paper", "year", "topics", "authors", "integration", "review")
    ]
    views = [
        dict(
            type="table",
            name="すべて",
            order=order,
            sort=[dict(property="formula.year", direction="DESC")],
        ),
        dict(
            type="table",
            name="発行年別",
            order=order,
            groupBy=dict(property="formula.year", direction="DESC"),
        ),
        dict(
            type="table",
            name="主題・タグ",
            order=["formula." + p for p in ("paper", "topics", "tags", "year", "venue", "doi")],
        ),
        dict(
            type="table",
            name="未レビュー",
            order=order,
            filters='note.review_status == "unreviewed" || note.review_status == "pending"',
        ),
        dict(
            type="table",
            name="Wiki統合状況",
            order=order,
            groupBy=dict(property="formula.integration", direction="ASC"),
        ),
    ]
    # 凍結manifestと対応したノートだけを含める。未登録PDF・処理途中を登録済みにしない。
    membership = {
        "or": ["file.path == " + json.dumps(r["path"], ensure_ascii=False) for r in rows]
        or ["false"]
    }
    return {
        "filters": {
            "and": [
                'file.ext == "md"',
                'file.inFolder("10_Papers/20_Source_Notes")',
                'note.type == "paper-note"',
                membership,
            ]
        },
        "formulas": formulas,
        "properties": {"formula." + p: dict(displayName=n) for p, n in names.items()},
        "views": views,
    }


def escape(value):
    return re.sub(r"([\\`*_{}\[\]()#+!|])", r"\\\1", html.escape(text(value), quote=False))


def render(rows, previous=None):
    config = bases(rows)
    if previous is not None:
        if not isinstance(previous, dict) or not isinstance(previous.get("views"), list):
            raise ValueError("Basesのビュー設定が不正です。既存の設定は上書きしません")
        # ビューごとの検索条件・列・並び順は保持し、登録対象の条件だけを更新する。
        saved = deepcopy(previous)
        saved["filters"] = config["filters"]
        config = saved
    config = yaml.safe_dump(config, allow_unicode=True, sort_keys=False, width=120)
    lines = [
        f"登録済みの論文ノート：**{len(rows)}件**。検索・フィルター・並べ替えは表のツールバーから操作できます。",
        "",
        "```base",
        config.rstrip(),
        "```",
        "",
        "> [!info]- Markdown一覧を表示",
        "> Basesを表示できない場合の一覧です。索引更新時点のメタデータを表示します。",
        ">",
        "> | 論文・要約 | 発行年 | 著者 | 主題 | レビュー | Wiki統合 |",
        "> |---|---:|---|---|---|---|",
    ]
    for row in rows:
        review = "未レビュー" if row["review"] == "unreviewed" else row["review"] or "未記載"
        integration = {"integrated": "Wiki統合済み", "proposal-pending": "統合提案あり"}.get(
            row["integration"], row["integration"] or "未記載"
        )
        # aliasの区切り文字はMarkdown表の列区切りにもなるため、一覧ではノート名を使う。
        # Bases側はPropertiesの原題を表示する。既存Wiki検証器が解決する通常のWikilinkを保つ。
        title = f"[[{row['path'][:-3]}]]"
        cells = [
            title,
            escape(row["year"]) or "未記載",
            escape("、".join(row["authors"])) or "未記載",
            escape("、".join(row["topics"])) or "未記載",
            escape(review),
            escape(integration),
        ]
        lines.append("> | " + " | ".join(cells) + " |")
    if not rows:
        lines.append("> | 登録済みの論文はありません | — | — | — | — | — |")
    return "\n".join(lines) + "\n"


def update_database(root, notes=None, *, check=False):
    root = Path(root)
    page = root / PAGE
    if not page.is_file():
        return ["論文データベースのページがありません。フレームワークを導入してください"]
    original = page.read_text(encoding="utf-8-sig")
    fm = frontmatter(page)
    if (
        fm.get("id") != "system:paper-database"
        or fm.get("owner") != "agent"
        or fm.get("agent_editable") is not True
    ):
        raise ValueError("論文データベースの管理権限を確認できません")
    if original.count(BEGIN) != 1 or original.count(END) != 1:
        raise ValueError("論文データベースの自動更新領域が不正です")
    before, rest = original.split(BEGIN)
    generated, after = rest.split(END)
    found = re.search(r"(?ms)^```base\n(.*?)\n```", generated)
    previous = yaml.safe_load(found[1]) if found else None
    output = before + BEGIN + "\n" + render(collect(root, notes), previous) + END + after
    if output == original:
        return []
    if check:
        return ["論文データベースが未更新です"]
    output = re.sub(
        r"(?m)^updated:.*$",
        "updated: " + datetime.now().astimezone().date().isoformat(),
        output,
        count=1,
    )
    page.write_text(output, encoding="utf-8", newline="\n")
    return []


def main():
    from _vault import VAULT

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    problems = update_database(VAULT, check=args.check)
    for problem in problems:
        print(problem)
    return int(bool(problems))


if __name__ == "__main__":
    raise SystemExit(main())
