# PDF・図表を読んだ日本語要約

全文解析では、要約担当AIへ本文に加えて視覚入力を渡す。

## Providerごとの入力

| Provider | 視覚入力 | CLIの実行権限 |
|---|---|---|
| Claude Code CLI | 送信用PDFをdocumentブロックとして添付。必要時は画像調整・100ページ単位の分割を行う | ツール無効。Readやシェルの権限を広げずに渡す |
| Codex CLI | PDF全ページを順番にPNG化し、--imageで添付 | シェル・Web・画像読取ツール等は無効のまま。画像は最初の入力に添付 |

ClaudeではPDFを先に置き、解析指示を後ろへ置く。本文はPDF内から読むため、抽出全文を別のテキストとして重複送信しない。書誌・ページ番号・研究文脈は引き続き渡す。Codexではページ画像に補助本文を添える。

どちらもWindowsネイティブで動く。PDFの固定コピーはC:の一時領域に作り、SHA-256とページ数を検査する。G:原本は変更せず、ProviderにG:/H:のパスやアクセス権は渡さない。送信用に調整した場合も、Vaultへ凍結するPDFは原本と同じものを使う。CLIのユーザー設定・MCP等の隔離も維持する。ClaudeのPDF添付経路はページ読取ツールを使わないため、PopplerやWSLは不要。

## 一連の流れ

1. PaperpileのBibTeXとPDFを照合し、選択したProvider・モデルを確定する。
2. ページ番号付きの本文と、原PDFまたは全ページ画像を用意する。
3. **同じ要約担当AI**が図表・凡例・軸・実験条件も読み、要約と重要な図表の選択、日本語説明をJSONにまとめる。
4. 共通の視覚読解Schemaで、全ページの完了申告、選択ページ・図表番号・対応する所見の整合を検査する。
5. プログラムがAIの選択した図表を元PDFから切り出す。境界を確定できない場合は、ページ全体と明記する。
6. Source Noteへ要点・図表・日本語説明・所見を出力する。変更ノートと参照元の検証は1回にまとめる。
7. 許可済みのVault出力では、凍結PDF・図表・Source Note・統合提案・索引・ログを反映する。50_Wiki本文への統合は別工程。

以前の「captionの規則で図表を選んで添付する」方式とは異なり、新しい全文解析ではAIが視覚入力から選ぶ。画像処理プログラムは切り出しを担当する。手動の抽出コマンドは、視覚読解済みJSONを `--analysis-json` で指定できる。

## 完了条件と再利用

視覚読解を有効にした全文解析では、視覚出力がない・読解未完了・ページが不足する結果を採用しない。入力上限やモデル非対応でも、抽出テキストだけの要約へ自動で切り替えない。

AIのpages_viewedは読解の自己申告である。全ページを実際に入力へ添付することと、応答の整合性をプログラムで確認するが、図の解釈の正確さを機械的に保証するものではない。evidence_statusと人間レビューは引き続きunverified / unreviewed。

視覚解析のSchemaはv2-visual。従来のv1解析は再利用・表示できるが、視覚読解済みとは記録しない。新たな全文解析では入力方式・PDF・モデル・プロンプト・図表件数をキャッシュ判定へ含める。保存済みの同条件の視覚解析は再利用し、図の解説だけのために2回目のAI解析を必須にしない。

既存のレビュー済みノートや手動編集を自動で再生成しない。すでに登録したPDFに新解析を適用する場合も、本文の更新は差分として確認する。新規出典は図表生成まで自動で行う。

## 設定

```yaml
analysis:
  policy: fulltext_external
  allow_external_fulltext: true
  require_visual_reading: true
  visual_max_pages: 300
  visual_max_pdf_mb: 64
  visual_max_image_mb: 128
  max_chars: 3000000

destination:
  include_visuals: true
  visual_max_items: 3
```

現在の初期値はPaperpile実測に基づく。上限超過時は停止し、対象を決めて上限・モデル・時間設定を見直す。ページを勝手に間引かない。視覚入力はテキストだけの解析よりトークンを消費する。上記は運用上の上限であり、各モデルのコンテキスト制限を保証しない。

