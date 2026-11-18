"""初回導入用の最小フレームワーク移行。呼出先はC:のpreviewだけ。"""

import json
import shutil
from pathlib import Path

import yaml

from paperflow.config import SettingsError

POLICY = """# PaperFlowによる論文取り込み

論文の通常入口はPaperFlowとする。PaperpileのG:原本は読み取り専用で、Vaultへバイト一致の凍結コピーを登録する。手動で10_Papers/00_Inboxへ置く手順は互換用のDirect Intakeとして残す。

## 正規フロー

1. PaperFlowでBibTeXとPDFを照合し、選択したProviderで日本語解析する。
2. C:で凍結登録・Source Note・統合提案・索引とログの差分を準備する。
3. 同じPDFハッシュは既存出典を再利用する。同じDOIでPDFが異なる場合は版の確認で停止する。
4. 共通Writerロックを排他的に取得し、準備後の変更をハッシュで検査して反映する。
5. 正式Source Noteは10_Papers/20_Source_Notes、提案は70_Review/Merge_Proposalsへ置く。
6. 50_Wikiの新規作成・更新は自動実行せず、wiki-promote等で提案をレビューしてから行う。

## 登録と根拠

凍結PDFの登録は原典の意味的検証を意味しない。AI解析のevidence_statusはunverified、review_statusはunreviewedを保持する。根拠のページがあることと、主張がそのページで裏付けられることを区別する。

manifest.ymlはsource_id・SHA-256・来歴を保持する。extracted.md / extracted.jsonはページとpNNN-bNNNのblock IDを保持する。主張とblockが未照合なら対応を捏造しない。

Source Noteにはintegration_status: proposal-pendingと、実在するintegration_proposalへのWikilinkを持たせる。提案は対象source_idsとSource Noteへのリンクを持つ。この組は「未統合の確認待ち」として認識する。統合済み・verifiedとは記録しない。

## 許可と保護

G:原本・既存の学術ノート・人間ノート・.obsidianは変更しない。PaperFlowが作成したSource Note・提案は前回生成ハッシュが一致する場合だけ更新する。既存の別のSource Noteは再利用し本文を保持する。

PaperFlow経由ではPaper Inboxの投入・削除を行わない。G:原本をfinalize_paper_ingest.pyへ渡さない。手動Inbox経由だけは従来のfinalization契約を維持する。

初回本番反映はレビュー済み差分の承認後。実PDFの追加外部送信・定期実行登録は独立の承認が必要。

## 運用コマンド

WindowsネイティブPythonでPaperFlowのプロジェクトから実行する。

```powershell
paperflow wiki-prepare --cached-only --paper-id ID --output C:\\path\\bundle
paperflow wiki-apply --bundle C:\\path\\bundle
```

初回フレームワーク導入時だけwiki-prepareへ--upgrade-frameworkを付ける。通常のsyncはlayout: llm_wiki / copy_pdf: trueの設定で同じフローを使う。dry-runではVault・DB・ディスク・外部LLMを変更しない。

## 検証と復旧

正式反映前にfrontmatter・Markdown・リンク・グラフの変更ファイル検証を行う。索引とMOCを再生成し、ログを追記する。既存の無関係な警告を隠すためにノートを変更しない。

C:のbundleはpreview、plan.json、元ファイルのoriginals、反映journalを持つ。反映途中の障害は自分の書き込みだけをハッシュ確認して復旧する。クラッシュ後は同じwiki-applyで未完了journalを復旧してから再試行できる。他のWriterの編集は上書きしない。
"""

SKILL = """---
name: wiki-ingest-paper
description: PaperFlowを正規入口として論文PDFを凍結登録し、日本語Source NoteとWiki統合提案を作る。手動Inboxは互換経路。
---

# 論文取り込み

AGENTS.mdと00_System/PaperFlow-Integration.md、引用・リンク・所有権の各ポリシーを読む。単一Writerで実行する。

## 通常入口

Paperpile登録済み論文はPaperFlowで処理する。G:原本を読み取り専用で凍結コピーし、manifest、ページ・block付き抽出物、正式Source Note、統合提案、索引・ログを一連の差分として作る。Providerとモデルの選択はPaperFlow側で行う。

- 新規論文: paperflow sync（layout: llm_wiki、copy_pdf: true）。
- 保存済み解析の再利用: paperflow wiki-prepare --cached-only --paper-id ID --output C:\\path\\bundle。
- 差分確認後の反映: paperflow wiki-apply --bundle C:\\path\\bundle。

## 制約と完了条件

原本は削除・移動・更新しない。初回本番反映・新たな外部送信・定期登録の承認を独立に守る。根拠はページを保持し、未照合のblock対応を捏造しない。人間のノートを変更しない。既存出典のDOI・ハッシュ・版を照合し、同一版を重複作成しない。

出典は凍結登録済みでも知識はレビュー待ち。integration_status: proposal-pendingと実在する提案を対にし、wiki-promoteへ引き渡す。50_Wikiは自動更新しない。変更ファイル検証・索引・ログと反映journalまで成功して完了とする。

## 手動Inboxの互換経路

Paperpile外のPDFが10_Papers/00_Inboxへ明示投入された場合だけ、register_source.py → extract_pdf.py → Source Note → 検証 → finalize_paper_ingest.pyを使う。G:のPaperpile原本にfinalizationを適用してはならない。
"""


def replace_once(text, old, new, name):
    if old not in text:
        raise SettingsError(f"フレームワークの版が想定と違います。移行を再確認してください: {name}")
    return text.replace(old, new, 1)


