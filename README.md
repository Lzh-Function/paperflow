# PaperFlow

PaperpileのPDF・BibTeXから、図表を含む日本語要約を作り、ObsidianのLLM-Wikiへ保存するWindowsアプリ。

**[操作マニュアル](docs/usage.md)**

## 初回セットアップ

1. ソース一式をC:内のフォルダへ置く。
2. **setup.cmd** をダブルクリックする。Python・固定依存ライブラリ・設定ファイル・起動アイコンが用意され、GUIが開く。
3. 開いた画面でPaperpile・BibTeX・Obsidian Vaultを選び、**このPCのパスを保存**。
4. 使用するClaude Code CLI / Codex CLI / Antigravity CLI（agy）を導入し、GUIの各ログインボタンで認証する。
5. 「取り込み」でAI・モデルを選び、対象と保存先を確認して送信・保存を許可する。

初回はインターネット接続が必要。既存の個人設定は上書きせず、PDF送信やVault書き込みを自動開始しない。更新後も `setup.cmd` を再実行できる。[導入手順とオプション](docs/setup.md)

## 普段の使い方

デスクトップの **PaperFlow**、またはフォルダ内の **PaperFlow.cmd** をダブルクリックして起動する。

1. AIとモデルを選び、送信・保存のチェックを入れて **設定を保存**。
2. **未取り込みを全件／追加する本数／累計の目標** を選び、**対象一覧を作成**。
3. 論文一覧を確認して **この○本を要約して保存**。
4. **要約フォルダを開く** で出力を確認。「履歴・消費量」で使用量を確認する。

「論文一覧」では取り込み済み・未処理・失敗を検索・絞り込みでき、PDFや保存済みノートを開ける。実行中も閲覧できる。

停止後は同じ一覧から再開できる。コマンド操作は [CLI操作マニュアル](docs/cli-usage.md) を参照。

自宅PCとラボPCで続きから使う場合は、**PC間同期** タブでDrive内の共有フォルダを選ぶ。Google Cloud・OAuth設定は不要。PC切り替え時にDriveの同期完了を確認する。[PC間同期の初期設定](docs/multi-pc-sync.md)

## 動作

Claude Code CLIにはPDFを直接添付し、Antigravity CLI（agy）には作業用PDFを直接読ませる。Codex CLIには全ページ画像を添付する。同じ要約担当が本文・図表を読み、重要な図表を選んで日本語で説明する。FakeProviderでは外部通信なしで動作確認できる。

出力はPDFの凍結コピー、図表、日本語Source Note、Wiki統合提案、索引とログ。G:のPaperpile原本・既存の学術本文・人間ノート・`.obsidian/**`を変更しない。Paperpileからの削除をVaultへ連動させる機能はない。

## 設定

運用設定はセットアップで作られる `config.production.yaml`、見本は[config.example.yaml](config.example.yaml)。各項目に日本語コメントを記載している。設定の外部送信・Vault書き込み許可と、コマンドで選ぶProvider・モデルは独立している。

## 資料

- [対象選択とバッチ実行](docs/batches.md)
- [モデル選択](docs/model-selection.md)
- [視覚読解](docs/visual-reading.md)
- [入力上限](docs/input-limits.md)
- [Wiki連携](docs/wiki-integration.md)
- [監視・定期実行](docs/windows-scheduler.md)
- [トラブル対応](docs/troubleshooting.md)

## 開発

WindowsネイティブPython 3.12以降を使用する。WSL・Dockerは実行時に不要。

```powershell
.\setup.cmd -Dev -NoLaunch
.\.venv-win\Scripts\python.exe -B -X utf8 -m pytest -q
```

- [設計仕様](docs/DESIGN.md)
- [検証記録](docs/verification.md)
- [依存ライセンス](docs/licenses.md)

`.data/`、個人設定、PDF本文、生ログはGit対象外。LLMの認証は各公式CLIを使用する。PC間同期はGoogle Driveデスクトップ版のファイル同期を利用し、PaperFlowはGoogle認証情報を取得・保存しない。
