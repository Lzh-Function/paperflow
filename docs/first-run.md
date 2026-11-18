# 3件から使い始める

**2026-11-17追記:** 本文解析済みの同じ3件を再利用し、利用者の差分確認・承認後にH:へ正式登録した。追加AI送信なし。現在の接続設定は `.data/wiki-integration/first-run-config.yaml`、正式な登録・提案の運用は [Wiki統合](wiki-integration.md)。以下は最初のC:解析試験の履歴で、旧 `PaperFlow/` 出力は互換モードとして残す。

2026-11-16に利用者が承認した、ClaudeによるPDF本文解析の試運転です。対象は下記3件だけ。Paperpileのライブラリ全体を解析する設定にはしていません。

| 対象 | PDFページ数 | 送信する抽出本文の文字数 |
|---|---:|---:|
| A Bayesian graph convolutional network for reliable prediction of molecular properties with uncertainty quantification | 9 | 67,925 |
| A Survey on Latent Reasoning | 38 | 138,722 |
| Accurate, Large Minibatch SGD: Training ImageNet in 1 Hour | 12 | 86,193 |

PDFファイル自体を添付せず、ローカルで抽出したページ番号付き本文テキストをClaude Code CLIに渡します。今回は3件とも全ページのテキストが20万文字の上限内に収まり、切り詰めなしです。画像としての図表・数式の視覚的な理解はこの経路では行いません。

## 試験専用の場所

```text
C:\Users\TAKUH\Documents\paperflow\.data\trials\2026-11-16-952246cb\
  config.yaml       # この3件だけの実行設定
  trial.bib         # 対象3件だけの書誌スナップショット
  state.db          # 試験専用DB
  manifest.json     # 対象ID・原本ハッシュ・送信分量・承認の記録
  prepared\        # ローカルのページ付き抽出結果
  cache\analysis\  # Schema検証済みの解析JSON
  preview-ja\PaperFlow\
    Papers\         # 書誌・読書メモ欄
    Analysis\       # 実際のClaudeによる日本語解析
```

本番の `config.yaml` はfake / abstract_only / Vault書き込み無効のままです。試験設定だけで本文送信を有効にし、出力先もC:の専用フォルダにしています。原本PDFと本番Vaultは変更しません。自動リトライなし、単一ワーカー、最大3件、CLI呼び出しも最大3回、1回のタイムアウト240秒。初回の費用枠は0.5でしたが、長文サーベイの再試験では1.0に変更しました。現在の試験設定では対応するClaude CLIへ `--max-budget-usd 1.0` を渡します。

## 実行と結果の確認

この試験用configは対象が3件しかないため、下記コマンドで数百件が一括投入されることはありません。

```powershell
Set-Location 'C:\Users\TAKUH\Documents\paperflow'
$env:PYTHONUTF8 = '1'
$pf = Join-Path $PWD '.venv-win\Scripts\paperflow.exe'
$trial = Join-Path $PWD '.data\trials\2026-11-16-952246cb\config.yaml'

# 対象と件数だけを表示。AIは呼ばない
& $pf --config $trial sync --dry-run --provider claude --limit 3

# 承認済み3件の本文解析。出力先はC:のみ
& $pf --config $trial sync --provider claude --limit 3

# 成否・所要時間と読書優先度を表示
& $pf --config $trial status
& $pf --config $trial report --min-priority 1
```

同じ入力と設定のまま再実行すると、成功済み解析はキャッシュから再利用します。`--force` は新しい解析を明示的に行う操作なので、再送したい場合だけ使います。Provider変更も別の解析として扱われます。この試運転での外部送信承認はClaudeによる上記3件を対象としています。

確認する内容は、要約が論文と合っているか、`topics`と読書優先度が興味に合うか、根拠ページと不確実性の表示が適切かです。生成したページ根拠は自動照合済みを意味せず、ノートの `evidence_status` はunverifiedです。

## 本番運用へ進む順序

1. C:の3件の要約を確認し、必要ならプロンプト・研究文脈・主題語を調整します。
2. 本番Vaultへの初回書き込みを利用者が承認した後、まずこの3件を専用の `PaperFlow/` 配下へ出力します。既存ノートの上書きはしません。
3. 結果がよければ対象を10件ずつに広げます。PDF本文の外部送信を継続的に許可する範囲も利用者が決めます。
4. 手動運用が安定してからwatchやタスクスケジューラを検討します。今回、定期実行は登録していません。

LLM-Wikiの正式な出典登録・索引・ログ更新は別の未完成工程です。現在のノートはLLM-Wikiのプロパティ形式に合わせた未登録の生成候補です。詳細は [LLM-Wiki適合方針](llm-wiki.md) を参照してください。

## 実施結果

3件ともfulltext・切り詰めなしで解析し、読書メモ用3ノートと解析3ノートをC:へ生成しました。長文サーベイの初回だけCLIが失敗し、1件の再試験後に成功しています。初回失敗の直接原因はログから確定していません。

全件が共通Schema、管理語彙、入力に存在する根拠ページの検証を通過しました。代表的な数値を3論文の抽出元ページとスポット照合しましたが、全主張の原典照合完了ではありません。同一入力での再実行は、外部Provider呼び出し0・解析0・ノート更新0を確認しました。

実行結果・プレビュー入口は試験フォルダの `README.md`、機械可読の検証結果は `verification.json`。再試験のCLIコスト報告値は `claude-diagnostics.json` にその1件分だけ記録しています。

生成ノートの本文・見出し・表示ラベルは日本語です。C:の見出し日本語版は `preview-ja/` に保存しています。保存済み解析JSONから再描画しており、日本語化のための追加AI送信は行っていません。

## Codexでの同じ3件の試験

利用者の追加依頼を受けて、同じ全文入力3件をCodexでも解析し、全件成功した。Claude版と別のC:出力を保持している。[Codex試験の手順と結果](codex-trial.md)を参照。
