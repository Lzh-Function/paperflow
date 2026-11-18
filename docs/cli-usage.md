# PaperFlow CLI操作マニュアル

Paperpileの論文を読み、日本語の要約・図表・PDFをObsidianへ保存する。

| 取り込み方 | 指定 |
|---|---|
| 未取り込みの論文を全件まとめて取り込む | `batch-plan --all` |
| 未取り込みから指定本数を乱択する | `batch-plan --count 本数` |
| 登録済みを含めた累計本数を指定する | `batch-plan --total 本数` |
| 新着・未処理分を追加する | 新しい一覧を`--all`で作る |

## 1. 設定ファイルを編集する

[config.production.yaml](../config.production.yaml)をメモ帳等で開く。

```text
C:\Users\TAKUH\Documents\paperflow\config.production.yaml
```

Claudeを使う場合は、次の4項目を検索して値を合わせ、保存する。

| 項目 | 設定する値 |
|---|---|
| `default_provider` | `'claude'` |
| `policy` | `'fulltext_external'` |
| `allow_external_fulltext` | `true` |
| `allow_vault_write` | `true` |

該当する行の値だけを変更し、項目名・字下げ・他の項目を残す。設定を保存した時点では、論文の送信や取り込みは始まらない。

Codexを使う場合は`default_provider`を`'codex'`にし、以降のコマンドの`--provider claude`も`--provider codex`へ変更する。

## 2. PowerShellを開き、モデルを選ぶ

WindowsのスタートメニューからPowerShellを開き、次の2行を順に実行する。

```powershell
Set-Location 'C:\Users\TAKUH\Documents\paperflow'
.\.venv-win\Scripts\paperflow.exe --config .\config.production.yaml select-model --provider claude
```

モデル候補の番号を入力してEnterを押す。選択が保存される。以降も同じPowerShell画面を使う。

## 3. 取り込み対象の一覧を作る

最初に、書誌とPDFの状態を読み込む。

```powershell
.\.venv-win\Scripts\paperflow.exe --config .\config.production.yaml scan
```

続いて、A・B・Cのどれか1つを実行する。いずれも対象一覧を保存する段階で、まだAI解析は始まらない。

### A. 全件を一括で取り込む

```powershell
.\.venv-win\Scripts\paperflow.exe --config .\config.production.yaml batch-plan --all --output .data\batches\import-01.json
```

処理可能な未取り込み論文をすべて選ぶ。すでにObsidianへ登録した論文は除外する。PDF未対応・同期中・既知のエラーなどは除外され、`excluded`に件数が表示される。

### B. 指定した本数を取り込む

```powershell
.\.venv-win\Scripts\paperflow.exe --config .\config.production.yaml batch-plan --count 5 --output .data\batches\import-01.json
```

`5`を追加したい本数へ変更する。未取り込みから乱択する。候補が指定本数より少ない場合は`shortage`に不足数が表示される。

### C. 累計本数を指定する

```powershell
.\.venv-win\Scripts\paperflow.exe --config .\config.production.yaml batch-plan --total 50 --output .data\batches\import-01.json
```

`50`を累計の目標本数へ変更する。すでに登録した本数を差し引き、不足分を乱択する。目標に達していれば追加0本になる。

### 一覧を確認する

```powershell
notepad.exe .data\batches\import-01.json
```

`selected_count`が今回の本数、`items`の`title`が論文名。保存場所は次のとおり。

```text
C:\Users\TAKUH\Documents\paperflow\.data\batches\import-01.json
```

同名ファイルは上書きしない。既に存在する場合は`import-02.json`等の新しい名前を使い、以降のコマンドも同じ名前に合わせる。

## 4. 一覧の論文を取り込む

```powershell
.\.venv-win\Scripts\paperflow.exe --config .\config.production.yaml batch-run --plan .data\batches\import-01.json --provider claude --no-interactive
```

このコマンドから実PDFを外部のProviderへ送信する。1本ずつ本文・図表を読み、日本語要約と図表説明を生成してObsidianへ保存する。全件を指定していても、内部では小分けに処理し、同時に大量送信はしない。

完了時の`registered`が一覧の登録済み本数。`status: complete`なら対象の処理が完了している。`partial`なら未完了が残っている。

## 5. Obsidianで要約を読む

LLM-Wikiを開き、ファイル一覧の次のフォルダへ進む。

```text
10_Papers
  └ 20_Source_Notes
```

論文タイトルのノートを開くと、要点・図表と説明・結果・限界を読める。ノート内のリンクから保存版PDFを開ける。

Windowsの保存先:

```text
H:\マイドライブ\LLM-Wiki\10_Papers\20_Source_Notes
```

## 6. 消費量を確認する

```powershell
.\.venv-win\Scripts\paperflow.exe --config .\config.production.yaml batch-report --plan .data\batches\import-01.json
```

本数、解析の試行回数、所要時間、取得できたトークン数・費用が表示される。`null`は不明を意味する。

文章形式のレポート:

```text
C:\Users\TAKUH\Documents\paperflow\.data\batches\import-01.report.md
```

## 7. 新着・未処理分を追加する

Paperpileへの追加とGoogle Driveの同期後、新しい一覧を作る。

```powershell
.\.venv-win\Scripts\paperflow.exe --config .\config.production.yaml scan
.\.venv-win\Scripts\paperflow.exe --config .\config.production.yaml batch-plan --all --output .data\batches\import-02.json
```

