"""登録済み論文の再要約、本文・図表の版管理、明示的な正式版切り替え。"""

import html
import json
import re
import shutil
import tempfile
import uuid
from pathlib import Path

from paperflow.config import SettingsError
from paperflow.models import digest
from paperflow.obsidian.writer import WriteConflict, local_lock
from paperflow.pipeline import Pipeline
from paperflow.store import Store
from paperflow.wiki.bundle import apply, sha, target
from paperflow.wiki.export import prepare, record_exports
from paperflow.wiki.render import render


def context(cfg, store, paper_id):
    paper = store.paper(paper_id)
    if cfg.destination.layout != "llm_wiki":
        raise SettingsError("要約の版管理はLLM-Wiki形式で利用できます")
    root = cfg.destination.vault_dir
    receipt = target(root, f"00_System/PaperFlow/receipts/{paper_id}.json")
    if not receipt.is_file():
        raise SettingsError("PaperFlowで正式登録した論文を選んでください")
    record = json.loads(receipt.read_text(encoding="utf-8"))
    sid = record["source_id"]
    if not re.fullmatch(r"paper:paperflow-[a-f0-9]{20}", sid):
        raise SettingsError("対応していない出典IDです")
    notes = [p for p in record["managed"] if p.startswith("10_Papers/20_Source_Notes/")]
    if len(notes) != 1:
        raise WriteConflict("PaperFlow管理の正式Source Noteを特定できません")
    note_path = notes[0]
    source_dir = "10_Papers/10_Sources/" + sid.split(":", 1)[1]
    pdf = target(root, source_dir + "/source.pdf")
    if not pdf.is_file() or sha(pdf) != record["pdf_sha256"]:
        raise WriteConflict("凍結PDFが未取得または変更されています。Driveの同期を確認してください")
    runs = store.rows(
        "SELECT * FROM analysis_runs WHERE run_id=? AND paper_id=? AND status='success'",
        (record["analysis_run_id"], paper_id),
    )
    if not runs:
        raise SettingsError("正式版の解析履歴がありません。PC間同期またはDBの復元が必要です")
    payload = json.loads(target(root, source_dir + "/extracted.json").read_text(encoding="utf-8"))
    if payload["sha256"] != record["pdf_sha256"] or payload["source_id"] != sid:
        raise WriteConflict("凍結出典の抽出物が一致しません")
    return dict(
        paper=paper,
        receipt=record,
        note_path=note_path,
        source_dir=source_dir,
        source_id=sid,
        pdf=pdf,
        pdf_hash=record["pdf_sha256"],
        run=runs[0],
        payload=payload,
        metadata=json.loads(paper["metadata_json"]),
    )


def _save(store, row):
    columns = list(row)
    with store.transaction():
        store.db.execute(
            f"INSERT INTO summary_versions ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)}) "
            + "ON CONFLICT(run_id) DO UPDATE SET "
            + ",".join(f"{c}=excluded.{c}" for c in columns if c != "run_id")
            + " WHERE summary_versions.note_text=''",
            tuple(row[c] for c in columns),
        )


def _base_row(ctx, run, note, proposal, visuals):
    metadata = dict(ctx["metadata"], file="")
    return dict(
        run_id=run["run_id"],
        paper_id=ctx["paper"]["paper_id"],
        pdf_sha256=ctx["pdf_hash"],
        source_id=ctx["source_id"],
        note_path=ctx["note_path"],
        note_text=note,
        proposal_text=proposal,
        metadata_json=json.dumps(metadata, ensure_ascii=False),
        visuals_json=json.dumps(visuals, ensure_ascii=False),
        created_at=run["started_at"],
    )


