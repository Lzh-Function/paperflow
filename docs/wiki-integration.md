# Obsidianへの保存形式

操作を始める場合は [使い方マニュアル](usage.md) を参照。本書は保存形式・状態・連携契約の詳細。

G:のPDF原本は変更せずVaultへ凍結コピーする。正式なSource Noteまで自動化し、Canonical Wikiへの追加・変更はレビュー用提案で止める。論文の通常入口をPaperFlowに置き、Vault側のPaperワークフロー・スキル・検証をこの自動化に合わせる。

## 出力と状態

```text
10_Papers/10_Sources/paperflow-<PDFハッシュ先頭20桁>/
  source.pdf                 G:原本とSHA-256が一致する凍結コピー
  manifest.yml               出典ID・ハッシュ・書誌・登録と抽出の来歴
  extracted.md               ページ・block付きの抽出本文
  extracted.json             ページ・block付きの機械可読抽出
10_Papers/20_Source_Notes/<原題の短縮名> -- <出典slug>.md
70_Review/Merge_Proposals/<出典slug>.md
00_System/PaperFlow/receipts/<PaperFlow ID>.json
```

Source Noteは `type: paper-note`、`source_id: paper:...`、登録済み `source_ids`、凍結PDFへの `source_file` を持つ。型を変えるだけの変換ではない。主な結果の所見表、限界、研究との関連、未確認事項、保存済み解析IDと解析範囲を記録する。

出典のバイト一致確認と、主張の意味的な裏付けを区別する。根拠のページ・詳細locatorを保持し、根拠未照合は `evidence_status: unverified`、人間レビュー前は `review_status: unreviewed`。抽出物のblockは安定IDで保存するが、主張との対応を確認するまでは割り当てない。保存済み解析で独立していない著者解釈は補完済みとせず、その不足を記す。

`integration_status: proposal-pending` と実在する `integration_proposal` を対で持つ。提案には対象出典とSource Noteへのリンク、既存Wikiの原題・aliases・topics一致候補を記録する。主題語の一致は意味的な関係を保証せず、候補として表示する。`other` は関連候補の根拠にしない。50_Wikiの学術ページは自動作成・更新しない。

## 設定

```yaml
destination:
  vault_dir: 'H:\My Drive\LLM-Wiki'
  layout: llm_wiki
  copy_pdf: true
  allow_vault_write: false
```

`allow_vault_write`でVault書き込みを制御する。従来出力を使う場合は`layout: paperflow` / `copy_pdf: false`を対で指定する。

Provider・モデル・外部本文送信・Vault書き込みの設定は独立している。定期実行の登録は別操作。

## 保存済み解析を引き渡す

```powershell
# 一覧のみ。ディスク・DB・Vault・外部CLIを変更しない
paperflow --config .data/wiki-integration/first-run-config.yaml wiki-prepare --cached-only --dry-run

# C:へ検証済みの反映用bundleを作る。Vaultはまだ変更しない
paperflow --config .data/wiki-integration/first-run-config.yaml wiki-prepare --cached-only --output C:\pf-review\first --upgrade-framework

# レビュー・初回承認後、許可済み設定で反映
paperflow --config APPROVED_CONFIG wiki-apply --bundle C:\pf-review\first
```

`--paper-id ID` を複数指定して対象を制限できる。保存済み解析時のPDFハッシュを再確認し、現在有効な解析だけを使う。AIを呼ばず、Provider・モデルは変更しない。`--upgrade-framework` は初回導入専用。

通常運用は `sync` から書誌観測・抽出・解析・同じ登録/提案フローまで進む。`sync --dry-run` は既存の無副作用契約を維持する。正式Wikiモードの `report --write` は `60_Outputs/PaperFlow/` へ保護付きで出力し、レポートは正式Source Noteへリンクする。

## 重複・編集・再実行

- 同一PDFハッシュは既存の凍結登録とSource Noteを再利用する。既存の別Source Note本文は変更しない。
- 同じDOIでPDFが異なる場合は自動マージせず、版の確認で停止する。
- 同じ解析ID・同じ管理対象ハッシュなら、PDF・ノート・提案・ログを増やさない。
- PaperFlowが生成したノートでも手動編集後は上書きせず停止し、C:の差分を保持する。
- 調査・準備後にVaultの管理ノート・契約・manifestが変わると反映を停止する。
- Windowsのパス長に合わせてファイル名の原題部分だけを短縮する。完全な原題と永続IDはPropertiesに保持し、既存ファイルは改名しない。

## 反映・排他・復旧

C:のbundleは `preview/`、`plan.json`、変更前の `originals/` と `journal.json` を持つ。ファイル単位の変更前後ハッシュを固定し、本番の直前にも一致を確認する。反映できるパスを出典・Source Note・提案・索引/MOC・必要な制御プレーンに限定する。

Vault共通Writerロックを排他的に作成する。同じホストでも既存ロックを上書きしない。Drive自体は分散ロックを提供しないため、別デバイスの作業停止・同期完了の運用規則は維持する。

通常の例外・Ctrl+Cは自分が書いたバイトだけを確認して巻き戻す。journalは原子的に保存する。プロセス強制終了・PC停止後は、別Writerが動いていないことを確認して残存ロックを手動解除し、同じbundleの `wiki-apply` で未完了journalを復旧して再試行する。他者の編集が入っていれば自動復旧でも上書きしない。

ファイル集合全体がファイルシステムの単一トランザクションになるわけではない。journalとハッシュを用いて中断を検出・復旧する。原本や人間ノートの削除、Inboxへの原本移動、原本のfinalizationは行わない。

## Vault側の移行

- AGENTS.mdと既存 `wiki-ingest-paper` をPaperFlow正規入口へ適合する。
- `wiki-promote` にレビュー待ち提案の受け方を追加する。
- 変更した2スキルだけをClaude用・Codex用配布先へ同期する。
- 日本語見出しの正規化と日本語の所見表をlintで認識する。旧英語形式も保持する。
- 実在するレビュー待ち提案が対象出典を参照していれば、未統合を情報表示にする。根拠verifiedやintegratedには昇格しない。
- 共通ロック取得を排他的な作成へ変更する。
- Source Noteの統合状態をSchemaに追加し、schema_version 6 / framework_version 1.8と移行ログを記録する。
- 出典索引・area索引・Academic MOC・追記ログだけを必要な範囲で更新する。Dropbox索引や無関係なスキル配布差分は変更しない。

既存の手動Inbox取り込みは互換経路として残す。通常のPaperpile論文をInboxへ手動投入する必要はなくなる。

## Wiki本文へ統合する次の段階

登録済みSource Noteの原典を確認し、既存ページと別名を検索した上で、必要最小限の統合差分を別に準備する。具体例は [Bayesian GCNのレビュー手順](wiki-promotion.md)。通常取り込みはWiki本文を編集せず、統合は承認済みplanのSHA-256を指定した `wiki-apply` だけで実施する。統合済みSource Noteの再解析は、引用される見出し・根拠blockを保持するレビュー差分が必要となる。
