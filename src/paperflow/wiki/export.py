import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

from paperflow.agents.base import AnalysisRequest
from paperflow.agents.schema import validate
from paperflow.config import SettingsError
from paperflow.obsidian.writer import WriteConflict
from paperflow.parsing.pdf_text import extract_pdf
from paperflow.store import Store
from paperflow.wiki.bundle import finalize_bundle, inventory, json_write, sha, target
from paperflow.wiki.render import render, safe_name, stamp


def cached_items(config, paper_ids=None):
    items = []
    with Store(config.storage.database, readonly=True) as store:
        papers = store.rows("SELECT * FROM papers WHERE source_present=1 ORDER BY paper_id")
        if paper_ids:
            if not set(paper_ids) <= {p["paper_id"] for p in papers}:
                raise SettingsError("指定された文献IDがDBにありません")
            papers = [p for p in papers if p["paper_id"] in paper_ids]
        for paper in papers:
            run = (
                store.latest_analysis(paper["paper_id"], paper["source_hash"])
                if paper["source_hash"]
                else None
            )
            if not run:
                if paper_ids:
                    raise SettingsError("指定論文の有効な保存済み解析がありません")
                continue
            match = json.loads(paper["match_json"] or "{}")
            if match.get("status") != "pdf_matched":
                raise SettingsError("確定したPDF対応が必要です")
            rows = store.rows(
                "SELECT sha256 FROM attachments WHERE paper_id=? AND source_path=?",
                (paper["paper_id"], match["path"]),
            )
            expected = rows[0]["sha256"] if rows else None
            if not expected or sha(Path(match["path"])) != expected:
                raise WriteConflict(
                    "保存済み解析時から原本PDFが変更されたかハッシュ記録がありません"
                )
            items.append(
                dict(
                    paper=paper,
                    metadata=json.loads(paper["metadata_json"]),
                    analysis=dict(run, data=json.loads(run["output_json"])),
                    pdf=match["path"],
                    pdf_hash=expected,
                )
            )
    return items


