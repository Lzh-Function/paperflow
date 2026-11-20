# 論文データベース

## 開く

1. ObsidianでLLM-Wikiを開き、`Ctrl+O` を押す。
2. **論文データベース** と入力して開く。**Source Index** と **Wiki Index** からも移動できる。
3. 表のビューを切り替え、検索・フィルター・並べ替えで論文を探す。論文名を押すと要約ノートが開く。

更新前からPaperFlowを起動していた場合は、実行中の取り込みが終わってからPaperFlowを閉じ、起動し直して次の取り込みを始める。
別PCでも取り込む場合は、そのPCのPaperFlowも同じバージョンへ更新する。Vaultの更新スクリプトとデータベースページはGoogle Driveで同期される。

| ビュー | 内容 |
|---|---|
| すべて | 発行年・著者・主題・レビュー・Wiki統合状態を一覧表示 |
| 発行年別 | 発行年でグループ化 |
| 主題・タグ | 主題・Obsidianタグ・掲載先・DOIを表示 |
| 未レビュー | 未レビュー・レビュー待ちのノートを抽出 |
| Wiki統合状況 | 統合提案あり・Wiki統合済み・状態未記載でグループ化 |

標準の列は参照用の数式で、管理ノートのPropertiesを表から直接編集しない。ビューごとの列・並び順・フィルターは調整でき、索引更新後も保持する。全ビュー共通の登録対象条件は自動生成される。

表が表示されない場合は、Obsidianの **設定 → コアプラグイン → Bases（ベース）** を有効にし、閲覧表示へ切り替える。Basesを表示できない環境では、ページ内の **Markdown一覧を表示** を開く。アプリ設定は利用者が操作する。

## 自動更新

PaperFlowで論文を保存すると、既存の索引更新と一緒に更新される。再解析版の採用・旧版復元でも同じ保存経路を使う。従来の手動取り込みも `scripts/update_index.py` を実行すると反映される。

既存のSource Noteと凍結manifestが対応する論文だけを対象にする。PaperFlow管理ノートと従来の手動取り込みを区別せず集約し、未登録PDF・下書き・C:だけにある試験結果を登録済みとして追加しない。

Basesの列はSource Noteの現在のPropertiesを参照する。タグや年の変更は表示へ追従し、索引更新時には登録対象と折りたたみMarkdown一覧も更新する。PDFの再解析、外部LLM、定期実行の登録は不要。

手動更新は、取り込みを停止した状態でVaultの既存コマンドを使う。

```powershell
python scripts/update_index.py
```

書き換えずに更新の必要性だけを確認する場合：

```powershell
python scripts/update_index.py --check
```

## 保存形式

- ページ：`00_System/Index/論文データベース.md`
- 更新処理：`scripts/paper_database.py`。`scripts/update_index.py` から同じ読取りコンテキストで呼び出す。
- 表示：Obsidian標準Basesのコードブロックと、通常のMarkdown表。
- 自動更新領域：`<!-- BEGIN:paper-database -->` から `<!-- END:paper-database -->`。外側の説明は保持する。

発行年・著者・主題は既存ノートの値を表示し、欠けた値をAIやファイル名から推測しない。レビュー、原典の検証、Wiki統合は別の状態として扱う。表に載ったことをレビュー済み・統合済みとはみなさない。

原PDF・抽出本文・既存Source Note・人間ノート・`.obsidian/**`は変更しない。パスはVault内の相対パスなので、別PCでも同じVault構造で使える。BasesのビューはGoogle Driveが同期するこのMarkdownページに保存される。

公式仕様：[Bases](https://obsidian.md/help/bases)、[Bases構文](https://obsidian.md/help/bases/syntax)。
