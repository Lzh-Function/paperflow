# Providerとモデルの対話選択

PowerShellでプロジェクトのフォルダに移動し、次のコマンドを実行する。`$uv` はセットアップ済みのWindows版uv（例: `.venv-win\Scripts\uv.exe`）。

```powershell
& $uv run paperflow select-model
```

1. Claude Code CLI / Codex CLI / Fakeから番号で選ぶ。
2. 選んだ公式CLIから返されたモデル候補を番号で選ぶ。モデルIDの手入力もできる。
3. ProviderごとのモデルIDと最後に選んだProviderをC:のローカル設定に記憶する。

**このコマンドは選択と保存だけを行う。論文の取り込み・解析・ノート出力は始めない。** CLIの認証は既存のログインを使い、PaperFlowでは認証情報を収集・保存しない。

## 解析時に選ぶ

通常の対話端末では、`sync`、`analyze`、`retry`、`watch` にも選択画面を自動表示する。Providerを指定するとモデル選択から始まる。`watch` は起動時に1回だけ選ぶ。

```powershell
# 候補を選びながら処理計画を確認。解析や出力は行わない
& $uv run paperflow sync --dry-run --interactive

# 公式CLIからのモデル一覧取得・更新だけを行う
& $uv run paperflow models --provider claude
& $uv run paperflow models --provider codex

# 保存済み一覧を読み取り専用で表示
& $uv run paperflow models --provider codex --cached
```

`dry-run` 中はモデル照会のCLI起動も行わない。保存済み候補か手入力を使い、選択内容・候補一覧の保存、DB更新、解析、ノート出力は行わない。先に `models` を実行すると対話式dry-runで候補を選べる。`select-model --dry-run` も保存しない。

モデルの選択は、本文の外部送信許可やVaultへの書き込み許可を変更しない。実行する範囲は対象一覧で固定する。[操作手順](usage.md)を参照。

## 選択の記憶とコマンド指定

選択は `storage.data_dir/model-preferences.json` に保存する。`config.yaml` は書き換えない。候補一覧は同じディレクトリの `model-catalog-claude.json` / `model-catalog-codex.json` に保存し、取得日時を保持する。CLI初期化応答のアカウント情報は保存しない。

Providerの優先順位は `--provider` → 最後に選んだProvider → `analysis.default_provider`。
モデルの優先順位は `--model` → 今回の対話選択 → そのProviderの保存済みモデル → `analysis.model` → CLIの既定値。
`analysis.model` は `analysis.default_provider` と同じProviderにだけ適用し、Claude用モデルをCodexへ流用しない。

`--model ID` は今回だけの指定で、モデル選択と記憶を省略する。自動処理ではProvider・モデルを明示すると再現しやすい。次の `MODEL_ID` はモデル一覧に表示された値に置き換える。

```powershell
# 入力待ちなしで、指定したProviderとモデルの計画を確認
& $uv run paperflow --json sync --dry-run --provider codex --model MODEL_ID --no-interactive

# 記憶した選択を使い、対話を省略して計画を確認
& $uv run paperflow sync --dry-run --no-interactive

# オフライン検証を明示
& $uv run paperflow sync --dry-run --provider fake --no-interactive
```

標準入出力が端末でない場合や `--json` では、自動で対話を開始しない。パイプ経由でも選びたい場合は `--interactive` を指定する。`--json` と `--interactive` の併用はエラー。`select-model` は常に対話専用で `--json` 非対応。

実行結果の `model_selection` にProvider、指定モデルID、指定の有無を出力する。`model: null` / `model_source: cli_default` はCLIに既定モデルの決定を任せた状態。指定済みは `model_source: pinned`。モデル指定を変更すると解析キャッシュの判定も変わり、再解析が必要になる場合がある。

## 候補の取得方法と取得失敗時

モデルIDの一覧はハードコードしない。ClaudeはCLIの制御プロトコルの初期化応答、Codexはapp-serverの初期化後の `model/list` を使う。Claudeの別名は応答の具体的なモデルIDへ解決し、同一モデルを重複表示しない。Codexは非表示モデルを除き、ページをたどって候補を取得する。

一覧取得ではユーザープロンプト、論文、`thread/start`、`turn/start` を送らず、推論を開始しない。CLIがモデル情報の更新・認証確認で通信する場合はある。返された候補はCLI側の案内であり、各モデルでの実際の推論成功や利用枠を保証するものではない。

照会に失敗すると取得日時付きの保存済み候補を表示する。保存済み候補もなければモデルIDを手入力する。前回選んだIDが現在の候補にない場合は「利用可否未確認」と表示する。別Providerへの自動切替は行わない。Ctrl+Cで中断すると解析を開始せず、未確定の選択も保存しない。

CLI制御プロトコルはバージョンによって変わり得る。照会は20秒・出力量・ページ数の上限を持ち、終了時に照会用プロセスを停止する。互換性エラー時はCLIを更新して一覧を再取得するか、モデルIDを手入力する。

方式の参照元: [Codex app-serverのmodel/listと初期化](https://learn.chatgpt.com/docs/app-server)、[Claude Agent SDKのsupportedModels](https://code.claude.com/docs/en/agent-sdk/typescript)。実装はWindowsに導入済みのCLIで確認した。