def run_script(root, script, args=(), *, expect_json=False):
    env = os.environ.copy()
    env.update(LLM_WIKI_ROOT=str(root.resolve()), PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")
    proc = subprocess.run(
        [sys.executable, "-B", "-X", "utf8", str(root / "scripts" / script), *args],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    if expect_json:
        try:
            data = json.loads(proc.stdout)
        except ValueError as exc:
            raise SettingsError(f"Wiki検証器の応答を読めません: {script}") from exc
        if proc.returncode not in (0, 1):
            raise SettingsError(f"Wiki検証器が異常終了しました: {script}")
        return data
    if proc.returncode:
        raise SettingsError(f"Wikiスクリプトが失敗しました: {script}: {proc.stderr[-300:]}")
    return proc.stdout


def validate_changed(stage, paths, *, attachment_root=None):
    args = ["--json"]
    for name in paths:
        args += ["--path", name]
    if (stage / "scripts/check_changed.py").is_file():
        if attachment_root is not None:
            args += ["--attachment-root", str(attachment_root)]
        result = run_script(stage, "check_changed.py", args, expect_json=True)
        if result["counts"]["error"]:
            report_path = stage.parent / "validation-error.json"
            json_write(report_path, result)
            errors = [f for f in result["findings"] if f.get("severity") == "error"]
            detail = " / ".join(f"{f['path']}: {f['message']}" for f in errors[:3])
            raise SettingsError(
                f"変更ファイル検証に失敗しました（{result['counts']['error']}件）: "
                f"{detail}。詳細: {report_path}。保存済み解析は保持されています"
            )
        return {"check_changed.py": result}
    reports = {}
    for script in [
        "validate_frontmatter.py",
        "validate_obsidian_markup.py",
        "resolve_links.py",
        "check_graph_integrity.py",
    ]:
        result = run_script(stage, script, args, expect_json=True)
        if result["counts"]["error"]:
            raise SettingsError(
                f"Wikiの変更ファイル検証に失敗しました: {script}。{result['findings'][:3]}"
            )
        reports[script] = result
    return reports


def freeze(stage, item, manifests, new_dirs):
    digest = item["pdf_hash"]
    meta = item["metadata"]
    same = [(p, m) for p, m in manifests if m.get("sha256") == digest]
    different = [
        m
        for p, m in manifests
        if meta.get("doi")
        and str(m.get("doi") or "").lower() == meta["doi"].lower()
        and m.get("sha256") != digest
    ]
    if len(same) > 1:
        raise WriteConflict("同じPDFハッシュの登録が複数あります。先に重複を解決してください")
    if not same and different:
        raise WriteConflict("同じDOIで異なるPDF版が既存登録されています。版の確認が必要です")
    if same:
        directory, manifest = same[0]
        if manifest.get("source_status") != "frozen":
            raise WriteConflict("既存出典は有効な凍結状態ではありません")
        return directory, manifest, False
    slug = "paperflow-" + digest[:20]
    directory = Path("10_Papers/10_Sources") / slug
    dest = target(stage, directory.as_posix())
    if dest.exists():
        raise WriteConflict("凍結出典IDの衝突を検出しました")
    dest.mkdir(parents=True)
    shutil.copyfile(item["pdf"], dest / "source.pdf")
    if sha(dest / "source.pdf") != digest:
        raise WriteConflict("PDFコピー後のハッシュが一致しません")
    extraction = extract_pdf(dest / "source.pdf", 1000000)
    records = []
    markdown = []
    for page in extraction.pages:
        number = page["page_number"]
        markdown.append(f"<!-- page: {number} -->")
        for index, text in enumerate(re.split(r"\n\s*\n", page["text"]), 1):
            if not text.strip():
                continue
            identifier = f"p{number:03d}-b{index:03d}"
            records.append(
                dict(block_id=identifier, page=number, chars=len(text.strip()), text=text.strip())
            )
            markdown.extend([f"<!-- block: {identifier} -->", text.strip(), ""])
    if not records:
        raise ValueError("抽出本文が空です。OCR等の確認が必要です")
    source_id = "paper:" + slug
    payload = dict(
        source_id=source_id,
        sha256=digest,
        extractor="paperflow-pymupdf-v1",
        extracted_at=stamp(),
        page_count=len(extraction.pages),
        block_count=len(records),
        blocks=records,
    )
    (dest / "extracted.md").write_text("\n".join(markdown) + "\n", encoding="utf-8")
    json_write(dest / "extracted.json", payload)
    manifest = dict(
        source_id=source_id,
        source_kind="paper",
        sha256=digest,
        size_bytes=(dest / "source.pdf").stat().st_size,
        original_filename=Path(item["pdf"]).name,
        registered_at=stamp(),
        registered_by="paperflow",
        source_status="frozen",
        doi=meta.get("doi") or "",
        title=meta["title"],
        authors=meta["authors"],
        year=int(meta["year"]) if str(meta["year"]).isdigit() else None,
        venue=meta.get("venue") or "",
        version=None,
        paperflow_id=item["paper"]["paper_id"],
        extraction=dict(
            extractor=payload["extractor"],
            extracted_at=payload["extracted_at"],
            pages=payload["page_count"],
            output="extracted.md",
        ),
    )
    (dest / "manifest.yml").write_text(
        yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    manifests.append((directory, manifest))
    new_dirs.append(directory.as_posix())
    return directory, manifest, True


def front(path):
    raw = path.read_text(encoding="utf-8-sig").split("---", 2)
    return yaml.safe_load(raw[1]) if len(raw) == 3 and not raw[0].strip() else {}


def prepare(config, items, bundle, *, upgrade=False):
    root = config.destination.vault_dir.resolve()
    bundle = bundle.resolve()
    if config.destination.layout != "llm_wiki" or not config.destination.copy_pdf:
        raise SettingsError("正式Wiki出力はlayout: llm_wiki / copy_pdf: trueが必要です")
    if (
        bundle.exists()
        or bundle.is_relative_to(root)
        or bundle.is_relative_to(config.source.paperpile_dir)
    ):
        raise SettingsError("bundleは原本・Vaultの外の新規ディレクトリにしてください")
    if os.name == "nt" and bundle.drive.upper() != "C:":
        raise SettingsError("bundleはC:内に置いてください")
    if not target(root, ".llm-wiki-root").is_file():
        raise SettingsError("Vaultマーカーがありません")
    if target(root, "00_System/Locks/agent-write-lock.yml").exists():
        raise WriteConflict("WikiのWriterが実行中です")
    before = inventory(root)
    stage = bundle / "preview"
    stage.mkdir(parents=True)
    for name in before:
        dest = target(stage, name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(target(root, name), dest)
    if upgrade:
        from paperflow.wiki.framework import upgrade_framework

        upgrade_framework(stage)
    elif not (stage / "00_System/PaperFlow-Integration.md").is_file():
        raise SettingsError("Wiki連携フレームワークが未導入です。先に初回差分を準備してください")
    from paperflow.subject_tags import register_topics

    register_topics(stage, config.taxonomy())
    manifests = []
    for p in (stage / "10_Papers/10_Sources").glob("*/manifest.yml"):
        manifests.append(
            (p.parent.relative_to(stage), yaml.safe_load(p.read_text(encoding="utf-8")))
        )
    note_files = list((stage / "10_Papers/20_Source_Notes").glob("*.md"))
    wiki = [(p.relative_to(stage).as_posix(), front(p)) for p in (stage / "50_Wiki").rglob("*.md")]
    new_dirs = []
    derived_assets = []
    frozen_guards = {}
    if (stage / "00_System/Index/論文データベース.md").is_file():
        from paperflow.wiki.database import copy_database_dependencies

        frozen_guards.update(copy_database_dependencies(root, stage))
    changed_notes = []
    registered = []
    for item in items:
        if sha(Path(item["pdf"])) != item["pdf_hash"]:
            raise WriteConflict("原本PDFのハッシュが一致しません")
        paper = item["paper"]
        run = item["analysis"]
        data = run["data"]
        directory, manifest, is_new = freeze(stage, item, manifests, new_dirs)
        sid = manifest["source_id"]
        slug = sid.split(":", 1)[1]
        # 再利用する出典も実ファイルのハッシュを照合する。既存凍結物は一切変更しない。
        if not is_new:
            for name in ["source.pdf", "extracted.md", "extracted.json"]:
                src = target(root, (directory / name).as_posix())
                if not src.is_file():
                    raise WriteConflict("既存出典の凍結PDF・抽出物が不足しています")
                if name == "source.pdf" and sha(src) != item["pdf_hash"]:
                    raise WriteConflict("既存凍結PDFがmanifestと一致しません")
                frozen_guards[(directory / name).as_posix()] = sha(src)
                shutil.copyfile(src, target(stage, (directory / name).as_posix()))
        payload = json.loads((stage / directory / "extracted.json").read_text(encoding="utf-8"))
        data = validate(
            data,
            AnalysisRequest(
                paper["paper_id"],
                {},
                "",
                data["coverage"],
                list(range(1, payload["page_count"] + 1)),
                config.taxonomy(),
            ),
        )
        item = dict(item, analysis=dict(run, data=data))
        visuals = []
        if is_new and config.destination.include_visuals:
            from paperflow.resources.wiki_extract_visuals import extract_visuals

            output = stage / directory / "figures/paperflow-visuals-v1"
            visual_data = extract_visuals(
                stage / directory / "source.pdf",
                output,
                expected_sha256=item["pdf_hash"],
                source_id=sid,
                maximum=config.destination.visual_max_items,
                selections=data.get("visual_review", {}).get("selected_visuals"),
            )
            visuals = visual_data["items"]
            derived_assets.extend(
                (output / v["file"]).relative_to(stage).as_posix() for v in visuals
            )
            derived_assets.append((output / "visuals.json").relative_to(stage).as_posix())
        if payload.get("sha256") != item["pdf_hash"]:
            raise WriteConflict("既存抽出物がPDFハッシュと一致しません")
        if payload.get("source_id") != sid:
            raise WriteConflict("抽出物の出典IDがmanifestと一致しません")
        existing = [p for p in note_files if front(p).get("source_id") == sid]
        if len(existing) > 1:
            raise WriteConflict("同じ出典のSource Noteが複数あります")
        if existing:
            note_path = existing[0].relative_to(stage).as_posix()
        else:
            title_part = safe_name(item["metadata"]["title"])
            suffix = f" -- {slug}.md"
            parent = stage / "10_Papers/20_Source_Notes"
            # Windowsの長いパス設定に依存しない。原題はfrontmatterに完全保持する。
            budget = 240 - len(str(parent.resolve()).encode("utf-16-le")) // 2 - 1 - len(suffix)
            while title_part and len(title_part.encode("utf-16-le")) // 2 > budget:
                title_part = title_part[:-1]
            if not title_part:
                raise SettingsError(
                    "C:のデータ保存先が長すぎます。storage.data_dirを短いパスにしてください"
                )
            note_path = f"10_Papers/20_Source_Notes/{title_part.rstrip()}" + suffix
        proposal_path = f"70_Review/Merge_Proposals/{slug}.md"
        receipt_path = f"00_System/PaperFlow/receipts/{paper['paper_id']}.json"
        receipt = stage / receipt_path
        old = json.loads(receipt.read_text(encoding="utf-8")) if receipt.exists() else None
        if old and old.get("status") == "integrated" and old["analysis_run_id"] != run["run_id"]:
            raise WriteConflict(
                "統合済み出典の再解析は、引用先の見出し・根拠blockを保持するレビュー差分が必要です"
            )
        if (
            old
            and old["analysis_run_id"] == run["run_id"]
            and all(
                target(stage, n).is_file() and sha(target(stage, n)) == h
                for n, h in old["managed"].items()
            )
        ):
            registered.append(
                dict(paper_id=paper["paper_id"], source_id=sid, note=note_path, status="unchanged")
            )
            continue
        for name, h in (old or {}).get("managed", {}).items():
            if not target(stage, name).is_file() or sha(target(stage, name)) != h:
                raise WriteConflict(
                    "管理ノートに手動編集があります。統合せずローカル差分を保持します"
                )
        candidates = []
        terms = set(data["tags"]) - {"other"}
        title = item["metadata"]["title"].casefold()
        for name, fm in wiki:
            common = terms & set(fm.get("topics") or [])
            exact = title in [
                str(fm.get("title", "")).casefold(),
                *[str(a).casefold() for a in fm.get("aliases") or []],
            ]
            if common or exact:
                candidates.append(dict(path=name, topics=sorted(common) or ["原題・別名の一致"]))
        text, proposal = render(
            item, sid, note_path, proposal_path, payload["blocks"], candidates[:10], visuals
        )
        version = item.get("summary_version")
        if version:
            if version["note_path"] != note_path or version["source_id"] != sid:
                raise WriteConflict("要約履歴の出典・保存先が正式ノートと一致しません")
            text, proposal = version["note_text"], version["proposal_text"]
            for asset in json.loads(version["visuals_json"])["assets"]:
                live = target(root, asset["path"])
                if live.is_file():
                    if sha(live) != asset["sha256"]:
                        raise WriteConflict("要約履歴の図表が変更されています")
                    frozen_guards[asset["path"]] = asset["sha256"]
                else:
                    from paperflow.summary_versions import asset_path

                    source = asset_path(config, version["run_id"], asset)
                    if not source.is_file() or sha(source) != asset["sha256"]:
                        raise WriteConflict(
                            "要約履歴の図表が未取得です。比較画面を更新してください"
                        )
                    destination = target(stage, asset["path"])
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, destination)
                    derived_assets.append(asset["path"])
        managed = {}
        if not existing or (old and note_path in old["managed"]):
            p = target(stage, note_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
            managed[note_path] = sha(p)
            changed_notes.append(note_path)
        p = target(stage, proposal_path)
        if p.exists() and (not old or proposal_path not in old["managed"]):
            raise WriteConflict("管理外の既存統合提案は上書きしません")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(proposal, encoding="utf-8")
        managed[proposal_path] = sha(p)
        changed_notes.append(proposal_path)
        json_write(
            receipt,
            dict(
                paperflow_id=paper["paper_id"],
                source_id=sid,
                pdf_sha256=item["pdf_hash"],
                analysis_run_id=run["run_id"],
                managed=managed,
                status="proposal-pending",
            ),
        )
        run_script(
            stage,
            "append_log.py",
            [
                "--operation",
                "ingest-paper",
                "--subject",
                sid,
                "--detail",
                f"PaperFlowで凍結登録・解析再利用・統合提案を作成。解析ID={run['run_id']}。原典照合とWiki昇格は未実施。",
            ],
        )
        registered.append(
            dict(
                paper_id=paper["paper_id"], source_id=sid, note=note_path, status="proposal-pending"
            )
        )
    if upgrade:
        run_script(
            stage,
            "append_log.py",
            [
                "--operation",
                "schema",
                "--subject",
                "paperflow-integration-v1",
                "--detail",
                "論文の正規入口をPaperFlowへ変更。凍結コピー・Source Note・提案まで自動化。日本語lintと排他を修正。",
            ],
        )
    intake = stage / "00_System/Index/intake-index.md"
    original_intake = intake.read_bytes() if intake.exists() else None
    if changed_notes or upgrade:
        run_script(stage, "update_index.py")
    database = "00_System/Index/論文データベース.md"
    if (stage / database).is_file() and sha(stage / database) != before.get(database):
        changed_notes.append(database)
    # このフローはDropboxを変更しない。疎なpreviewの入庫状況で索引を上書きしない。
    if original_intake is not None:
        intake.write_bytes(original_intake)
    reports = validate_changed(stage, changed_notes, attachment_root=root) if changed_notes else {}
    details = dict(
        new_source_dirs=new_dirs,
        derived_assets=derived_assets,
        frozen_guards=frozen_guards,
        registered=registered,
        validation=reports,
        framework_upgrade=upgrade,
    )
    plan = finalize_bundle(bundle, root, before, details)
    return plan


def prepare_reports(config, store, minimum, bundle):
    from paperflow.reporting.reports import report, review_report

    plan = prepare(config, [], bundle)
    stage = bundle / "preview"
    outputs = {
        "60_Outputs/PaperFlow/読書候補.md": report(store, minimum, config),
        "60_Outputs/PaperFlow/要確認.md": review_report(store),
    }
    for name, text in outputs.items():
        live = target(config.destination.vault_dir, name)
        rows = store.rows("SELECT generated_hash FROM managed_files WHERE path=?", (name,))
        if live.exists() and sha(live) != (rows[0]["generated_hash"] if rows else None):
            if live.read_text(encoding="utf-8") != text:
                raise WriteConflict("既存レポートの手動編集または管理外ファイルを保持します")
        p = target(stage, name)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    plan["validation"].update(
        validate_changed(stage, outputs, attachment_root=config.destination.vault_dir)
    )
    details = {
        k: v
        for k, v in plan.items()
        if k not in {"version", "vault", "created_at", "inventory", "changes"}
    }
    plan = finalize_bundle(bundle, config.destination.vault_dir, plan["inventory"], details)
    return plan, outputs


def record_exports(config, store, plan):
    from paperflow.store import now

    with store.transaction():
        for record in plan["registered"]:
            paper_id = record["paper_id"]
            store.paper(paper_id)
            note = target(config.destination.vault_dir, record["note"])
            receipt = json.loads(
                target(
                    config.destination.vault_dir, f"00_System/PaperFlow/receipts/{paper_id}.json"
                ).read_text(encoding="utf-8")
            )
            store.update(paper_id, state="exported")
            store.db.execute(
                "INSERT OR REPLACE INTO exports VALUES (?,?,?,?,?,?,?)",
                (
                    paper_id,
                    "WikiSource",
                    str(note),
                    sha(note),
                    receipt["analysis_run_id"],
                    now(),
                    record["status"],
                ),
            )