通常のconfig.yamlは引き続きFakeProvider・抄録のみ・外部全文送信無効・Vault書き込み無効である。実論文の追加送信や数百件の処理を、この改修で開始したわけではない。

全341ファイルを測定した最大値と変更根拠、計測画像の後始末は [現在の入力上限](input-limits.md) に記録した。以前の100ページ・24MiB等の設定可能範囲を、モデル側の制限とは扱わない。

## 実機の確認

色と形の名前をテキストに含めず、赤い三角形と青い長方形だけを描いた合成1ページPDFを使用した。

- Claude: PDFを直接添付し、色・形・配置の読解、ページ全体の選択、日本語説明、関連所見のJSON出力まで成功。
- Codex: 同じPDFのページ画像を添付し、同様の読解・選択・説明まで成功。
- 製品の両Providerと共通Schemaを通過。実論文の新たな送信0、既存Vault本文の変更0。

結果は `.data/vision-probe/claude-managed-result.json` と `codex-managed-result.json`。画像と入力PDFも同フォルダの合成fixtureだけ。実論文の図表読解精度や、長大PDFの成功率まで実証したものではない。

Windowsネイティブのテストは189件成功。Ruff、wheel/sdistビルドも成功。H:へ反映するのは処理方針・スキル・抽出スクリプト等8ファイルだけで、既存論文本文は変更しない。反映証跡は `.data/visual-reading-framework/applied.json`。

参考: [Claude Code CLIのストリーム入力](https://code.claude.com/docs/en/cli-reference)、[ClaudeのPDF入力](https://platform.claude.com/docs/en/build-with-claude/pdf-support)、[Codex CLIの画像添付](https://learn.chatgpt.com/docs/cli/reference)。

## 視覚読解を完了できないと返された場合

AIが添付を認識できないと回答した場合も、未確認の視覚読解結果は採用しない。PDFそのものを送ったことと、モデルが内容を読めたことは別に扱う。検証で不採用になった回答と送信形式・PDFハッシュ・ページ数等はC:の `.data/diagnostics` に診断用JSONとして保存し、正常な解析キャッシュやVault登録には使わない。本文・図表の確認を省略して成功扱いにすることはない。

PDFと抽出全文の重複除去は、入力コンテキストの浪費を減らす。モデル固有の上限や画像の解釈の誤りがなくなるわけではない。PDFを先、指示を後にする構成は [Anthropicの長文入力の指針](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/claude-prompting-best-practices) も参照。

## 大容量PDFのClaude入力準備

Claude CodeにはAPIとは別の添付・リクエスト容量の制約があり、上限超過時に添付を取り除いて再試行することがある。[Claude Codeのエラー仕様](https://code.claude.com/docs/en/errors#request-too-large)。このPCでは合成16MiBのPDFは視認でき、19MiB以上では添付を認識できない応答になった。固定の厳密な障害境界を全環境で保証する測定ではない。

PaperFlowはClaudeへ添付する作業PDFの合計を16MiB以内へ準備する。大きい場合は最初に画質を変えない再圧縮を試し、収まらなければ180dpiを超える埋め込み画像を150dpi・JPEG品質85へ調整する。文字・ベクトルと全ページを保持し、抽出文字列とページ寸法の一致を確認する。画像調整は非可逆なので、要約の不確実性にも処理内容を記録する。これでも収まらなければ送信前に停止する。

100ページを超えるPDFは、ページを省略せず順番に100ページ以下の添付へ分け、元PDFの通しページ番号との対応をAIへ渡す。全体のモデルコンテキスト制限は引き続き適用される。

重い再圧縮はWindowsネイティブPythonの別プロセスで実行し、GUIのイベント処理を占有しない。原本、再圧縮、画像調整、分割の作業コピーはすべて一時領域に置き、通常終了・例外・タイムアウト時に後始末する。原本のG:には書き込まず、全ページPNGを永続キャッシュに積まない。
