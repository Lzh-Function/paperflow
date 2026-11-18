# 論文の根拠確認から既存Wikiへの統合提案

2026-11-17。利用者の「次に確認が必要なタイミングまで進める」という依頼に対し、登録済みBayesian GCN 1件を対象に準備した。出典登録は済んでいるが、既存Wikiの学術本文を変更する承認は別なので、今回はC:の差分と検証まで。本番への統合は未実施。

## 用意した変更

- 新規Concept・Claimを作らず、既存の `50_Wiki/Concepts/分子探索のオラクル信頼性.md` に具体例と限界を追記する。
- Source NoteのF1–F9へ原典のページ・表/図・抽出blockを対応付ける。活性閾値の表記などを原典へ合わせる。
- 出力分布・群別精度・選別成績を確率校正や一般的な適用領域保証と同一視しない。
- 人工ノイズ実験の結果とCEPアーティファクトという著者の解釈を分ける。ESI、独立再現、人間レビューの未実施を保持する。
- 承認後の状態としてSource Note・提案・処理記録をintegratedへ進め、引用・索引・ログを整合させる。ノート全体のevidence_statusはpartial、人間レビュー済みとはしない。

変更は既存9ファイルで、Wiki本文は既存Conceptの1ページだけ。他はSource Note・提案・receipt・索引/MOC・ログ。人間ノート、原本、他の学術ページ、フレームワークを変更しない。

## レビューする場所

```text
C:\Users\TAKUH\Documents\paperflow\.data\wiki-promotion\bayesian-gcn\
  REVIEW.md                   確認事項、9ファイルの一覧とリンク
  DIFF.md                     現在の本番と承認後の全差分
  evidence-review.json        所見別の原典照合記録
  figures\                    原典PDF p.5–8の確認用画像
  bundle\
    plan.json                 operation: promoteの固定反映計画
    preview\                 承認後の9ファイルを含む検証用Vault
    originals\               変更前9ファイル
```

C:プレビューのintegratedは承認後の状態。本番はproposal-pendingのまま。未反映の内容を実際に統合済みと報告しない。

## 検証

本番の添付を読み取り、変更対象のMarkdownだけC:のプレビューに差し替える検証で、frontmatter・Markdown・リンク・グラフの4種すべてエラー0・警告0。H:へ書き込まずに既存添付と新しい引用blockの解決を確認する。

C:リハーサルは9ファイル反映、再反映は0。続けて通常取り込みの差分を準備しても変更0で、レビュー済みの本文・引用blockが汎用生成へ戻らないことを確認した。

作業中の本番Source Noteの表整形について、セル本文と他の本文が同一であることを確認し、最新状態を変更前コピーへ取り込んだ。現在の本番とC:プレビューの差分を提示している。

## 確認後の反映

Wiki統合は通常取り込みと分離し、具体的な差分の承認とplan SHA-256の一致を必須にする。

```powershell
paperflow --config .data/wiki-integration/first-run-config.yaml wiki-apply --bundle .data/wiki-promotion/bayesian-gcn/bundle --approved-plan-sha256 APPROVED_SHA256
```

承認前にこのコマンドを実行しない。H:が準備後に変わった場合は中止し、他者の変更を保持して差分を組み直す。引数のSHA-256は内容確認を代替するものではなく、確認した計画と実行する計画の一致を検査する。

## 取り込み側の改善

保存済み解析の根拠候補と所見が別の文章だった場合、従来の完全一致フィルタでは表にページ情報が出なかった。文章が違う候補も独立した欄に保持し、ページの存在を検証するよう修正した。候補の意味的対応は推測せず未照合と表示する。locatorに長い原文引用が混ざっても、Wikiには位置情報だけを表示する。

すでにWikiへ統合したSource Noteに新しい解析IDが来た場合は、引用される見出し・根拠blockを自動生成で消さず、レビュー差分が必要として停止する。同じ解析ID・生成ハッシュの通常再実行は従来どおり変更0。
