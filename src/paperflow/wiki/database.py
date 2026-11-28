"""論文データベースのフレームワーク差分をC:に準備する。"""

import os
import re
import shutil
from datetime import datetime
from importlib.resources import files

import yaml

from paperflow.config import SettingsError
from paperflow.paths import resolve_path
from paperflow.resources.wiki_paper_database import BEGIN, END, PAGE, update_database
from paperflow.wiki.bundle import finalize_bundle, inventory, sha, target
from paperflow.wiki.export import run_script, validate_changed

HOOK_ANCHOR = (
    '    problems += apply(INDEX_DIR / "source-index.md", build_source_index(notes), args.check)'
)
HOOK = "\n    # PaperFlow: paper database index\n    from paper_database import update_database\n    problems += update_database(VAULT, notes, check=args.check)"
LINK = "\n論文の発行年・著者・主題を表で見るには [[00_System/Index/論文データベース|論文データベース]] を開いてください。\n"
GUIDE = """
## 論文データベースの自動更新

登録済み論文の一覧は `00_System/Index/論文データベース.md`。Source Noteの既存Propertiesを参照し、発行年・著者・主題・タグ・レビュー・Wiki統合状態を表示する。Markdown本文や主題をAIで再生成しない。

通常の `python scripts/update_index.py` が登録済みSource Noteと凍結manifestを照合し、データベースの自動更新領域も一緒に更新する。追加のLLM呼び出し・PDFの再読解・定期実行は不要。索引更新後の変更ファイル検証は従来どおり1回だけ行う。未登録・処理途中のPDFを登録済み一覧へ含めず、レビュー・Wiki統合済みとも推定しない。

表の列は参照用の数式で、管理Source NoteのPropertiesを直接編集する入口にしない。生成領域外の説明は保持し、既存の学術本文・人間ノート・原本・.obsidianは変更しない。
"""


def page_template():
    date = datetime.now().astimezone().date().isoformat()
    fm = dict(
        id="system:paper-database",
        type="output",
        title="論文データベース",
        status="active",
        owner="agent",
        created=date,
        updated=date,
        aliases=[],
        tags=["note/output", "source/paper"],
        topics=["navigation"],
        projects=[],
        source_ids=[],
        related_concepts=[],
        related_claims=[],
        source_notes=[],
        review_status="not-required",
        agent_editable=True,
        graph_exempt_reason="凍結登録済みSource Noteを集約する参照用データベース。",
    )
    return (
        "---\n"
        + yaml.safe_dump(fm, allow_unicode=True, sort_keys=False)
        + "---\n\n# 論文データベース\n\n表の左上でビューを切り替え、検索・フィルター・並べ替えで論文を探します。論文名を押すと要約ノートが開きます。\n\n主題・タグは分類、レビューとWiki統合は別の状態です。原典の正しさを確認済みとみなすものではありません。\n\n"
        + BEGIN
        + "\n"
        + END
        + "\n\n## 更新と表示\n\nPaperFlowの論文保存・要約版の採用と、従来の索引更新で自動更新します。ノートのタグや年が変わるとBasesの表示も追従します。この表の列は参照用です。新しい論文はPaperFlowから登録します。\n\n表が表示されない場合は、Obsidianの「設定 → コアプラグイン → Bases（ベース）」を有効にし、閲覧表示へ切り替えてください。追加プラグインは不要です。下の折りたたみMarkdown一覧も利用できます。\n\n索引を手動で再生成する場合は取り込みを停止し、`python scripts/update_index.py` を実行します。`--check` は更新の必要性だけを調べ、ファイルを変更しません。\n\n出典別の索引は [[00_System/Index/source-index|Source Index]] を参照してください。\n"
    )


def copy_database_dependencies(root, stage):
    # 疎なC:プレビューにも、既存のナビゲーションが指す凍結済み制御索引だけを用意する。
    # 廃止経路の原本や会話履歴を展開せず、既存リンクと本文を変更しない。
    guards = {}
    for zone in ("35_Remote_Captures", "35_Agent_Sessions"):
        name = zone + "/90_Archive/legacy-remote-capture/index/remote-capture-index.md"
        live = target(root, name)
        if not live.is_file():
            continue
        destination = target(stage, name)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(live, destination)
        guards[name] = sha(destination)
    return guards


