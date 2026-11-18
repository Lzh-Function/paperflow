# 実機確認記録

## 照合改善後の追記（2026-11-16）

省略名・Starred/Trashedの重複・版違いへの対応を実装し、実ライブラリでdry-runを検証後、C:のDBにscan結果を反映した。書誌337件のうち332件がPDF照合済み、要確認0件、候補なし5件。以前の294件の対応先に変更なし。原本・BibTeX・本番Vaultは変更していない。PDF本文の読み取りと外部LLM呼び出しも実施していない。

## BibTeX連携後の追記（2026-11-16）

利用者がPaperpileのGoogle Drive自動BibTeX連携を有効化。`G:\マイドライブ\paperpile.bib`（878,133バイト）が現れ、WindowsネイティブPythonで337件を正常に解析した。抄録あり333件、DOIあり200件、明示file参照あり0件。個人用`config.yaml`の`source.bibtex_file`を接続した。`scan --dry-run`は書誌337件、PDF候補341件を検出し、照合結果は安定性待ち294件、要確認37件、PDFなし6件。本番書き込みと外部本文送信の許可は無効のまま。

以下はBibTeX連携前の初回確認記録。

2026-11-16（日本時間）。G/HはWindows PowerShellによるメタデータ参照だけで調査した。開発用シェルはWSLだが、稼働・受け入れ試験にはWindowsのネイティブPythonを使用した。

| 項目 | 確認結果 |
|---|---|
| OS | Windows 11 / build 26200 |
| プロジェクト | `C:\Users\TAKUH\Documents\paperflow` |
| Paperpile | `G:\マイドライブ\Paperpile` が存在 |
| PDF配置 | `All Papers` 335件、`Trashed Papers` 4件、`Starred Papers` 2件、計341パス |
| BibTeX | G:のマイドライブ配下では検出なし。利用者から「エクスポート未設定」と回答あり |
| Vault | `H:\マイドライブ\LLM-Wiki` が存在し、`.llm-wiki-root` を確認 |
| Writerロック | 調査時は `00_System/Locks/agent-write-lock.yml` なし |
| Python | `C:\Users\TAKUH\miniconda3\python.exe` 3.12.2 |
| Windows Claude | `C:\Users\TAKUH\.local\bin\claude.exe` 2.1.219 |
| Windows Codex | npm配下のネイティブ `codex.exe` 0.145.0 |
| Windows uv | プロジェクト専用 `.venv-win\Scripts\uv.exe` を導入 |

`doctor` と `scan --dry-run`、`sync --dry-run` はWindowsから正常終了。実データは341 PDF候補、書誌0件、解析0件、出力0件。原本PDFの内容は読んでいない。ファイルのオフライン取得可否、Drive上のrename動作、実PDFの抽出品質は未検証。

VaultのAGENTS.mdと指定された制御プレーン文書・テンプレートを読み取り、`docs/llm-wiki.md`へ適合方針を記録した。`.obsidian/**`はアプリ管理領域として内容を参照していない。

WindowsのPATHに `python` / `py` / `uv` が見えないため、READMEではフルパスまたはプロジェクト専用uvを使用する。WSL側のCLI（Claude 2.1.325、Codex 0.162.1）はWindows稼働の検証根拠にはしていない。
