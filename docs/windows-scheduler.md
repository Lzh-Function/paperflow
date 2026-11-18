# Windowsでの手動監視と定期実行

PaperFlowはタスクを自動登録しない。この文書の登録操作は、利用者が定期実行を明示承認し、初回本番syncと復旧試験を終えてから行う。

## 手動監視

PowerShellでプロジェクトへ移動し、承認済み `config.yaml` を使用する。

```powershell
.\.venv-win\Scripts\paperflow.exe --config .\config.yaml watch --interval 300
```

停止はCtrl+C。`watch --once`は一度の動作確認用。別プロセスのPaperFlowはローカルOSロックで排他し、Vault側Writerロックがあれば書かない。他の端末のエージェントを停止し、Drive同期完了を確認する。

## タスクスケジューラ（承認後の手動操作）

1. Windowsの「タスク スケジューラ」から「タスクの作成」を開く。
2. 名前は `PaperFlow Sync`。「ユーザーがログオンしているときのみ実行する」。Driveが見える通常ユーザーで起動し、管理者権限は不要。
3. トリガーを作成し、5分間隔で繰り返す。ログオン後にDriveが利用可能になる時間を考慮する。
4. 操作のプログラムは `C:\Users\TAKUH\Documents\paperflow\.venv-win\Scripts\python.exe`。
5. 引数は `-X utf8 -m paperflow --config "C:\Users\TAKUH\Documents\paperflow\config.yaml" sync --limit 10`。
6. 開始フォルダーは `C:\Users\TAKUH\Documents\paperflow`。
7. 「既に実行中の場合」は「新しいインスタンスを開始しない」。PCのスリープ解除・ログオフ中の実行は初期運用では無効にする。
8. 手動起動して履歴と `paperflow status` を確認する。長時間実行を強制終了する場合も、DBと生成ファイルは再実行で復旧できるが、終了直前のCLI課金が重複し得る。

有限の `sync` を繰り返す方式では一時障害は `retry --failed` で再投入する。`watch` は待機期限を過ぎた再試行も行う。どちらも回数上限がある。

無効化はタスクを右クリック→「無効」。不要になったら「削除」。タスクを消しても原本・ノート・ローカルDBは消えない。PaperFlowに登録・削除コマンドはない。