def upgrade_framework(root: Path):
    from paperflow.wiki.framework_refresh import refresh

    policy = root / "00_System/PaperFlow-Integration.md"
    if policy.exists():
        refresh(root)
        return

    def edit(name, change):
        path = root / name
        path.write_text(
            change(path.read_text(encoding="utf-8-sig")), encoding="utf-8", newline="\n"
        )

    def markup(text):
        text = replace_once(
            text, "import re\n", "import re\nimport unicodedata\n", "markup imports"
        )
        text = replace_once(
            text,
            'return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()',
            'return re.sub(r"[_\\W]+", " ", unicodedata.normalize("NFKC", text).casefold()).strip()',
            "日本語見出し",
        )
        text = text.replace(
            '"mailto:", "#", "obsidian://"', '"mailto:", "#", "obsidian://", "file://"'
        )
        text = text.replace('"locator unavailable",', '"locator unavailable", "ページ", "未特定",')
        text = replace_once(
            text,
            'header = table_header(note.body, "Main findings")',
            'header = table_header(note.body, "Main findings") or table_header(note.body, "主な結果")',
            "所見表",
        )
        text = text.replace(
            "header[: len(PAPER_FINDING_COLUMNS)] != PAPER_FINDING_COLUMNS:",
            'header[: len(PAPER_FINDING_COLUMNS)] not in (PAPER_FINDING_COLUMNS, ["id", "所見", "根拠", "適用範囲", "状態"]):',
        )
        return text

    edit("scripts/validate_obsidian_markup.py", markup)

    def graph(text):
        old = "    linked_canonical = False\n"
        new = """    if note.frontmatter.get("integration_status") == "proposal-pending":
        link = str(note.frontmatter.get("integration_proposal") or "")
        if link.startswith("[[") and link.endswith("]]"):
            proposals, _ = index.resolve(link[2:-2])
            if len(proposals) == 1 and proposals[0].type == "output" and proposals[0].frontmatter.get("status") == "review-pending" and note.id in (proposals[0].frontmatter.get("source_ids") or []):
                report.info("source-note-proposal-pending", note.relpath, "出典登録済み。Wikiへの統合はリンク先の提案でレビュー待ち")
                return
    linked_canonical = False
"""
        return replace_once(text, old, new, "統合提案の状態")

    edit("scripts/check_graph_integrity.py", graph)

    def locks(text):
        start = text.index("    if lock_path.exists():", text.index("def acquire_lock("))
        end = text.index("    return lock_path", start) + len("    return lock_path")
        result = (
            text[:start]
            + """    lock_path.parent.mkdir(parents=True, exist_ok=True)
    global _writer_lock_token
    token = os.urandom(16).hex()
    try:
        with lock_path.open("x", encoding="utf-8") as handle:
            handle.write(dump_yaml({"lock_type": "agent-write", "host": hostname(),
                "agent": agent, "started_at": now_iso(), "operation": operation, "source": source, "token": token}))
    except FileExistsError:
        return None
    _writer_lock_token = token
    return lock_path"""
            + text[end:]
        )
        first = result.index("def release_lock()")
        last = result.index("def copy_file(", first)
        return (
            result[:first]
            + """def release_lock() -> None:
    lock_path = VAULT / "00_System" / "Locks" / "agent-write-lock.yml"
    token = globals().get("_writer_lock_token")
    if lock_path.exists() and token and load_yaml(lock_path).get("token") == token:
        lock_path.unlink()


"""
            + result[last:]
        )

    edit("scripts/_vault.py", locks)
    policy.write_text(POLICY, encoding="utf-8")
    edit(
        "AGENTS.md",
        lambda text: (
            text
            + "\n## PaperFlowによる正規論文取り込み\n\n"
            + "論文の通常入口はPaperFlowとする。詳細は `00_System/PaperFlow-Integration.md`。凍結登録・正式Source Note・統合提案まで自動化し、50_Wikiの変更はwiki-promoteでのレビュー後に限る。PaperFlowの原本はInboxへ移動せず削除しない。手動Inbox経路は互換用として残す。\n"
        ),
    )
    skill = root / "agent-skills/wiki-ingest-paper/SKILL.md"
    skill.write_text(SKILL, encoding="utf-8")
    edit(
        "agent-skills/wiki-promote/SKILL.md",
        lambda text: (
            text
            + "\n## PaperFlowの統合提案\n\n70_Review/Merge_ProposalsのPaperFlow提案から対象Source Noteと凍結出典を確認する。proposal-pendingはレビュー待ちであり、出典登録を根拠のverifiedやWiki統合済みと混同しない。既存ページ・aliases・条件を照合し、承認した変更だけを反映して提案とSource Noteの統合状態を更新する。\n"
        ),
    )
    marker = yaml.safe_load((root / ".llm-wiki-root").read_text(encoding="utf-8"))
    marker.update(
        schema_version=max(6, int(marker.get("schema_version", 0))),
        framework_version="1.8",
        paperflow_integration=1,
    )
    (root / ".llm-wiki-root").write_text(
        yaml.safe_dump(marker, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    path = root / "schemas/paper-note.schema.json"
    schema = json.loads(path.read_text(encoding="utf-8"))
    schema["properties"].update(
        integration_status={"enum": ["proposal-pending", "integrated", "rejected"]},
        integration_proposal={"type": "string"},
        paperflow_id={"type": "string"},
    )
    path.write_text(json.dumps(schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # 今回変更したスキルだけ同期し、既存の無関係な配布差分は直さない。
    for name in ("wiki-ingest-paper", "wiki-promote"):
        for prefix in (".claude/skills", ".agents/skills"):
            dest = root / prefix / name / "SKILL.md"
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(root / "agent-skills" / name / "SKILL.md", dest)
    refresh(root)