新しい一覧には、取り込み済みを除いた未処理分が入る。以前から残っている未処理分も対象になる。一覧を確認後、実行する。

```powershell
.\.venv-win\Scripts\paperflow.exe --config .\config.production.yaml batch-run --plan .data\batches\import-02.json --provider claude --no-interactive
```

本数を絞る場合は`--all`を`--count 本数`に変更する。

## 8. 中断・失敗した処理を再開する

止まったときと同じ一覧で、同じ`batch-run`を再実行する。

```powershell
.\.venv-win\Scripts\paperflow.exe --config .\config.production.yaml batch-run --plan .data\batches\import-01.json --provider claude --no-interactive
```

完了済みは飛ばし、残りの同じ対象を処理する。再開中はProvider・モデル・解析設定を変えない。新たに追加された論文を取り込みたい場合は、再開とは別に新しい一覧を作る。

失敗理由は次のコマンドで確認できる。

```powershell
.\.venv-win\Scripts\paperflow.exe --config .\config.production.yaml status
```

## 9. Paperpileで論文を削除した場合

**Obsidianへ保存済みのPDF・図表・要約・統合提案は残る。自動の連動削除はしない。**

PaperpileとGoogle Driveの同期後、次のコマンドで変化を読み込む。

```powershell
.\.venv-win\Scripts\paperflow.exe --config .\config.production.yaml scan
.\.venv-win\Scripts\paperflow.exe --config .\config.production.yaml report --min-priority 1
```

| 同期後の状態 | PaperFlowの動作 | Obsidian側 |
|---|---|---|
| BibTeXから論文が消えた | 新しい取り込み対象から外す。レポートに`missing_from_source`と表示する | 保存済みデータを保持 |
| BibTeXには残り、原本PDFだけが消えた | 新しいバッチでは選ばない。固定済み一覧の未処理分なら送信せず失敗を報告する | 保存済みデータを保持 |
| 削除前に作った一覧の未処理論文がBibTeXから消えた | 実行時に送信せず`missing_from_source`を報告する | 新しく作成しない |
| 削除した論文を復元した | 同じ論文として再検出する。既登録なら新しいバッチから除外する | 保存済みデータを再利用 |

PaperFlowはPaperpileの削除ボタンを直接監視しない。G:へ同期されたBibTeXとPDFで判断する。同期前やBibTeXに書誌が残っている間は、書誌削除としては検知しない。BibTeXの出力対象から外した場合も、削除と同じ`missing_from_source`になる。

Obsidian側のノートやPDFを削除・整理する機能は、この取り込みフローにはない。Paperpileで削除しても、既存Wikiの引用先や統合済み本文を自動で書き換えない。

PaperpileのTrashへの移動と完全削除は別の操作で、Trash内の文献は復元できる。Google Drive同期が有効ならファイル削除も同期される。[Paperpileの削除仕様](https://paperpile.com/h/trash-delete-references/)、[Google Drive同期仕様](https://paperpile.com/h/sync-google-drive/)

## 動作と設定

### 処理範囲

- 対象一覧は作成時点の文献ID・書誌・PDFハッシュで固定する。作成後に変わった入力は送信しない。
- 取り込み済みの判定は、Vaultの凍結出典とSource Noteを参照する。モデルを変えても登録済みを自動で再解析しない。
- PDF未対応・外部送信禁止・同期中・既知のエラー・入力上限超過は対象から外す。
- 保存した一覧を再実行しても、一覧作成後の新着論文を追加で取り込まない。
- 既存のWiki本文への統合は提案まで。`50_Wiki`への追記は別のレビュー工程。

### 設定ファイル

編集する運用設定は`config.production.yaml`。`config.yaml`は元の個人設定、`config.example.yaml`は見本。使用するファイルは`--config`で指定する。

`#`から行末までは説明。項目名と字下げを保って値を編集する。モデルは`select-model`で選べる。設定ファイルの`default_provider`より、コマンドの`--provider`と保存済み選択が優先される。

### PowerShellの記号と変数

本文のコマンドはそのまま入力できる。`PS C:\...>`は画面の入力待ち表示なので入力しない。

別資料の`& $pf`は、`$pf`に入っているプログラムのパスを実行する書き方。`$cfg`は設定ファイルのパスを入れる変数。`& $`という一つの記号ではない。コマンドプロンプト（cmd.exe）では同じ書き方は使えない。

### 入力上限・一時画像

入力上限は300ページ・PDF64MiB・全ページPNG128MiB・補助本文300万文字。モデルやCLI側のコンテキスト制限は別に適用される。100ページを超えるPDFをClaudeへ渡す場合は、対応するコンテキストのモデルを選ぶ。[入力上限](input-limits.md)

全ページPNGと入力PDFの作業コピーは一時フォルダに作り、正常終了・例外・タイムアウト時に削除する。Obsidianへ添える選択済み図表は保存する。OS強制終了・停電は通常の削除処理の対象外。

### 消費量

CLIが返した値だけを記録する。取得できない値は不明。費用表示は定額プランの残り利用率や請求確定額を示すものではない。保存済み解析の再利用は新たな消費に加算しない。

### 詳細資料

- [対象選択・分割実行・再開](batches.md)
- [モデル選択](model-selection.md)
- [PDFと図表の読解](visual-reading.md)
- [保存形式とWiki連携](wiki-integration.md)
- [監視・定期実行](windows-scheduler.md)
- [エラー別の対処](troubleshooting.md)