def _archive_current(cfg, store, ctx, *, persist=True):
    run = ctx["run"]
    existing = store.rows("SELECT * FROM summary_versions WHERE run_id=?", (run["run_id"],))
    if existing:
        return existing[0]
    root = cfg.destination.vault_dir
    note = target(root, ctx["note_path"]).read_text(encoding="utf-8")
    proposal_path = "70_Review/Merge_Proposals/" + ctx["source_id"].split(":", 1)[1] + ".md"
    proposal = target(root, proposal_path).read_text(encoding="utf-8")
    assets = []
    for path in dict.fromkeys(re.findall(r"!\[\[([^\]|]+)(?:\|[^\]]*)?\]\]", note)):
        if not path.startswith(ctx["source_dir"] + "/figures/") or not path.endswith(".png"):
            raise WriteConflict("要約内に管理対象外の画像参照があります")
        image = target(root, path)
        if not image.is_file():
            raise WriteConflict("正式版の図表が未取得です。Driveの同期を確認してください")
        assets.append(dict(path=path, sha256=sha(image)))
    row = _base_row(ctx, run, note, proposal, dict(assets=assets, recipe=None))
    if persist:
        _save(store, row)
    return row


def asset_path(cfg, run_id, asset):
    if not re.fullmatch(r"[a-f0-9]{32}", run_id) or not re.fullmatch(
        r"[a-f0-9]{64}", asset["sha256"]
    ):
        raise SettingsError("要約履歴の識別子が不正です")
    suffix = Path(asset["path"]).suffix
    if suffix not in {".png", ".json"}:
        raise SettingsError("要約履歴の図表形式が不正です")
    return cfg.storage.data_dir / "summary-versions" / run_id[:16] / (asset["sha256"][:24] + suffix)


def _build(cfg, store, ctx, run):
    from paperflow.agents.schema import normalize_response
    from paperflow.resources.wiki_extract_visuals import extract_visuals

    data = normalize_response(json.loads(run["output_json"]))
    # Windowsの260文字制限に備え、図表フォルダ名は短縮IDにする。
    subdir = "figures/pfv-" + run["run_id"][:16]
    visuals = dict(assets=[], recipe=None)
    selected = []
    if cfg.destination.include_visuals:
        base = cfg.storage.data_dir / "summary-versions"
        base.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".extract-", dir=base) as temporary:
            output = Path(temporary) / "images"
            visual_data = extract_visuals(
                ctx["pdf"],
                output,
                expected_sha256=ctx["pdf_hash"],
                source_id=ctx["source_id"],
                maximum=cfg.destination.visual_max_items,
                selections=data.get("visual_review", {}).get("selected_visuals"),
            )
            selected = visual_data["items"]
            visuals["recipe"] = visual_data
            for name in dict.fromkeys([*(v["file"] for v in selected), "visuals.json"]):
                src = output / name
                asset = dict(path=ctx["source_dir"] + "/" + subdir + "/" + name, sha256=sha(src))
                dest = asset_path(cfg, run["run_id"], asset)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dest)
                visuals["assets"].append(asset)
    item = dict(
        paper=ctx["paper"],
        metadata=ctx["metadata"],
        analysis=dict(run, data=data),
        pdf_hash=ctx["pdf_hash"],
    )
    proposal_path = "70_Review/Merge_Proposals/" + ctx["source_id"].split(":", 1)[1] + ".md"
    note, proposal = render(
        item,
        ctx["source_id"],
        ctx["note_path"],
        proposal_path,
        ctx["payload"]["blocks"],
        [],
        selected,
        visual_subdir=subdir,
    )
    row = _base_row(ctx, run, note, proposal, visuals)
    _save(store, row)
    return row


