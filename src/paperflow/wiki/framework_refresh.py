"""軽量検証と図表付きノートのフレームワーク更新。stageにのみ書く。"""

import re
import shutil
from importlib.resources import files

import yaml

SKILLS = (
    "wiki-ingest-paper",
    "wiki-ingest-web",
    "wiki-triage-inbox",
    "wiki-research",
    "wiki-integrate-user-note",
    "wiki-promote",
    "wiki-lint",
    "wiki-synthesize",
    "wiki-normalize-note",
)

PROCESSING_POLICY = """# 文献処理と検証の実行方針

## 通常の取り込み

- 文献の要約生成は選んだProviderで1回行い、入力ハッシュと保存済み結果を再利用する。新規の全文解析は視覚読解を必須とする。ClaudeはPDFそのものを添付し、Codexは全ページを画像として添付する。
- 初回取り込みは未レビューのSource Noteと統合提案まで。原典の全主張の逐一再照合、他論文の追跡、Canonical Wikiの更新を同じ工程へ追加しない。
- 変更したMarkdownと、その変更で影響する既存の参照元をcheck_changed.pyで1回検証する。処理した実パスを指定し、時間窓だけで大量のノートを巻き込まない。
- check_changed.pyが成功した後に、同じ4検証器やwiki_lint.pyを重ねて実行しない。
- 検証後に文章・見出し・リンクを変更した場合だけ、その変更の検証をやり直す。同じバイトをコピーした反映の後は、変更対象のハッシュと書き込み結果を確認する。
- PDFハッシュは登録・コピー時に確認し、通常の取り込みごとに過去の全PDFを再ハッシュしない。
- ロック、原本保護、書き込み前ハッシュ、重複ID・名前、frontmatter、引用先・添付・関係の整合は省略しない。

```powershell
python scripts/check_changed.py --path "10_Papers/20_Source_Notes/今回の論文.md" --path "70_Review/Merge_Proposals/今回の提案.md" --brief
```

## 全体監査と意味的レビュー

wiki_lint.pyはライブラリ全体の整備、明示的な全体監査、フレームワーク更新時に使う。毎回の文献登録の必須工程にはしない。必要時は--no-writeで読取り監査できる。定期実行の登録は利用者承認後。

原典の意味的な照合は、Wikiへ昇格させる主張と必要なページに絞る。未確認は未確認として残す。1件の取り込みの完了条件を、全引用文献の精読や校正理論の再検証まで広げない。新たな独立調査が必要なら別の提案にする。

## 図表と読みやすさ

- 新規Source Noteは要点を先頭に置き、原図の図表を標準3件まで添える。要約担当AIが本文と全ページの視覚入力から重要な図表を選び、日本語の表示名・説明・対応する所見を返す。
- PDFに図表captionと位置があれば、その近くの原図や表を切り出す。生成AIで論文の図を描き直さない。
- 境界を十分に確定できなければ、ページ抜粋と明記して原ページを表示する。図表の部分欠落を隠さない。
- 自動選択・自動切り出しは未照合。PDFページと元ハッシュ、切り出し位置、画像ハッシュをvisuals.jsonへ保存する。添付を根拠verifiedへの昇格条件にしない。
- 長い根拠候補と技術的な処理記録は折りたたむ。内容を削らず、通常の読書で必要な要点・図表・結果を先に見せる。
- 既存のレビュー済みノートや人間の編集は自動で再生成しない。既存ノートへの図表追加は必要な対象を選んで行う。

## 視覚入力の完了条件

全ページを添付したうえで、AIのpages_viewedと選択図表のページ・関連所見を共通Schemaで検査する。AIの読解完了の申告は人間の原典照合や学術的正しさの保証とは区別する。未完了、画像非対応、入力上限超過なら停止し、文字だけに自動で切り替えない。

上限値は使用する設定ファイルを正とする。2026-11-17のPaperpile実測に基づく初期値は300ページ、PDF64MiB、Codex向け全ページPNG合計128MiB、補助テキスト300万文字。これは運用上の上限であり、モデルの制限・料金・トークン上限を保証しない。ClaudeのPDFは1M未満のコンテキストでは100ページ上限という条件があるため、大きなPDFには対応モデルを選ぶ。実際のCLI経由の成功は別途確認する。視覚入力はテキストのみの解析よりトークンを消費する。

過去の文字のみの解析を視覚読解済みと記録しない。既存ノートやキャッシュを一括で再解析しない。再解析は対象と送信範囲を確認してから行う。実論文の新たな外部送信の承認は引き続き必要。

## 視覚入力の一時ファイル

全ページPNGや入力PDFの作業コピーはC:の一時ディレクトリへ置き、CLIの正常終了・例外・タイムアウト時に削除する。計測だけならページごとメモリ上で画像化して破棄し、大量のPNGを保存しない。Source Noteへ添える選択済み図表は成果物として保持する。OSの強制終了・停電では通常の終了処理が走らないことを区別する。

## エージェントの出力量

同じセッションで読んだ変更のない規約は毎工程で読み直さない。作業対象のSource Note・索引と必要な原典ページを読み、無関係な全文を会話へ取り込まない。規約が更新された場合は該当箇所を確認する。

成功時は件数・警告の有無・保存先だけを報告する。検証JSONや本文全体を会話へ反復して貼らない。意味的な疑問がない通常登録は機械的検査をモデルに再推論させない。
"""


