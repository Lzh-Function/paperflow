# LLM-Wikiとの連携

PaperFlowはPaperpileの論文を凍結登録し、正式Source NoteとWiki統合提案を保存する。

## 保存先

- 原PDF・manifest・ページ付き抽出本文: `10_Papers/10_Sources/<出典ID>/`
- 選択した図表と来歴: 同じ出典の`figures/paperflow-visuals-v1/`
- 日本語Source Note: `10_Papers/20_Source_Notes/`
- Wiki統合提案: `70_Review/Merge_Proposals/`
- 処理記録: `00_System/PaperFlow/receipts/`

## 状態

出典登録と学術的な検証は別に扱う。AI要約は`evidence_status: unverified`、人間レビュー前は`review_status: unreviewed`、Wiki統合前は`integration_status: proposal-pending`を持つ。

論文の細かい主題は`topics`に保持し、Obsidianで検索できるよう`tags`にも`topic/<slug>`を併記する。領域は`area: academic`。タグはVaultの登録語彙を使い、取り込み時に管理taxonomyをタグ辞書へ登録する。未採用の主題語候補は自動登録しない。

`00_System/Index/論文データベース.md` は登録済み論文の発行年・著者・主題・タグを表に集約する。Obsidian標準Basesで検索・絞り込み・並べ替えができ、既存の索引更新に連動する。[使い方と自動更新](paper-database.md)

## 取り込みと統合

本文と視覚入力から要約・図表説明を生成し、変更ノートと参照元を1回検証して保存する。既存Wikiの原題・別名・主題語に基づく関連候補は提案へ記録する。主題語の一致だけで支持・反証などの意味関係を確定しない。

同一PDFは既存出典を再利用する。同一DOIで異なるPDFを登録する場合は版の確認が必要。人間の編集や参照済み見出しを自動で上書きしない。

Paperpileの削除はVaultへ連動させない。登録したPDF・要約・図表と既存Wikiの引用は保持する。

## 資料

- [操作手順](usage.md)
- [保存形式・ロック・復旧](wiki-integration.md)
- [PDFの視覚読解](visual-reading.md)
- [検証](framework-refresh.md)