def generate(cfg, paper_ids, *, provider=None, checkpoint=None, should_stop=None, on_progress=None):
    """選択分だけを再送。現在の正式版と文献の成功/失敗状態は保持する。"""
    from paperflow.progress_sync import settings

    ids = list(dict.fromkeys(paper_ids))
    if not ids:
        raise SettingsError("再要約する論文を選んでください")
    if settings(cfg).get("enabled") and checkpoint is None:
        raise SettingsError("PC間同期が有効です。共有状態を確認するGUIから実行してください")
    if cfg.analysis.default_provider != "fake" and not (
        cfg.analysis.allow_external_fulltext
        and cfg.analysis.policy == "fulltext_external"
        and cfg.analysis.require_visual_reading
    ):
        raise SettingsError("再要約にはPDF全文の外部送信許可と視覚読解が必要です")
    result = dict(completed=[], failures=[], stopped=False)
    with local_lock(cfg.storage.data_dir), Store(cfg.storage.database) as store:
        # 全選択を先に検証し、対象誤りで一部だけ送信しない。
        for paper_id in ids:
            ctx = context(cfg, store, paper_id)
            if store.rows(
                "SELECT v.run_id FROM summary_versions v JOIN analysis_runs a USING(run_id) "
                "WHERE v.paper_id=? AND v.note_text='' AND a.status IN ('success','running')",
                (paper_id,),
            ):
                raise SettingsError(
                    "未保存または実行中の再要約があります。履歴を更新し、保存済み回答から候補を復元してください"
                )
            if ctx["metadata"].get("no_external") and cfg.analysis.default_provider != "fake":
                raise SettingsError("選択中に外部送信禁止の論文があります")
            if ctx["paper"]["error_type"] in {"sync_result_unknown", "sync_legacy_review"}:
                raise SettingsError("前PCの解析結果が未確認です。PC間同期で確認してください")
        for paper_id in ids:
            if should_stop and should_stop():
                result["stopped"] = True
                break
            ctx = context(cfg, store, paper_id)
            _archive_current(cfg, store, ctx)
            if on_progress:
                on_progress(dict(paper_id=paper_id, title=ctx["paper"]["title"], phase="started"))
            stat = ctx["pdf"].stat()
            match = dict(
                status="pdf_matched",
                path=str(ctx["pdf"]),
                size=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
            )
            try:

                def started(run_id, started_at):
                    row = _base_row(
                        ctx,
                        dict(run_id=run_id, started_at=started_at),
                        "",
                        "",
                        dict(
                            assets=[],
                            recipe=None,
                            requested=dict(
                                include_visuals=cfg.destination.include_visuals,
                                maximum=cfg.destination.visual_max_items,
                            ),
                        ),
                    )
                    _save(store, row)

                run = Pipeline(cfg, provider).analyze_variant(
                    store,
                    ctx["paper"],
                    ctx["metadata"],
                    match,
                    checkpoint=checkpoint,
                    on_started=started,
                )
                row = _build(cfg, store, ctx, run)
                result["completed"].append(dict(paper_id=paper_id, run_id=row["run_id"]))
            except (RuntimeError, OSError, ValueError) as exc:
                result["failures"].append(dict(paper_id=paper_id, message=str(exc)))
                if getattr(exc, "category", "") in {
                    "authentication",
                    "missing_cli",
                    "cli_version",
                    "cli_wrapper",
                    "launch",
                }:
                    break
            if checkpoint:
                checkpoint()
            if on_progress:
                on_progress(
                    dict(
                        phase="saved",
                        paper_id=paper_id,
                        title=ctx["paper"]["title"],
                        completed=len(result["completed"]),
                        failures=len(result["failures"]),
                    )
                )
    return result


def history(cfg, paper_id):
    with Store(cfg.storage.database, snapshot=True) as store:
        ctx = context(cfg, store, paper_id)
        versions = store.rows(
            "SELECT v.*,a.provider,a.model,a.status,a.elapsed_seconds,u.usage_json "
            "FROM summary_versions v JOIN analysis_runs a USING(run_id) "
            "LEFT JOIN analysis_usage u USING(run_id) WHERE v.paper_id=? AND a.status='success' AND v.note_text!='' ORDER BY v.created_at DESC,v.rowid DESC",
            (paper_id,),
        )
        if not any(v["run_id"] == ctx["run"]["run_id"] for v in versions):
            versions.insert(
                0,
                dict(
                    ctx["run"],
                    note_text=target(cfg.destination.vault_dir, ctx["note_path"]).read_text(
                        encoding="utf-8"
                    ),
                ),
            )
        failures = store.rows(
            "SELECT run_id,provider,model,status,error_message,started_at FROM analysis_runs "
            "WHERE paper_id=? AND status!='success' ORDER BY started_at DESC,rowid DESC",
            (paper_id,),
        )
        pending = store.rows(
            "SELECT v.run_id FROM summary_versions v JOIN analysis_runs a USING(run_id) "
            "WHERE v.paper_id=? AND v.note_text='' AND a.status='success'",
            (paper_id,),
        )
    return dict(
        title=ctx["paper"]["title"],
        current=ctx["run"]["run_id"],
        versions=versions,
        failures=failures,
        pending=pending,
    )


