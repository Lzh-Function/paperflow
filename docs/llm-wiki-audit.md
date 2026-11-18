# LLM-Wiki接続状況の監査

確認日: 2026-11-17（日本時間）。結論は**共通形式への部分適合、標準ワークフローへの接続は未完成**。本番Vaultには `PaperFlow/` 自体がまだ存在しない。

## 確認方法

VaultのAGENTS.mdと指定ポリシー、辞書、Schema、テンプレート、`wiki-ingest-paper` / `wiki-promote` / `wiki-query` / `wiki-lint` の契約を読み取り確認した。スキルの取り込み・昇格・修復処理は実行していない。

WindowsネイティブPythonで、Vaultの検証スクリプト・辞書・SchemaのコピーをC:の一時ディレクトリに置いた。合成BibTeX・合成PDF・FakeProviderから4ノートを生成し、コピーした検証器へ明示パスを渡して検証した。`LLM_WIKI_ROOT` は一時fixtureに固定し、bytecode生成を無効化した。本番Vault上のスクリプト実行、索引更新、lint監査ファイル出力、実PDFの読み取り・外部送信は行っていない。`.obsidian/**` は参照・探索対象外。

証跡: `.data/wiki-compatibility-audit-2026-11-17.json`。読み取った契約ファイル群のSHA-256・mtimeは検証前後で不変。これはVault全体をハッシュした検証ではない。

## 現在の出力契約

本番書き込みを承認・有効化した場合の出力先:

```text
LLM-Wiki/PaperFlow/
  Papers/<paper_id>.md      書誌・PDF/DOI参照・人間用メモ
  Analysis/<paper_id>.md    日本語解析・根拠候補・解析記録
  Reports/top-rated.md     読書優先度一覧（report --write）
  Inbox/review-needed.md   照合・失敗等の要確認一覧（report --write）
```

- Papersは `type: personal-note`、`owner: human`、`agent_editable: false`。作成後は上書きしない。
- Analysisとレポートは `type: output`、`owner: agent`。管理対象ハッシュが一致する場合だけ更新する。
- `area: academic`、`area/academic`、型に対応する `note/*` タグ、永続ID、レビュー状態を持つ。AI分野の分類は `topics`。
- 文献ノートは `registration_status: external-unregistered`、`source_ids: []`。Analysisは `evidence_status: unverified`。`external_source_id: paperpile:<paper_id>` はPaperFlow内部の追跡値で、Vault登録済みの `paper:*` 出典IDでもPaperpile公式の取得済みIDでもない。
- 原本はG:を参照し、PDFをVaultへ複製しない。PDFのローカルURIは他PCでの可搬性を保証しない。

これは登録済み `paper-note` / Canonical Wikiではなく、**登録・確認前の候補**という契約。

## 実機で確認した未接続箇所

| 項目 | 現状と影響 |
|---|---|
| 標準探索範囲 | `scripts/_vault.py:80` の `NOTE_ZONES` に `PaperFlow` がない。通常のlint・索引生成では自動収集されない |
| 出典索引 | `scripts/update_index.py` の `SOURCE_SECTIONS` は `10_Papers/20_Source_Notes` 等の固定パス。PaperFlowの候補は出典索引へ自動登録されない |
| エージェント検索 | `wiki-query` の検索例・取得順は索引、50_Wiki、標準Source Notes等。PaperFlowを読むには別途そのパスの指定が必要 |
| スキル接続 | PaperFlow専用の引渡しスキル・キュー・受領記録はない。解析CLIは独立stagingでツールを制限し、Vaultのスキルを自動実行しない |
| 出典登録 | Vaultの `register_source.py` は `10_Papers/10_Sources/<id>/source.pdf` へ凍結コピーしmanifestを作る。PaperFlowの `copy_pdf: false` とそのままでは両立しない |
| 出典の重複照合 | PaperFlow内部の文献識別はあるが、Vaultの登録済み出典とのDOI・ハッシュ・版照合とID対応表は未実装 |
| Source Note変換 | `Paper-Source-Note.md` の形式への変換、登録済み `source_id` / `source_file` の解決、所見と根拠の表、著者解釈・エージェント推論の独立節などは未実装 |
| 根拠の受け渡し | ページ候補はあるが、Vaultの登録済み抽出物・安定したblock IDへの対応と原典照合がない。文字数超過・部分解析の情報は保持する |
| グラフ統合 | 既存ページ・aliasesを検索して関連先を解決する処理、Concept/Claimへのリンク、統合提案はない。`topics` は語彙分類で、グラフの辺ではない |
| 単一Writer | 既存Vaultロックの存在を検査して停止するが、PaperFlow自身はVault共通ロックを取得しない。C:のローカルロックだけでは他のWikiエージェントと排他できない |
| 完了処理 | Wiki側の索引・MOC・追記ログ・changed-file lint・昇格の記録を行わない |