def install(stage):
    def write(path, text):
        data = text.encode("utf-8")
        if not path.exists() or path.read_bytes() != data:
            path.write_bytes(data)

    script = stage / "scripts/update_index.py"
    original = script.read_text(encoding="utf-8-sig")
    if "# PaperFlow: paper database index" not in original:
        if original.count(HOOK_ANCHOR) != 1:
            raise SettingsError(
                "update_index.pyの形式が想定と違います。自動更新の接続箇所を確認してください"
            )
        write(script, original.replace(HOOK_ANCHOR, HOOK_ANCHOR + HOOK, 1))
    write(
        stage / "scripts/paper_database.py",
        files("paperflow.resources").joinpath("wiki_paper_database.py").read_text(encoding="utf-8"),
    )
    page = stage / PAGE
    if not page.exists():
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(page_template(), encoding="utf-8", newline="\n")
    update_database(stage)
    for name in ["00_System/Index/index.md", "00_System/Index/source-index.md"]:
        path = stage / name
        text = path.read_text(encoding="utf-8-sig")
        if "[[00_System/Index/論文データベース" not in text:
            anchor = text.find("\n## ")
            text = text[:anchor] + LINK + text[anchor:] if anchor >= 0 else text + LINK
            text = re.sub(
                r"(?m)^updated:.*$",
                "updated: " + datetime.now().astimezone().date().isoformat(),
                text,
                count=1,
            )
            path.write_text(text, encoding="utf-8", newline="\n")
    for name in ["00_System/PaperFlow-Integration.md", "agent-skills/wiki-ingest-paper/SKILL.md"]:
        path = stage / name
        text = path.read_text(encoding="utf-8-sig")
        if "## 論文データベースの自動更新" not in text:
            path.write_text(text.rstrip() + "\n" + GUIDE, encoding="utf-8", newline="\n")
        if name.startswith("agent-skills/"):
            for prefix in [".claude/skills", ".agents/skills"]:
                dest = stage / prefix / "wiki-ingest-paper/SKILL.md"
                dest.parent.mkdir(parents=True, exist_ok=True)
                write(dest, path.read_text(encoding="utf-8"))
    marker = stage / ".llm-wiki-root"
    data = yaml.safe_load(marker.read_text(encoding="utf-8"))
    current = tuple(int(part) for part in str(data.get("framework_version", "0")).split("."))
    if current < (1, 12):
        data["framework_version"] = "1.12"
    data["paper_database"] = 1
    write(marker, yaml.safe_dump(data, allow_unicode=True, sort_keys=False))


def prepare_database(cfg, bundle):
    root = resolve_path(cfg.destination.vault_dir)
    bundle = resolve_path(bundle)
    if (
        bundle.exists()
        or bundle.is_relative_to(root)
        or bundle.is_relative_to(resolve_path(cfg.source.paperpile_dir))
    ):
        raise SettingsError("差分は原本・Vaultの外の新規C:フォルダに作成してください")
    if os.name == "nt" and bundle.drive.upper() != "C:":
        raise SettingsError("差分はC:内へ作成してください")
    if not target(root, ".llm-wiki-root").is_file():
        raise SettingsError("Vaultマーカーがありません")
    before = inventory(root)
    stage = bundle / "preview"
    stage.mkdir(parents=True)
    for name in before:
        dest = target(stage, name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(target(root, name), dest)
    guards = copy_database_dependencies(root, stage)
    install(stage)
    run_script(
        stage,
        "append_log.py",
        [
            "--operation",
            "schema",
            "--subject",
            "paper-database-v1",
            "--detail",
            "論文データベースを追加。既存Source Noteのメタデータを参照し、索引更新で自動更新。論文本文・凍結原本は変更していない。",
        ],
    )
    changed = [PAGE, "00_System/Index/index.md", "00_System/Index/source-index.md"]
    reports = validate_changed(stage, changed, attachment_root=root)
    return finalize_bundle(
        bundle,
        root,
        before,
        dict(
            registered=[],
            new_source_dirs=[],
            derived_assets=[],
            frozen_guards=guards,
            validation=reports,
            framework_upgrade=False,
            paper_database=True,
        ),
    )