def recover(cfg, paper_id, *, checkpoint=None):
    from paperflow.progress_sync import settings

    if settings(cfg).get("enabled") and checkpoint is None:
        raise SettingsError("PC間同期が有効です。共有状態を確認するGUIから実行してください")
    completed = []
    with local_lock(cfg.storage.data_dir), Store(cfg.storage.database) as store:
        ctx = context(cfg, store, paper_id)
        for version in store.rows(
            "SELECT * FROM summary_versions WHERE paper_id=? AND note_text=''", (paper_id,)
        ):
            run = store.rows(
                "SELECT * FROM analysis_runs WHERE run_id=? AND status='success'",
                (version["run_id"],),
            )
            if not run:
                continue
            if version["pdf_sha256"] != ctx["pdf_hash"] or version["source_id"] != ctx["source_id"]:
                raise WriteConflict("保存済み回答と現在の凍結PDFが一致しません")
            requested = json.loads(version["visuals_json"])["requested"]
            if (
                not isinstance(requested["include_visuals"], bool)
                or not 1 <= requested["maximum"] <= 8
            ):
                raise SettingsError("保存済み図表設定が不正です")
            previous = cfg.model_copy(deep=True)
            previous.destination.include_visuals = requested["include_visuals"]
            previous.destination.visual_max_items = requested["maximum"]
            ctx["metadata"] = json.loads(version["metadata_json"])
            _build(previous, store, ctx, run[0])
            completed.append(version["run_id"])
        if checkpoint:
            checkpoint()
    return completed


def _version(store, paper_id, run_id):
    rows = store.rows(
        "SELECT * FROM summary_versions WHERE paper_id=? AND run_id=?", (paper_id, run_id)
    )
    if not rows:
        raise SettingsError("この論文の保存済み要約版が見つかりません")
    run = store.rows("SELECT * FROM analysis_runs WHERE run_id=?", (run_id,))[0]
    validate_version(rows[0], run)
    return rows[0]


def validate_version(version, run):
    """同期した版から、別出典・ノート・制御ファイルへの書き込みを許さない。"""
    import yaml

    if (
        version["run_id"] != run["run_id"]
        or version["paper_id"] != run["paper_id"]
        or not re.fullmatch(r"[a-f0-9]{64}", version["pdf_sha256"])
        or not re.fullmatch(r"paper:paperflow-[a-f0-9]{20}", version["source_id"])
    ):
        raise ValueError("要約版の解析・出典対応が不正です")
    note = version["note_path"]
    if not re.fullmatch(r"10_Papers/20_Source_Notes/[^/\\:]+\.md", note):
        raise ValueError("要約版のノート保存先が不正です")
    if not version["note_text"]:
        if json.loads(version["visuals_json"])["assets"]:
            raise ValueError("保存待ちの要約版に図表があります")
        return
    if run["status"] != "success":
        raise ValueError("成功していない要約版です")
    parts = version["note_text"].split("---", 2)
    fm = yaml.safe_load(parts[1]) if len(parts) == 3 and not parts[0].strip() else {}
    if not isinstance(fm, dict) or any(
        fm.get(k) != v
        for k, v in {
            "source_id": version["source_id"],
            "paperflow_id": version["paper_id"],
            "analysis_run_id": version["run_id"],
            "source_hash": version["pdf_sha256"],
        }.items()
    ):
        raise ValueError("要約版のメタデータが一致しません")
    prefix = "10_Papers/10_Sources/" + version["source_id"].split(":", 1)[1] + "/figures/"
    visuals = json.loads(version["visuals_json"])
    paths = set()
    for asset in visuals["assets"]:
        if (
            not re.fullmatch(
                re.escape(prefix)
                + r"(?:paperflow-visuals-v1|pfv-[a-f0-9]{16})/(?:visuals\.json|(?:figure|table|page)-\d+-p\d+\.png)",
                asset["path"],
            )
            or not re.fullmatch(r"[a-f0-9]{64}", asset["sha256"])
            or asset["path"] in paths
        ):
            raise ValueError("要約版の図表保存先・ハッシュが不正です")
        paths.add(asset["path"])
    images = set(re.findall(r"!\[\[([^\]|]+)(?:\|[^\]]*)?\]\]", version["note_text"]))
    if not images <= paths:
        raise ValueError("要約版に記録外の図表参照があります")


