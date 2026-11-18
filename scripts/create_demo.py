"""合成資料だけで再現可能なデモを作る。既存出力は上書きしない。"""

import argparse
from pathlib import Path

import pymupdf
import yaml


def make_demo(root: Path, count=10):
    root.mkdir(parents=True, exist_ok=False)
    source, vault = root / "大学 Paperpile", root / "個人 Vault"
    source.mkdir()
    vault.mkdir()
    (vault / ".llm-wiki-root").write_text("synthetic-fixture\n", encoding="utf-8")
    (root / "taxonomy.yaml").write_text("topics: [smiles, benchmark, other]\n", encoding="utf-8")
    entries = []
    for i in range(count):
        title = f"Synthetic molecular benchmark experiment {i:02d}"
        path = source / f"Fixture_Yamada_2026_{title}.pdf"
        doc = pymupdf.open()
        page = doc.new_page()
        text = (
            f"Synthetic benchmark {i}. This document is generated for offline software testing. "
            "Methods compare example values without making real scientific claims. "
            "Results are placeholders. No personal or licensed paper content is present."
        )
        page.insert_textbox(pymupdf.Rect(40, 40, 550, 700), text, fontsize=11)
        doc.save(path)
        doc.close()
        entries.append(
            f"@article{{Fixture{i},title={{{title}}},author={{Yamada, Taro}},"
            f"year={{2026}},doi={{10.9999/fixture{i}}},abstract={{{text}}}}}"
        )
    (root / "library.bib").write_text("\n".join(entries), encoding="utf-8")
    config = dict(
        source=dict(paperpile_dir=str(source.resolve()), bibtex_file="library.bib"),
        destination=dict(vault_dir=str(vault.resolve()), allow_vault_write=True),
        storage=dict(data_dir="cache", database="cache/state.db"),
        analysis=dict(default_provider="fake", taxonomy_file="taxonomy.yaml", retry_base_seconds=0),
        sync=dict(stable_scan_count=2),
    )
    path = root / "config.yaml"
    path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(".data/demo"))
    args = parser.parse_args()
    print(make_demo(args.output))
