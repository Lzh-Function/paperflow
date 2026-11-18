"""通常取り込み用。変更と参照元を1つの読取りコンテキストで検証する。"""

import argparse
import functools
import importlib
import json
import os
import sys
import time
from pathlib import Path


def modules():
    scripts = Path(os.environ.get("LLM_WIKI_ROOT", Path(__file__).resolve().parents[1])) / "scripts"
    sys.path.insert(0, str(scripts))
    return [
        importlib.import_module(name)
        for name in (
            "_vault",
            "validate_frontmatter",
            "validate_obsidian_markup",
            "resolve_links",
            "check_graph_integrity",
        )
    ]


def selected_paths(root, names):
    result = set()
    for name in names:
        p = Path(name)
        p = p if p.is_absolute() else root / p
        try:
            relative = p.resolve().relative_to(root.resolve())
        except ValueError as exc:
            raise ValueError("Vault外の検証要求は受け付けません") from exc
        if any(part.casefold() == ".obsidian" for part in relative.parts):
            raise ValueError(".obsidianは検証対象に含めません")
        if p.suffix != ".md" or not p.is_file():
            raise ValueError(f"検証対象のMarkdownがありません: {name}")
        result.add(relative.as_posix())
    return result


def check(names, attachment_root=None):
    started = time.perf_counter()
    v, fm, markup, links, graph = modules()
    # relpathはループ内で何千回も使われる。Drive上のresolveを同一検証中に繰り返さない。
    v.rel = functools.cache(v.rel)
    wanted = selected_paths(v.VAULT, names)
    report = v.Report("check_changed")
    notes = v.load_notes(v.note_paths())
    known = {n.relpath for n in notes}
    for name in wanted - known:
        notes.append(v.parse_note(v.VAULT / name))
    for note in notes:
        for method in ("body_without_code", "headings", "block_ids", "links", "property_links"):
            setattr(note, method, functools.cache(getattr(note, method)))
    index = v.LinkIndex(notes)
    index.resolve = functools.cache(index.resolve)
    if attachment_root is not None:
        attachment_root = Path(attachment_root).resolve()
        if not (attachment_root / ".llm-wiki-root").is_file():
            raise ValueError("添付の参照元にVaultマーカーがありません")
        # C:の疎なプレビューから、既存添付の存在だけを元Vaultへ照会する。
        # 本文を複製せず、空ファイルや架空のリンク先も作らない。
        for note in notes:
            for ref in [*note.links(), *(r for _, r in note.property_links())]:
                name = ref.target
                p = Path(name)
                if p.is_absolute() or ".." in p.parts or "\\" in name or ":" in name:
                    continue
                if (
                    not p.suffix
                    or p.suffix == ".md"
                    or any(s.casefold() == ".obsidian" for s in p.parts)
                ):
                    continue
                candidate = (attachment_root / p).resolve()
                if candidate.is_relative_to(attachment_root) and candidate.is_file():
                    index.attachments.add(name.casefold())
    # LinkIndexが補助対象として読む制御文書・凍結抽出物にも同じキャッシュを使う。
    for note in {id(n): n for n in index.by_path.values()}.values():
        for method in ("body_without_code", "headings", "block_ids"):
            setattr(note, method, functools.cache(getattr(note, method)))
    selected = [n for n in notes if n.relpath in wanted]
    validators = fm.build_validators()
    if validators is None:
        report.error(
            "dependency", "schemas", "jsonschemaがありません。Schema検証を省略せず停止します"
        )
    tasks = v.load_yaml(v.VAULT / "00_System/Dictionaries/task-domains.yml") or {}
    ids = {}
    aliases = {}
    for note in notes:
        if note.id:
            ids.setdefault(note.id, set()).add(note.relpath)
        for alias in [note.name, *note.aliases]:
            aliases.setdefault(alias.casefold(), set()).add(note.relpath)
    for note in selected:
        if note.fm_error:
            report.error("frontmatter-parse", note.relpath, note.fm_error)
            continue
        if not note.frontmatter:
            report.error("frontmatter-missing", note.relpath, "frontmatterがありません")
            continue
        fm.check_schema(note, report, validators)
        fm.check_vocabulary(note, report)
        fm.check_area(note, report, True)
        fm.check_tags(note, report)
        fm.check_link_properties(note, report)
        fm.check_unquoted_property_links(note, report)
        fm.check_task(note, report, tasks)
        for issue in v.filename_problems(note.path.name):
            report.error("filename", note.relpath, issue)
        if note.id and len(ids[note.id]) > 1:
            report.error("duplicate-id", note.relpath, "ID重複: " + ", ".join(sorted(ids[note.id])))
        for alias in [note.name, *note.aliases]:
            if len(aliases[alias.casefold()]) > 1:
                report.error("alias-collision", note.relpath, "名前/別名の重複: " + alias)
        if note.frontmatter.get("kanban-plugin") != "board":
            markup.check_headings(note, report)
        for fn in (
            markup.check_block_ids,
            markup.check_callouts,
            markup.check_markdown_links,
            markup.check_evidence_tables,
            markup.check_general_note,
            markup.check_locators,
        ):
            fn(note, report)
    # 引用先の見出し・block・型の変更が、既存の参照元を壊していないかも一度だけ確認。
    inbound = set()
    for note in notes:
        for ref in [*note.links(), *(ref for _, ref in note.property_links())]:
            targets, _ = index.resolve(ref.target)
            if any(t.relpath in wanted for t in targets):
                inbound.add(note.relpath)
                break
    affected = wanted | inbound
    for note in notes:
        if note.relpath not in affected or note.fm_error:
            continue
        links.check_note(note, index, report, False)
        graph.check_relations(note, index, report)
        if note.relpath in wanted:
            graph.check_canonical(note, index, report)
            graph.check_source_note(note, index, report)
    graph.check_orphans(notes, index, report, wanted)
    result = report.to_dict()
    result["scope"] = dict(
        changed=len(wanted),
        inbound=len(inbound - wanted),
        context_notes=len(notes),
        context_builds=1,
        full_audit=False,
        semantic_review=False,
    )
    result["elapsed_seconds"] = round(time.perf_counter() - started, 4)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", action="append", default=[])
    parser.add_argument("--paths-file", type=Path)
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--attachment-root",
        type=Path,
        help="C:のプレビューで既存添付の存在を照会する元Vault（読取り専用）",
    )
    parser.add_argument("--brief", action="store_true", help="結果の件数と必要な指摘だけを表示")
    args = parser.parse_args()
    names = list(args.path)
    if args.paths_file:
        data = json.loads(args.paths_file.read_text(encoding="utf-8"))
        if not isinstance(data, list) or not all(isinstance(n, str) for n in data):
            parser.error("paths-fileは相対パスのJSON配列にしてください")
        names += data
    if not names:
        parser.error(
            "今回処理したファイルを--pathまたは--paths-fileで指定してください。全体監査はwiki_lint.pyです"
        )
    try:
        result = check(names, args.attachment_root)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        counts = result["counts"]
        print(
            f"変更{result['scope']['changed']}件＋参照元{result['scope']['inbound']}件: "
            f"エラー{counts['error']}、警告{counts['warning']}（{result['elapsed_seconds']:.2f}秒）"
        )
        findings = [f for f in result["findings"] if f["severity"] != "info"]
        for f in findings[: 10 if args.brief else None]:
            print(f"{f['path']}: {f['check']} — {f['message']}")
        if args.brief and len(findings) > 10:
            print(f"ほか{len(findings) - 10}件。--jsonで全指摘を取得できます。")
    return 1 if result["counts"]["error"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