def ensure_assets(cfg, version, pdf):
    """PC間では選択情報とハッシュを共有し、未取得の切り出し画像だけ再現する。"""
    from paperflow.resources.wiki_extract_visuals import extract_visuals

    visuals = json.loads(version["visuals_json"])
    missing = []
    for asset in visuals["assets"]:
        cached = asset_path(cfg, version["run_id"], asset)
        live = target(cfg.destination.vault_dir, asset["path"])
        if cached.is_file() and sha(cached) == asset["sha256"]:
            continue
        if live.is_file() and sha(live) == asset["sha256"]:
            continue
        missing.append(asset)
    if not missing:
        return
    recipe = visuals["recipe"]
    if not recipe:
        raise WriteConflict("旧版の図表が未取得です。VaultのDrive同期を完了してください")
    base = cfg.storage.data_dir / "summary-versions"
    base.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".restore-", dir=base) as temporary:
        output = Path(temporary) / "images"
        extract_visuals(
            pdf,
            output,
            expected_sha256=version["pdf_sha256"],
            source_id=version["source_id"],
            maximum=recipe["maximum"],
            selections=recipe["selections"],
        )
        for asset in missing:
            source = output / Path(asset["path"]).name
            if not source.is_file() or sha(source) != asset["sha256"]:
                raise WriteConflict(
                    "図表の再現結果が保存版と一致しません。元PCのVault同期を確認してください"
                )
            dest = asset_path(cfg, version["run_id"], asset)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, dest)


def adopt(cfg, paper_id, run_id, *, checkpoint=None):
    from paperflow.progress_sync import settings

    if settings(cfg).get("enabled") and checkpoint is None:
        raise SettingsError("PC間同期が有効です。共有状態を確認するGUIから実行してください")
    if not cfg.destination.allow_vault_write:
        raise SettingsError("Vaultへの保存を許可してください")
    with local_lock(cfg.storage.data_dir), Store(cfg.storage.database) as store:
        ctx = context(cfg, store, paper_id)
        if ctx["receipt"].get("status") == "integrated":
            raise WriteConflict("Wiki統合済みの論文です。引用先を確認するレビュー差分が必要です")
        # 手動編集は版切り替えでも自動上書きしない。
        for name, expected in ctx["receipt"]["managed"].items():
            if (
                not target(cfg.destination.vault_dir, name).is_file()
                or sha(target(cfg.destination.vault_dir, name)) != expected
            ):
                raise WriteConflict(
                    "正式ノートまたは統合提案に手動編集があります。自動切り替えを停止しました"
                )
        _archive_current(cfg, store, ctx)
        version = _version(store, paper_id, run_id)
        if version["pdf_sha256"] != ctx["pdf_hash"]:
            raise WriteConflict("要約版と現在の凍結PDFが一致しません")
        ensure_assets(cfg, version, ctx["pdf"])
        run = store.rows(
            "SELECT * FROM analysis_runs WHERE run_id=? AND status='success'", (run_id,)
        )[0]
        item = dict(
            paper=ctx["paper"],
            metadata=json.loads(version["metadata_json"]),
            analysis=dict(run, data=json.loads(run["output_json"])),
            pdf=str(ctx["pdf"]),
            pdf_hash=ctx["pdf_hash"],
            summary_version=version,
        )
        bundle = cfg.storage.data_dir / "sv" / uuid.uuid4().hex[:8]
        plan = prepare(cfg, [item], bundle)
        if checkpoint:
            checkpoint()
        result = apply(cfg, bundle)
        record_exports(cfg, store, plan)
        with store.transaction():
            store.update(paper_id, source_hash=run["source_hash"])
        if checkpoint:
            checkpoint()
        return dict(result, run_id=run_id, bundle=str(bundle))


