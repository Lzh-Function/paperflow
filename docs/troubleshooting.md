# 問題の切り分け

| 症状 | 対処 |
|---|---|
| G/Hがない | Drive for desktopの接続とログオンユーザーを確認。空の代替Vaultを作らない |
| `bibtex` がfalse | 自動BibTeX出力を設定し、実際の `.bib` を `source.bibtex_file` に指定 |
| `pdf_candidates_only` | 書誌の正本が未設定。PDF名から書誌は生成しない正常な保留状態 |
| `retry_wait`、エラーなし | PDFのサイズ・mtimeの安定を次のscanで確認。サイズ0は解析しない |
| `needs_review` | `match_reason` / レポートの照合理由を確認。[照合規則](pdf-matching.md)に足りない著者・年・版等を確認し、必要ならBibTeXの明示 `file` を使う。原本のリネームは不要 |
| `authentication` | 選択した公式CLIでログインし直す。アプリに秘密情報は渡さない |
| `cli_version` | `doctor`とCLIのhelpを確認。安全オプション非対応のCLIは自動で緩和しない |
| `cli_wrapper` | Windowsのバッチ起動を避け、ネイティブ `.exe` をcommand設定へ指定 |
| `schema` / `invalid_json` | レポートの理由と `.data/diagnostics/<run_id>.rejected.json` を確認。明確な文字列エスケープ不備は自動正規化済み。構造が曖昧な回答や全検証を通らない回答は採用しない。Claudeのresultは代用不可 |
| `rate_limit` / `timeout` | 待機後 `retry --failed`。回数を超えたら原因を調査し `analyze --force` で手動再評価 |
| `call_limit` | 今回の実行回数上限。次回のretryで継続。自動で他Providerへ送らない |
| `conflict` | Vault側を保持し `.data/conflicts/` の候補を確認。自動マージはない |
| Writerロックあり | 他端末/他Agentが作業中か確認。古いロックも勝手に削除しない |
| DBが空だが生成ノートあり | 同じDBを復元してから実行。DBを消して再syncしない |
| `missing_from_source` | BibTeXの出力範囲・一時エクスポート欠落を確認。記録・ノートは削除しない |
| 画像PDF/短い抽出 | 視覚読解ではPDF/全ページ画像を渡し、抽出不足を記録する。文字抽出のないPDFは凍結登録で停止する場合がある。OCRは未実装 |
| PDFリンクが開かない | 正式Wiki形式ではVault内の凍結PDFを参照する。同期完了とsource.pdfの存在を確認。旧形式のfile:///G:/リンクはWindows向け |

終了コードは0=成功、1=処理エラー、2=設定/引数エラー、3=論文単位の部分失敗、130=中断。`doctor`の未設定・未承認項目は診断上の警告であり、G/HやVaultマーカーの欠落は終了2。

## JSONと特殊記号

Claude・Codex・agy共通で、要約JSONの文字列値にある不正なバックスラッシュと未エスケープの改行・タブ等を正規化する。文字・記号を削除せず、補正箇所の数を要約の「未確認事項」へ記録する。正しい `\\`・`\n`・`\t`・`\uXXXX`、引用符、日本語、数式、SMILESはそのまま保持する。追加のAI呼び出しは行わない。

不足した引用符・括弧、不完全なUnicodeエスケープ、重複キー、複数JSONなどは推測で補わない。補正後もSchema・管理語彙・全ページ読解・図表参照の検証に通らなければ不採用にする。キー名やCLI通信の外枠には文字列の自動補正を適用しない。

不採用回答の `raw_response` は元の回答文字列、`parse_error` は構文エラーの位置と理由。形式補正後も構文エラーとなった場合は `normalized_response` に補正を試みた文字列を残し、`parse_error.position_source` でエラー位置がどちらの文字列を指すかを示す。コードフェンス等を除いたJSON断片の位置も、元の回答の位置とは区別する。補正候補も不採用のままで、自動復旧・自動再送はしない。使用量・所要時間は同じ解析実行の記録に保持する。回答を取得できなかった場合は生回答が存在しない。

DBは既定のDELETE journalで単一Writer。実行停止後に `.data/state.db` をローカルへバックアップする。解析JSONにも論文の情報が含まれ得るため `.data/` は非公開にし、必要なら停止中にローカル結果/衝突候補を手動削除する。DB、Vault、解析JSONの自動TTL削除は実装していない。CLIのユーザー領域のログ保持は各CLIの仕様に従う。
