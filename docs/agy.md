# Antigravity CLI（agy）を使う

## GUIで設定する

1. PaperFlowを再起動する。
2. **PC間同期 → agyにログイン** を押す。すでにログイン済みなら省略できる。
3. **取り込み** のAIを **Antigravity CLI（agy）** にする。
4. **モデル一覧を更新** を押し、PDF読解に使うモデルを選ぶ。
5. **設定を保存** を押す。通常どおり対象一覧を作り、**要約して保存** を押す。

ログイン画面は公式の `agy acp --login` を起動する。PaperFlowは認証情報を取得・保存しない。PDF外部送信・Vault保存の許可は、従来どおり設定画面で確認する。

Windowsの標準導入先 `AppData\Local\agy\bin\agy.exe` は自動検出する。見つからない場合は、**設定 → AI・読解 → Antigravity CLI（agy）** に実行ファイルのフルパスを保存する。`analysis.agy_command` にも指定できる。

登録済み論文は、**論文一覧 → 選択論文を再要約・版を比較** からagyを選べる。選択した論文だけを再要約し、候補を比較して正式版を選ぶ。[再要約と版管理](summary-versions.md)

## PDFの渡し方

| Provider | 入力 |
|---|---|
| Claude Code CLI | PDFをdocumentブロックとして直接添付 |
| Antigravity CLI（agy） | C:のPDFコピーをread_fileで直接開いて読解 |
| Codex CLI | 全ページを画像化し、順番に添付 |

agyへ渡すPDFは原PDFと同じ内容で、SHA-256とページ数を照合する。Python側でPNGへ変換したり、PDFの画像を圧縮したりしない。原本のG:・VaultのH:へはアクセスさせず、C:の作業用PDF全体だけを読み取らせる。抽出全文を別のテキストとして重ねて送信しない。

agyの通常のstream-json入力はテキスト専用。ACPの埋め込みresourceでも、確認した版ではPDFが破棄された。この経路は使わず、公式ACPから作業用PDFのread_fileを許可する。PDFを開いたツールの完了記録がなく、回答だけを返した場合も不採用にする。シェル・ファイル書き込み・Web・MCP・追加権限・他ファイルの読み取り要求は停止する。

このPCのCodex CLIは、execの添付オプションとapp-serverの入力Schemaの双方にPDF入力がないため、全ページ画像を使用する。PDF対応のない経路で文字のみの要約へ切り替えることはしない。

## 保存・消費量・制約

日本語の指示・分類・図表選択・共通JSON Schema検証・Obsidian出力・登録済み除外・選択論文の再要約・PC間同期は、他のProviderと共通。明示したモデルが利用可能一覧にない場合は送信前に停止し、別モデルへ自動で振り替えない。

要約JSON内の明確なエスケープ不備は、文字・記号を保持して共通処理で正規化し、未確認事項へ記録する。全検証は省略しない。不採用回答の本文・構文エラーの位置・入力証跡はC:の `.data/diagnostics/` に保持し、CLIの一時作業ログとは分けて扱う。[JSONと特殊記号](troubleshooting.md#jsonと特殊記号)

モデル一覧は `agy --output-format json models` から取得する。モデル推論は行わない。候補はC:へキャッシュし、Providerごとの選択を記憶する。

入力・出力・キャッシュ利用は、ACPが申告したトークン数を保存する。入力は非キャッシュ入力とキャッシュ入力を合わせた値。費用の申告がなければ不明と表示し、推計価格を加算しない。

`max_budget_usd` はClaude用で、agyには適用しない。選択した本数・入力上限・待ち時間は制御するが、金額の上限を保証する機能ではない。

PaperFlowの作業用PDF・ログは、正常終了・例外・タイムアウト時に削除する。全ページPNGのキャッシュは作らない。成果物として選択した図表は保持する。OS強制終了時は通常の後始末を保証しない。agy自身の会話履歴・認証・キャッシュはCLIの管理対象で、PaperFlowから削除しない。

ページ数・元PDF容量・待ち時間は既存の設定を使う。モデルやagy側にも別の入力・コンテキスト制限がある。今回の実通信確認は合成2ページPDFであり、長大な実論文の成功率まで確認したものではない。

## コマンドで設定する

プロジェクトフォルダのPowerShellで実行する。

```powershell
.\.venv-win\Scripts\paperflow.exe models --provider agy
.\.venv-win\Scripts\paperflow.exe select-model --provider agy
```

実PDF・Vaultを使わず、合成PDFの接続試験を行う場合：`MODEL_ID` を一覧にあるモデルIDに置き換える。

```powershell
.\.venv-win\Scripts\python.exe scripts\smoke_providers.py --provider agy --model MODEL_ID --visual
```

## 確認記録

2026-11-20、Windowsネイティブagy 1.5.13で確認した。モデル一覧23候補を取得。製品Providerで、色を抽出本文に記載しない合成2ページPDFを読み、1ページの赤・2ページの青、図表2点の選択・説明・全ページ完了・共通Schema/意味検証に成功した。解析約16.77秒、CLI申告入力30,149・出力1,043トークン。実PDFの追加送信・本番Vault変更は行っていない。証跡は `.data/agy-development/visual-provider.json`。

公式資料：[ACP](https://antigravity.google/docs/cli/acp/)、[Headless入力](https://antigravity.google/docs/cli/headless/)、[権限](https://antigravity.google/docs/permissions/)。実装は各PCのCLIヘルプと応答形式を検査する。