def _html_markdown(text):
    """生成ノートで使う見出し・表・箇条書きだけを、HTMLを許可せず表示する。"""
    parts, table = [], False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            cells = stripped[1:-1].split("|")
            if all(re.fullmatch(r"\s*:?-+:?\s*", cell) for cell in cells):
                continue
            if not table:
                parts.append("<table>")
                table = True
            parts.append(
                "<tr>"
                + "".join("<td>" + html.escape(cell.strip()) + "</td>" for cell in cells)
                + "</tr>"
            )
            continue
        if table:
            parts.append("</table>")
            table = False
        if not stripped:
            continue
        # リンクは表示文字だけを使い、外部サイトやローカルファイルを自動で開かない。
        stripped = re.sub(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]", lambda m: m[2] or m[1], stripped)
        heading = re.match(r"^(#{1,6})\s+(.+)$", stripped)
        if heading:
            level = len(heading[1])
            parts.append(f"<h{level}>" + html.escape(heading[2]) + f"</h{level}>")
        elif stripped.startswith("- "):
            parts.append('<p class="bullet">・' + html.escape(stripped[2:]) + "</p>")
        elif stripped.startswith(">"):
            quote = re.sub(r"^>\s*(?:\[![^\]]+\]-?\s*)?", "", stripped)
            parts.append('<p class="annotation">' + html.escape(quote) + "</p>")
        else:
            parts.append("<p>" + html.escape(stripped) + "</p>")
    if table:
        parts.append("</table>")
    return "".join(parts)


def comparison(cfg, paper_id, run_id):
    """外部リソース・スクリプトなしの、本文と図表の比較HTML。"""
    with local_lock(cfg.storage.data_dir), Store(cfg.storage.database, snapshot=True) as store:
        ctx = context(cfg, store, paper_id)
        current = _archive_current(cfg, store, ctx, persist=False)
        versions = [
            current,
            current if run_id == current["run_id"] else _version(store, paper_id, run_id),
        ]
    sections = []
    for label, version in zip(["現在の正式版", "比較する要約版"], versions, strict=True):
        ensure_assets(cfg, version, ctx["pdf"])
        text = version["note_text"]
        if text.startswith("---\n"):
            text = text.split("---", 2)[2]
        assets = {a["path"]: a for a in json.loads(version["visuals_json"])["assets"]}
        parts, previous = [], 0
        for match in re.finditer(r"!\[\[([^\]|]+)(?:\|[^\]]*)?\]\]", text):
            parts.append(_html_markdown(text[previous : match.start()]))
            asset = assets.get(match[1])
            if asset:
                local = asset_path(cfg, version["run_id"], asset)
                if not local.is_file():
                    local = target(cfg.destination.vault_dir, asset["path"])
                parts.append(
                    '<img alt="論文の図表" src="'
                    + html.escape(local.resolve().as_uri(), quote=True)
                    + '">'
                )
            previous = match.end()
        parts.append(_html_markdown(text[previous:]))
        sections.append("<section><h2>" + label + "</h2>" + "".join(parts) + "</section>")
    page = (
        '<!doctype html><html lang="ja"><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src \'none\'; img-src file:; style-src \'unsafe-inline\'"><title>要約の比較</title><style>body{margin:24px;font:16px/1.8 sans-serif;color:#243b31;background:#f4f7f5}main{display:grid;grid-template-columns:1fr 1fr;gap:20px}section{background:white;padding:20px;min-width:0;overflow-wrap:anywhere}img{max-width:100%;height:auto}section>h2:first-child{position:sticky;top:0;background:white;padding:12px}table{border-collapse:collapse;font-size:14px}td{border:1px solid #cad5cd;padding:8px}tr:first-child{background:#eef3f0;font-weight:bold}.annotation{color:#61726a;font-size:14px}.bullet{margin:4px 0}</style><h1>'
        + html.escape(ctx["paper"]["title"])
        + "</h1><main>"
        + "".join(sections)
        + "</main></html>"
    )
    path = (
        cfg.storage.data_dir / "summary-comparisons" / (digest([paper_id, run_id])[:20] + ".html")
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(page, encoding="utf-8")
    return str(path)