共通プロパティがあることやPaperFlow側の解析JSON Schemaに通ることは、Wikiへの登録・統合完了を意味しない。

## 合成fixtureの検証結果

| 検証 | 結果 |
|---|---|
| Vault版frontmatter検証器を4ノートへ明示実行 | **0エラー・0警告**。現在宣言している personal-note / output としての共通形式は適合 |
| Vault版Markdown検証器を同じ4ノートへ明示実行 | **0エラー・14警告**。内訳は以下 |
| 同じfixtureを通常のnote探索・出典索引生成へ投入 | **収集0ノート、出典索引に掲載なし**。PaperFlow配下の4ノートが探索範囲外であることを再現 |
| Analysisのtypeとタグだけをpaper-noteに変えた対照試験 | **2エラー**。`id` が `paper:*` でなく、`source_id` もない。名前や型の変更だけでは正式登録にならない |

Markdownの14警告は次の通り。警告を隠す設定変更は行っていない。

- **10件:** 日本語H2の重複扱い。Vault検証器の `normalize()` がASCII英数字以外を除くため、異なる日本語見出しがすべて空文字になって衝突する。日本語本文・見出しの要件を保持し、Vault検証器の日本語対応を検討する必要がある。
- **2件:** 原本PDFへの `file:///...` がVault内部Markdownリンクと扱われる。Vault外の原本参照を許容・識別する契約が必要。
- **2件:** レポートのfrontmatter `title` とH1の `PaperFlow` 接頭辞の不一致。こちらはPaperFlow側で整合させられる機械的な差分。

検証器の0エラーは学術的な根拠の正しさ、登録済み出典、リンク解決、正式なグラフ統合を保証しない。14警告と探索範囲の欠落が残るため、完全適合とは判定しない。

## 推奨する差し込み方（未実装）

```text
Paperpile原本 + BibTeX
  → PaperFlow（抽出・日本語解析・候補JSON）
  → 登録済み出典との照合／新規外部出典の登録
  → 10_Papers/20_Source_Notes の正式Source Note
  → 70_Review/Merge_Proposals の関連付け・統合提案
  → wiki-promote等による既存50_Wikiへの統合
  → 索引・MOC・ログ・changed-file lint
```

後段はVaultの既存フレームワークを使う。PaperFlowは文献単位の抽出・解析を担当し、Canonical Wikiを独自に増設しない。

1. **既にVaultへ登録済みの論文**は、DOIだけで同一版と断定せずハッシュ・版も照合し、既存 `paper:*` とSource Noteを再利用する。新たなPDF複製は不要。
2. **未登録の論文**でG:原本のみを維持するなら、Vault側に外部出典manifest・外部原本の検証・抽出物の位置の契約が必要。現在の標準登録スクリプトに外部参照専用モードはない。
3. ID対応表・入力ハッシュ・解析範囲・根拠locator・処理履歴を引渡し単位に含める。候補を登録済みやverifiedへ機械的に書き換えない。
4. 共通Writerロック、重複判定、日本語lint互換性、再実行時の冪等性、索引・ログの整合までC:の合成fixtureで検証してから本番へ接続する。

PDFのVault内凍結コピーを許可する方式もあるが、現行 `copy_pdf: false` の変更を伴う。今回の確認だけでは外部出典方式も凍結コピー方式も採用・実行していない。本番への初回書き込みやVault側のフレームワーク変更も行っていない。
