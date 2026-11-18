# 依存ライセンスと再現性

依存は `uv.lock` に解決バージョン・配布ハッシュを固定し、Windows 3.12.2と開発環境3.12.11で確認した。更新は `uv lock` の差分と両CLIの合成疎通試験を含めて検証する。

2026-11-16時点のインストール済み配布メタデータ/同梱ライセンスを確認した主要依存：

| 依存 | 固定版 | 表示ライセンス |
|---|---|---|
| bibtexparser | 1.4.3 | LGPLv3 または BSD |
| pyparsing | 3.2.3 | MIT（同梱LICENSE） |
| Click | 8.5.1 | BSD-3-Clause |
| Pydantic | 2.14.3 | MIT |
| PyYAML | 6.0.3 | MIT |
| jsonschema | 4.26.0 | MIT |
| PyMuPDF | 1.28.2 | AGPL-3.0 または Artifex commercial |

PyMuPDFは[公式ライセンス説明](https://pymupdf.readthedocs.io/en/latest/about.html#license-and-copyright)に従う二重ライセンス。公開・配布時には採用するライセンスと同梱条件を別途確認する。今回、外部配布・公開はしていない。PaperFlow独自コードの公開ライセンスは利用者未指定のため設定していない。

bibtexparser 1.4.3の互換性を保つためpyparsingは3.2.3を固定している。PyMuPDF単体のローカル抽出を採用し、OCRやpymupdf4llmは依存に含めていない。開発用のpytest・Ruffは実行時依存から分離した。