def refresh(root):
    scripts = root / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    for source, dest in [
        ("wiki_check_changed.py", "check_changed.py"),
        ("wiki_extract_visuals.py", "extract_paper_visuals.py"),
    ]:
        (scripts / dest).write_text(
            files("paperflow.resources").joinpath(source).read_text(encoding="utf-8"),
            encoding="utf-8",
        )
    policy = root / "00_System/Processing-Policy.md"
    policy.write_text(PROCESSING_POLICY, encoding="utf-8")

    def append(name, marker, body):
        p = root / name
        s = p.read_text(encoding="utf-8-sig")
        if marker not in s:
            s += "\n" + marker + "\n\n" + body + "\n"
        p.write_text(s, encoding="utf-8")

    # 既存規約の安全条件を保持し、通常処理のコマンドを単一検証へ変更する。
    p = root / "AGENTS.md"
    s = p.read_text(encoding="utf-8-sig")
    old = """uv run python scripts/validate_frontmatter.py --changed
uv run python scripts/validate_obsidian_markup.py --changed
uv run python scripts/check_graph_integrity.py --changed
uv run python scripts/update_index.py
uv run python scripts/append_log.py --operation ingest-paper --subject "paper:<id>" --detail "..."
uv run python scripts/wiki_lint.py --changed"""
    new = """uv run python scripts/check_changed.py --path "<changed-note.md>" --brief
# 索引・ログは取り込みフローが更新。個別の4検証器や全体lintは通常ここへ重ねない。
# 全体整備・フレームワーク変更時のみ: uv run python scripts/wiki_lint.py --no-write"""
    s = s.replace(old, new)
    s = s.replace(
        "7. Run changed-file lint.",
        "7. 手順2〜4の機械検証はcheck_changed.pyの1回にまとめる。重複lintは実行しない。",
    )
    p.write_text(s, encoding="utf-8")
    append(
        "AGENTS.md",
        "## 通常処理の軽量検証と図表",
        "文献処理の通常手順は `00_System/Processing-Policy.md` に従う。変更したノートと影響先をcheck_changed.pyで1回だけ検査し、全体監査・原典の意味的レビューを毎回重ねない。新規Source Noteは原図の図表を添え、技術詳細を折りたたむ。",
    )
    append(
        "00_System/PaperFlow-Integration.md",
        "## 軽量処理と図表付き要約",
        "通常の検証はcheck_changed.pyの1回にまとめる。新規凍結出典のfigures/paperflow-visuals-v1に元PDFから切り出した画像とvisuals.jsonを保存する。図表は標準3件、位置が不明確ならページ抜粋と明記する。要点・図表を先に置き、技術記録は折りたたむ。詳細はProcessing-Policy.md。",
    )
    append(
        "00_System/PaperFlow-Integration.md",
        "## PDFの視覚読解",
        "新規全文解析では、Claude CLIへPDFを直接添付し、Codex CLIへ全ページ画像を添付する。要約担当AIが図表を選び日本語で説明する。未完了・容量超過時は文字のみへ切り替えない。旧キャッシュを視覚読解済みとは扱わず、既存ノートの一括再解析も行わない。詳細はProcessing-Policy.md。",
    )
    for name in SKILLS:
        if name == "wiki-ingest-paper":
            append(
                f"agent-skills/{name}/SKILL.md",
                "## 視覚読解を伴う要約",
                '全文解析はPDFまたは全ページ画像を要約担当AIへ渡し、重要な図表の選択・日本語説明・対応する所見を解析JSONへ含める。PaperFlowは自動で実行する。手動Inbox経路も、視覚読解済みJSONを `python scripts/extract_paper_visuals.py --source-dir "10_Papers/10_Sources/<登録ID>" --analysis-json analysis.json` へ渡し、その選択と説明をSource Noteへ反映する。抽出テキストだけで図を読んだと記録しない。過去ノートは一括再生成しない。',
            )
        append(
            f"agent-skills/{name}/SKILL.md",
            "## 検証の実行回数と情報量",
            '通常の変更は `python scripts/check_changed.py --path "<実際に変更したノート.md>" --brief` を1回実行する。成功後に個別4検証器・wiki_lintを重ねない。全体監査は別作業。意味的照合は昇格する主張に必要な範囲とし、全引用論文へ拡大しない。新規論文ノートは図表付き、詳細な候補と処理記録は折りたたむ。00_System/Processing-Policy.mdを参照。',
        )
        p = root / f"agent-skills/{name}/SKILL.md"
        s = p.read_text(encoding="utf-8")
        # 古い推奨コマンドが新方針と競合しないよう、例中の重複呼出しを置き換える。
        s = re.sub(
            r"^uv run python scripts/(?:validate_frontmatter|validate_obsidian_markup|check_graph_integrity|resolve_links|wiki_lint)\.py --changed[^\n]*$",
            'uv run python scripts/check_changed.py --path "<changed-note.md>" --brief',
            s,
            flags=re.M,
        )
        for old_step in (
            "Validate Obsidian markup and Graph integrity, then run changed-file lint.",
            "Validate frontmatter, citations, links, tags, and Graph integrity; update indexes and log, then lint.",
            "Run frontmatter, Obsidian markup, citation, Graph-integrity validation, and full changed-file lint.",
        ):
            s = s.replace(
                old_step, "索引・ログ更新後、処理したファイルをcheck_changed.pyで1回検証する。"
            )
        if name == "wiki-ingest-paper" and "## 原図・表の添付" not in s:
            s += '\n## 原図・表の添付\n\nPaperFlowは新規出典から図表を自動抽出する。手動Inbox経路も凍結登録・本文抽出後に `python scripts/extract_paper_visuals.py --source-dir "10_Papers/10_Sources/<登録ID>"` を実行し、出力PNGをSource Noteへ埋め込む。元PDFや抽出本文は変更しない。図表が大きい場合は原ページへのリンクを添え、境界が不明ならページ抜粋と明記する。\n'
        command = 'uv run python scripts/check_changed.py --path "<changed-note.md>" --brief'
        lines = s.splitlines()
        last = max((i for i, line in enumerate(lines) if line == command), default=-1)
        s = "\n".join(line for i, line in enumerate(lines) if line != command or i == last) + "\n"
        if name == "wiki-lint":
            s = """---
name: wiki-lint
description: 通常は変更ファイルと参照元だけを一度検証する。全体監査は明示された場合に分離して実行する。
---

# Wikiの検証

AGENTS.mdと00_System/Processing-Policy.mdを読む。単一エージェントで完結する。

## 通常の文献取り込み・ノート更新

処理した実ファイルを指定して、次を1回だけ実行する。

```powershell
python scripts/check_changed.py --path "<changed-note.md>" --brief
```

複数ファイルは--pathを追加、または--paths-fileでJSON配列を渡す。Properties、本文、ID/名前の重複、リンク、関係、変更先への既存参照を同じ読取りコンテキストで確認する。

成功後に個別の4検証器やwiki_lint.pyを重ねない。結果は件数と未解決の指摘だけ報告し、全文JSONを会話へ繰り返し貼らない。内容を再編集した場合だけ再検証する。

## 全体監査

ライブラリ全体の整備・フレームワーク変更・明示された全体監査の時だけ実行する。

```powershell
python scripts/wiki_lint.py --no-write
```

全体監査は凍結原本、索引、スキル配布、リモート登録、古いInbox、全グラフ等を含む。通常取り込みのたびに過去の全PDFを読み直さない。定期登録は利用者承認後。

## 意味的なレビュー

Wikiへ採用する主張と必要な原典位置に絞る。未確認は明示し、全引用論文の精読へ自動拡大しない。機械検証で学術的な正しさや図表の解釈が証明されたとは記録しない。曖昧な修正は提案へ残す。

.obsidianは読み取り・走査・索引・検証の対象外。人間ノート、原本、根拠、既存の参照blockを警告を消すために書き換えない。
"""
        p.write_text(s, encoding="utf-8")
        for prefix in (".claude/skills", ".agents/skills"):
            dest = root / prefix / name / "SKILL.md"
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(p, dest)
    append(
        "00_System/Templates/Paper-Source-Note.md",
        "## 図表付き要約の表示規約",
        "新規自動生成ノートは「要点」の直後に「主要な図表」を置く。図表は登録済み出典のfigures配下への埋め込みとPDFページリンクで示す。境界が不明確ならページ抜粋と明記する。根拠候補・処理記録は折りたたむ。既存ノートの参照済み見出しは一括変更しない。",
    )
    marker = yaml.safe_load((root / ".llm-wiki-root").read_text(encoding="utf-8"))
    marker.update(
        framework_version="1.10", paperflow_processing_profile="incremental-with-visual-reading"
    )
    (root / ".llm-wiki-root").write_text(
        yaml.safe_dump(marker, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
