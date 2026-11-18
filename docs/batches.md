# 全件・本数指定の取り込み

[操作手順](usage.md)

本数は正の整数で指定する。

| 指定 | 動作 |
|---|---|
| `batch-plan --all` | 処理可能な未登録論文を全件選択 |
| `batch-plan --count N` | 未登録の論文から、追加N本を乱択 |
| `batch-plan --total N` | 登録済みを含む累計N本になるよう、不足分を乱択 |

計画ごとに追加本数・累計目標を指定する。同じ論文は取り込み済みとして除外する。モデル・Providerを変えても、すでにVaultへ入った論文をこの機能が自動で再解析することはない。

## 前提

[使い方マニュアル](usage.md) の運用設定を使う。実PDF送信の対象範囲・Providerの承認と、全文・視覚読解・Vault書き込みの有効化は別途必要。計画を作る操作だけではAI解析・Vault書き込みは始まらない。

## 全件を選択する

```powershell
.\.venv-win\Scripts\paperflow.exe --config .\config.production.yaml scan
.\.venv-win\Scripts\paperflow.exe --config .\config.production.yaml batch-plan --all --output .data\batches\all-01.json
```

処理可能な未登録論文をすべて一覧へ固定する。既登録・PDF未対応・同期中・外部送信禁止・既知のエラー等は除外する。実行・再開・消費量確認は本数指定と共通。

## 追加する本数を指定する

PowerShellで実行する。

