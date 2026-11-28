# Windowsへのセットアップ

## 最初に行うこと

1. Gitで取得したフォルダ、または展開したソース一式をC:内へ置く。例：`C:\Apps\PaperFlow`。
2. フォルダ内の **setup.cmd** をダブルクリックする。
3. 準備が終わるとGUIが開く。「PC間同期」の **初期設定・保存場所の変更を表示 → 1. このPCの保存場所を設定** で、Paperpile・BibTeX・Obsidian Vaultを選び、**このPCのパスを保存** を押す。
4. 1台で使う場合は「取り込み」へ進む。2台で続きを共有する場合は [共有フォルダの設定](multi-pc-sync.md) を行う。

セットアップはuv 0.13.8を必要に応じて公式配布から取得し、SHA-256を検証する。Python 3.12と依存ライブラリは自動で準備する。管理者権限やシステムPATHの変更は不要。初回はインターネット接続が必要。

既存の `config.production.yaml` は保持する。新規作成時はコメント付き見本をコピーし、PDF外部送信・Vault保存は無効のまま。DBの初期化、実論文の解析、Vault移行、定期実行の登録は行わない。

## AIと保存先を設定する

1. 使用するCLIをWindowsへ導入する。[Claude Codeの公式導入案内](https://code.claude.com/docs/en/setup)、[Codex CLIの公式導入案内](https://developers.openai.com/codex/cli)。[Antigravity CLI（agy）](https://antigravity.google/docs/quickstart)も選択できる。使うCLIだけでよい。
2. 必要なら「設定」→「AI・読解」のClaude CLI / Codex CLI / Antigravity CLI（agy）欄へ、このPCの実行ファイルを指定して保存する。
3. 「PC間同期」の **Claudeにログイン** / **Codexにログイン** / **agyにログイン** を押して認証する。
4. 「取り込み」でAI・モデルを選び、入力元と保存先を確認する。
5. PDF外部送信・Obsidian保存のチェックを入れ、**設定を保存** を押す。解析は実行ボタンを押すまで始まらない。

LLM-Wiki形式の出力には、対応フレームワークを含む既存Vaultが必要。セットアップは一般のVaultへフレームワークを自動導入しない。[Wiki連携](llm-wiki.md)

## 起動と更新

通常はデスクトップの **PaperFlow**、または **PaperFlow.cmd** を開く。

ソースを更新するときは、処理を停止してPaperFlowを閉じ、Gitの更新後に **setup.cmd** を再実行する。既存設定・DB・解析結果は保持され、起動アイコンを更新する。

## PowerShellから実行する場合

プロジェクトフォルダでPowerShellを開いて実行する。

```powershell
.\setup.cmd
```

GUIを開かず準備だけ行う：

```powershell
.\setup.cmd -NoLaunch
```

デスクトップへアイコンを置かない：

```powershell
.\setup.cmd -NoDesktopShortcut
```

開発用のpytest・Ruffも入れる：

```powershell
.\setup.cmd -Dev -NoLaunch
.\.venv-win\Scripts\python.exe -B -X utf8 -m pytest -q
```

既存のWindows Pythonを指定する：

```powershell
.\setup.cmd -Python 'C:\Path\To\python.exe'
```

Python 3.12以降とTcl/Tkが必要。コマンド欄に表示されるパスは、利用するPCの保存先へ置き換える。

## 保存場所

| 場所 | 内容 | Git管理 |
|---|---|---|
| `setup.cmd` / `PaperFlow.cmd` / `scripts/` | セットアップ・起動処理 | 対象 |
| `uv.lock` | 依存ライブラリの版と配布ハッシュ | 対象 |
| `config.example.yaml` | コメント付き設定の見本 | 対象 |
| `.tools/` | セットアップ用uv・Python・ダウンロードキャッシュ | 対象外 |
| `.venv-win/` | このPCのPython実行環境 | 対象外 |
| `config.production.yaml` / `config.yaml` | PC固有の設定 | 対象外 |
| `.data/` | DB・解析結果・実行履歴 | 対象外 |
| `*.lnk` | このPCのパスを持つショートカット | 対象外 |

別PCへ `.venv-win` や `.data` をコピーせず、そのPCでも **setup.cmd** を実行する。進捗は専用共有フォルダから引き継ぐ。

## 失敗した場合

コンソールのエラーを確認する。ネットワーク・プロキシ・ファイル権限を直した後、同じ **setup.cmd** を再実行する。SHA-256不一致時は取得したuvを実行しない。既存設定が不正な場合は上書きせず停止する。

Tkのエラーが出る場合はTcl/Tkを含むWindows版Pythonを導入し、`-Python` で指定する。C:以外、Paperpile原本、Vault、`.obsidian`の中へアプリを設置しない。
