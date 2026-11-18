# Codexによる同じ3件の本文試験

2026-11-16、利用者の「codexでもテストしてください」という明示依頼を受け、Claude版と同じ3論文をWindowsネイティブCodex CLI 0.145.0で解析した。

| 対象 | ページ | 本文文字数 | 成功時の解析時間 | Codexの読書優先度 |
|---|---:|---:|---:|---:|
| Bayesian GCN（分子物性・不確実性） | 9 | 67,925 | 36.86秒 | 5 |
| A Survey on Latent Reasoning | 38 | 138,722 | 42.25秒 | 4 |
| ImageNet in 1 Hour | 12 | 86,193 | 43.25秒 | 4 |

合計59ページ、292,840文字。3件とも全文テキストで切り詰めなし。同じSchema・プロンプト・129主題語・研究文脈で、Claude版と本文・書誌が同じことを照合した。全件がSchema、taxonomy、coverage、ページ番号の検証を通り、日本語の読書メモ3件・解析3件を生成した。

## 保存先

```text
C:\Users\TAKUH\Documents\paperflow\.data\trials\2026-11-16-codex-e0122512\
  README.md             # Codex/Claudeの要約を並べて開く入口
  config.yaml           # Codex用、対象3件限定、出力はC:
  trial.bib             # 同じ3件の書誌だけ
  state.db              # Codex試験専用DB
  manifest.json         # 承認範囲・入力ハッシュ・バージョン
  verification.json     # 検証結果
  codex-diagnostics.json # 本文を含まないJSONLイベント種別・使用量
  prepared\             # 比較用のローカル抽出結果
  cache\analysis\       # 検証済みJSON
  preview-ja\PaperFlow\ # 日本語ノート6件
```

Claude版は `2026-11-16-952246cb` に保持している。本番DBとClaude版DBはハッシュ不変を確認し、本番Vaultへは書き込んでいない。

## 再実行

```powershell
Set-Location 'C:\Users\TAKUH\Documents\paperflow'
$env:PYTHONUTF8 = '1'
$pf = Join-Path $PWD '.venv-win\Scripts\paperflow.exe'
$trial = Join-Path $PWD '.data\trials\2026-11-16-codex-e0122512\config.yaml'
& $pf --config $trial sync --dry-run --provider codex --limit 3
& $pf --config $trial sync --provider codex --limit 3
& $pf --config $trial status
```

対象は試験用BibTeXの3件に限定される。同じ入力の再実行は、外部Providerを呼ぶと失敗する検証ガードを付けて確認し、追加呼び出し0・解析0・ノート更新0だった。

単一ワーカー、最大3件・3 CLI呼び出し、入力上限20万文字、タイムアウト240秒、自動リトライなし。今回のCodex呼び出しにはドル建ての費用上限を指定していない。診断用の `--json` はイベントの種別・使用量のみを記録し、最終解析は通常と同じ `-o` のJSONファイルから取得した。記録イベントにコマンド実行・MCP・検索・ファイル変更はなかった。

## 結果の読み方

この試行では、Codexは主な結果を5〜6項目にまとめ、Claudeは9〜13項目で実験数値や引用研究を詳しく展開した。モデルの既定設定を用いた各試行の成功時間を記録している。長文サーベイのClaude時間は再試験時の値。

優先度・主題語は同じ入力でも差がある。Bayesian GCNはClaudeで4、Codexで5。Codexが付けたstructure-based-drug-designとai-safetyは、本文の中心課題に絞って分類する場合の見直し候補。Schema通過は要約内容や分類の全面的な原典照合を意味せず、evidence_statusはunverifiedを維持した。

今回の承認範囲はこの同じ3件のCodex本文試験。本番Vaultへの初回書き込み、他の論文の送信、定期実行登録は行っていない。