`& $pf`の意味は [コマンド表記の説明](usage.md#powershellの記号と変数) を参照。`$pf`・`$cfg`等は同じPowerShell画面内で使う変数。下の`Read-Host`は本数を聞いて入力を受け取り、`[int]`で整数にする。設定ファイルを編集する記号ではない。

```powershell
Set-Location 'C:\Users\TAKUH\Documents\paperflow'
$pf = Join-Path $PWD '.venv-win\Scripts\paperflow.exe'
$cfg = Join-Path $PWD 'config.production.yaml'  # 承認・設定済みの運用ファイル
$n = [int](Read-Host '今回、追加したい本数')

# 書誌とPDFの観測を更新。AI・Vault出力なし
& $pf --config $cfg scan

# まず表示だけ。DB・ディスク・外部CLIへの副作用なし
& $pf --config $cfg batch-plan --count $n --dry-run

# 対象を乱択してC:へ計画を保存。論文の解析はしない
& $pf --config $cfg batch-plan --count $n
```

結果の `plan` が計画JSONのフルパス。`items` に今回のタイトル・文献ID・PDFハッシュを記録する。以降はそのファイルを使う。dry-runと保存操作は別の抽選なので同じ対象にしたい場合は、dry-runの出力にある `seed` を保存操作の `--seed` に指定する。DB・登録状況が変われば同じseedでも候補は変わり得る。**保存後は対象を抽選し直さない。**

```powershell
$plan = Read-Host '表示されたplanのフルパス'

# 計画JSONのitemsを確認。読み取りだけ
(Get-Content -LiteralPath $plan -Raw -Encoding UTF8 | ConvertFrom-Json).items |
    Select-Object paper_id, title, pages

# Provider・モデルを選択する。解析は始めない
& $pf --config $cfg select-model

# 取り込み済みを除いた、この計画の残り対象を表示するだけ
& $pf --config $cfg batch-run --plan $plan --dry-run --no-interactive

# 確認・承認済みの対象と設定で実際に要約・保存
& $pf --config $cfg batch-run --plan $plan --no-interactive

# このバッチの本数・消費量を見る。読み取り専用
& $pf --config $cfg batch-report --plan $plan
```

既存の `sync --limit` と異なり、計画にない論文は処理しない。内部では `sync.max_papers_per_run` と `analysis.max_cli_calls_per_run` の小さい値ずつ分割し、計画全体を順に処理する。計画の本数がこの設定値より大きくても、1回のbatch-runで続行する。AIの同時実行は1件ずつ。

失敗しても勝手に別の論文を補充しない。そのため指定N本すべてが成功するとは限らない。候補自体がN本未満なら `shortage` で不足数を報告する。

## 累計本数を指定する

「さらにN本」ではなく「累計N本にしたい」場合は、計画作成を次に置き換える。

```powershell
$targetTotal = [int](Read-Host '登録済みも含めた累計の目標本数')
& $pf --config $cfg batch-plan --total $targetTotal
```

累計は、現在のVaultに凍結出典とSource Noteの両方がある論文をPDFハッシュで数える。今回のバッチ以外・以前の手動登録分も含む。すでに目標へ達していれば追加0本。計画後に別バッチで登録が進んだ場合も、実行時に残り必要本数を計算し直す。ただし計画外の論文を新たに選び足すことはない。

## 次の規模へ進む

消費量と成果物を確認後、別の好きな本数で `batch-plan --count` または `--total` を実行し、新しい計画を作る。その時点の取り込み済みを自動で除外する。

除外はDBの処理状態だけに依存せず、Vaultの出典manifestとSource Noteを確認する。PDFハッシュ、DOI、書誌同一性を使うため、別のDBや手動で登録した論文も除外する。同じPDFが複数の書誌に対応する場合も、バッチ内では1件だけ選ぶ。原本・過去の全PDFを毎回コピー・再ハッシュする運用にはしない。

同期中・PDF未対応・外部送信禁止・既知のエラー・暗号化PDF・視覚入力上限超過などは候補から外し、除外理由を計画に記録する。Google Driveの同期が完了してから使う。

## 消費量のレポート

計画JSONの隣に `.progress.json` と `.report.md` を保存する。レポートはC:にあり、Vault本文やWikiを更新するものではない。

- 今回の対象本数・登録済み本数・残り本数。
- このバッチで新たに発生した解析の試行回数・所要時間（CLI起動前の失敗も試行に含む）。
- CLIが返した入力・出力・キャッシュトークン数。
- CLIが返した場合だけUSD費用。ClaudeはCLI表示の推定費用。Codex等で取得できない場合は「不明」。
- 各解析のProvider・モデル・状態と消費量。

保存済み解析の再利用は、新規の消費として二重計上しない。複数回再開した場合も、同じrun IDを重複加算しない。値が欠けている呼出しの本数は `unreported_calls` に示す。取得分だけの合計なので、未知の失敗・中断コストまで含む請求確定額とは扱わない。

CLIのトークン数から、Claude/Codexの定額プランの残り利用率や請求額を逆算する機能ではない。利用枠は各Providerの表示も併せて確認する。

## 中断・失敗・再開

同じ計画へ、同じProvider・モデル・解析設定で `batch-run` を再実行する。すでにVaultへ保存した論文はスキップし、残りの同じ対象だけを処理する。解析済みでVault保存だけ失敗した場合は、条件が変わっていなければ保存済み解析を再利用する。

計画作成後にPDF・書誌が変わった場合は、その論文を送らず停止する。差分を確認して新しい計画を作る。再開時にProvider・モデル・解析設定を変えることも拒否し、意図しない再課金を防ぐ。

期限・認証・ノートの競合などは原因を直してから再開する。強制終了の直前に送信済みだった呼出しは、結果を取得できず再呼出しが必要になることがある。原本・既存人間ノート・.obsidianは変更しない。定期実行は登録しない。

## 検証範囲

任意本数の選択、乱択のseed、累計目標、既存Vaultの登録除外、分割実行、入力変更時の送信防止、失敗後の再開、消費量記録、v1 DBからの移行、dry-runの無副作用を合成fixtureで確認する。実論文の追加外部送信は実施していない。


消費量取得の参照: [Codex CLIのJSONLイベントとusage](https://learn.chatgpt.com/docs/non-interactive-mode)。料金表を使った独自の費用推定は行わない。
