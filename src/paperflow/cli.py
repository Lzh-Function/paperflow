import json
import sys
import time
import uuid
from pathlib import Path

import click

from paperflow.agents.base import ProviderError
from paperflow.agents.catalog import catalog_data
from paperflow.config import SettingsError, load_config
from paperflow.doctor import doctor as inspect_environment
from paperflow.model_selection import configure, get_catalog
from paperflow.obsidian.writer import SafeWriter, WriteConflict, local_lock
from paperflow.pipeline import Pipeline
from paperflow.progress_sync import SyncError
from paperflow.progress_sync import settings as sync_settings
from paperflow.reporting.reports import report as make_report
from paperflow.reporting.reports import review_report
from paperflow.store import Store, now


def emit(ctx, data):
    # JSON is intentionally also the default textual report, with readable Japanese.
    click.echo(json.dumps(data, ensure_ascii=False, indent=2))


@click.group()
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default="config.yaml",
    show_default=True,
)
@click.option(
    "--verbose", is_flag=True, help="詳細な工程情報を表示（本文・認証情報は出力しません）。"
)
@click.option("--json", "as_json", is_flag=True, help="機械可読JSONで出力。")
@click.version_option(package_name="paperflow")
@click.pass_context
def app(ctx, config_path, verbose, as_json):
    """Paperpileを読み取り専用で取り込み、専用Obsidianノートを生成します。"""
    ctx.obj = dict(config=load_config(config_path), json=as_json, verbose=verbose)


def execute(ctx, **kwargs):
    result = Pipeline(ctx.obj["config"]).run(**kwargs)
    if "model_selection" in ctx.obj:
        result["model_selection"] = ctx.obj["model_selection"]
    if ctx.obj["verbose"]:
        click.echo("scan→extract→analyze→export: 処理件数は結果JSONを参照。", err=True)
    emit(ctx, result)
    if result.get("failures"):
        ctx.exit(3)


def model_options(command):
    command = click.option(
        "--interactive/--no-interactive",
        default=None,
        help="Provider・モデルを番号で選択。端末では自動表示、JSON・非端末では省略。",
    )(command)
    command = click.option("--model", help="今回だけ使用するモデルID。対話でのモデル選択を省略。")(
        command
    )
    return click.option(
        "--provider", "provider_name", type=click.Choice(["fake", "claude", "codex", "agy"])
    )(command)


@app.command("select-model")
@click.option("--provider", "provider_name", type=click.Choice(["fake", "claude", "codex", "agy"]))
@click.option("--dry-run", is_flag=True, help="保存済み候補だけを表示し、選択内容を保存しません。")
@click.pass_context
def select_model(ctx, provider_name, dry_run):
    """Providerとモデルを対話で選択・記憶。論文の解析は行いません。"""
    info = configure(ctx, provider_name, dry_run=dry_run, selection_only=True)
    emit(ctx, dict(**info, saved=not dry_run))


@app.command()
@click.option("--provider", type=click.Choice(["claude", "codex", "agy"]), required=True)
@click.option("--cached", is_flag=True, help="保存済み候補のみ読む。CLI照会・ファイル変更なし。")
@click.pass_context
def models(ctx, provider, cached):
    """公式CLIのモデル候補を照会・保存。論文やプロンプトは送信しません。"""
    try:
        emit(ctx, catalog_data(get_catalog(ctx.obj["config"], provider, cached_only=cached)))
    except ProviderError as exc:
        raise click.ClickException(str(exc)) from exc


@app.command("batch-plan")
@click.option("--all", "all_papers", is_flag=True, help="処理可能な未登録論文を全件選択。")
@click.option("--count", type=click.IntRange(min=1), help="未登録から追加する本数。")
@click.option("--total", type=click.IntRange(min=1), help="Vaultの登録済み論文を含む累計目標本数。")
@click.option("--seed", type=int, help="乱択の再現用seed。省略時は自動生成。")
@click.option(
    "--output",
    type=click.Path(path_type=Path),
    help="C:の新規計画JSON。省略時はローカルdata_dir/batches。",
)
@click.option("--dry-run", is_flag=True, help="表示だけ。DB・ファイル・外部CLIへの副作用なし。")
@click.pass_context
def batch_plan(ctx, count, total, all_papers, seed, output, dry_run):
    """未登録PDFを全件または指定本数選び、対象を固定する。AI・Vault書き込みなし。"""
    from paperflow.batches import plan

    emit(
        ctx,
        plan(
            ctx.obj["config"],
            count=count,
            total=total,
            all_papers=all_papers,
            seed=seed,
            output=output,
            dry_run=dry_run,
        ),
    )


@app.command("batch-run")
@click.option("--plan", "plan_path", type=click.Path(exists=True, path_type=Path), required=True)
@click.option("--dry-run", is_flag=True, help="残り対象の表示だけ。解析・出力・設定保存なし。")
@model_options
@click.pass_context
def batch_run(ctx, plan_path, dry_run, provider_name, model, interactive):
    """固定した対象だけを分割して要約・Vault保存する。同じ計画で再開可能。"""
    from paperflow.batches import execute

    selection = configure(ctx, provider_name, model, interactive, dry_run=dry_run)

    def progress(data):
        if not ctx.obj["json"]:
            click.echo(
                f"バッチ: {data['registered']}/{data['planned']}本登録済み、今回の対象確認{data['attempted']}本、外部AI呼出し{data['external_calls']}回",
                err=True,
            )

    result = execute(ctx.obj["config"], plan_path, dry_run=dry_run, on_progress=progress)
    result["model_selection"] = selection
    shown = (
        {**result, "usage": {key: value for key, value in result["usage"].items() if key != "runs"}}
        if not dry_run and not ctx.obj["json"]
        else result
    )
    emit(ctx, shown)
    if not dry_run and result["status"] != "complete":
        ctx.exit(2)


@app.command("batch-report")
@click.option("--plan", "plan_path", type=click.Path(exists=True, path_type=Path), required=True)
@click.pass_context
def batch_report(ctx, plan_path):
    """バッチの登録本数とCLI申告の消費量を表示。読み取り専用。"""
    from paperflow.batches import report

    result = report(ctx.obj["config"], plan_path)
    if not ctx.obj["json"] and "usage" in result:
        result["usage"].pop("runs", None)
    emit(ctx, result)


@app.command("wiki-prepare")
@click.option(
    "--cached-only", is_flag=True, required=True, help="保存済み解析だけを使用。AI呼び出しなし。"
)
@click.option("--paper-id", "paper_ids", multiple=True)
@click.option("--output", type=click.Path(path_type=Path), help="C:内の新規bundle保存先。")
@click.option("--upgrade-framework", is_flag=True, help="初回のWikiフレームワーク移行差分も準備。")
@click.option(
    "--dry-run", is_flag=True, help="候補だけ表示。ディスク・DB・Vault・外部CLIへの副作用なし。"
)
@click.pass_context
def wiki_prepare(ctx, cached_only, paper_ids, output, upgrade_framework, dry_run):
    """保存済み解析から凍結登録・Source Note・統合提案をC:で準備。"""
    from paperflow.wiki.export import cached_items, prepare

    config = ctx.obj["config"]
    items = cached_items(config, paper_ids)
    if dry_run:
        emit(
            ctx,
            dict(
                dry_run=True,
                papers=[
                    dict(paper_id=i["paper"]["paper_id"], title=i["metadata"]["title"])
                    for i in items
                ],
            ),
        )
        return
    if output is None:
        raise click.UsageError("--outputでC:内の新規ディレクトリを指定してください")
    plan = prepare(config, items, output, upgrade=upgrade_framework)
    emit(
        ctx,
        dict(
            bundle=str(output.resolve()),
            changes=[e["path"] for e in plan["changes"]],
            registered=plan["registered"],
            validation=plan["validation"],
        ),
    )


@app.command("wiki-apply")
@click.option("--bundle", type=click.Path(exists=True, path_type=Path), required=True)
@click.option(
    "--approved-plan-sha256",
    help="Wiki統合の差分確認・承認後のplan SHA-256。通常取り込みでは不要。",
)
@click.pass_context
def wiki_apply(ctx, bundle, approved_plan_sha256):
    """確認済みbundleをハッシュ検査・共通Writerロック付きで反映。"""
    from paperflow.wiki.bundle import apply
    from paperflow.wiki.export import record_exports

    config = ctx.obj["config"]
    if sync_settings(config).get("enabled"):
        raise SyncError("PC間同期中のVault書き込みは、共有状態を確認するGUIから実行してください")
    with local_lock(config.storage.data_dir):
        plan = json.loads((bundle / "plan.json").read_text(encoding="utf-8"))
        with Store(config.storage.database, readonly=True) as store:
            for record in plan["registered"]:
                store.paper(record["paper_id"])
        result = apply(config, bundle, approved_promotion_sha256=approved_plan_sha256)
        with Store(config.storage.database) as store:
            record_exports(config, store, plan)
        emit(ctx, result)


@app.command("tags-backfill")
@click.option("--dry-run", is_flag=True, help="変更候補だけ表示。ディスク・DBへ書き込みません。")
@click.option("--apply", "apply_now", is_flag=True, help="本文を保持した主題タグ追補を反映。")
@click.option("--output", type=click.Path(path_type=Path), help="C:の確認用bundle保存先。")
@click.pass_context
def tags_backfill(ctx, dry_run, apply_now, output):
    """既存PaperFlow生成Source NoteへObsidianのtopic/*タグを追補。"""
    from paperflow.wiki.tag_migration import apply_migration, inspect, prepare

    cfg = ctx.obj["config"]
    if dry_run and apply_now:
        raise SettingsError("--dry-runと--applyは同時に指定できません")
    if dry_run:
        emit(ctx, dict(dry_run=True, notes=inspect(cfg)))
        return
    if apply_now and sync_settings(cfg).get("enabled"):
        raise SyncError("タグ追補の反映前に両PCの処理を終了して同期を解除してください")
    output = (output or cfg.storage.data_dir / "subject-tags" / uuid.uuid4().hex).resolve()
    with local_lock(cfg.storage.data_dir):
        plan = prepare(cfg, output)
        result = dict(bundle=str(output), notes=len(plan["notes"]), changes=len(plan["changes"]))
        if apply_now:
            SafeWriter(cfg).preflight()
            result["applied"] = apply_migration(cfg, output)
        emit(ctx, result)


@app.command()
@click.pass_context
def doctor(ctx):
    """原本・Vault・BibTeX・CLIの読み取り専用診断。"""
    data = inspect_environment(ctx.obj["config"])
    emit(ctx, data)
    if any(
        not c["ok"] for c in data["checks"] if c["check"] in {"paperpile", "vault", "wiki_marker"}
    ):
        ctx.exit(2)


@app.command()
@click.option("--dry-run", is_flag=True)
@click.pass_context
def scan(ctx, dry_run):
    """LLMを呼ばずにBibTeXとPDF候補を検出。"""
    execute(ctx, dry_run=dry_run, scan_only=True)


@app.command()
@click.option("--dry-run", is_flag=True, help="DB・ファイル・外部LLMを変更せず提案だけ表示。")
@click.option("--limit", type=click.IntRange(min=1))
@model_options
@click.pass_context
def sync(ctx, dry_run, limit, provider_name, model, interactive):
    """scan→抽出→解析→安全なノート出力。"""
    selection = configure(ctx, provider_name, model, interactive, dry_run=dry_run)
    execute(ctx, dry_run=dry_run, limit=limit, provider_name=selection["provider"])


@app.command()
@click.option("--paper-id", required=True)
@model_options
@click.option("--force", is_flag=True)
@click.pass_context
def analyze(ctx, paper_id, provider_name, force, model, interactive):
    """指定文献を解析し、ローカルに結果を保存（Vaultは変更しません）。"""
    selection = configure(ctx, provider_name, model, interactive)
    execute(
        ctx, paper_ids=[paper_id], provider_name=selection["provider"], force=force, export=False
    )


@app.command()
@click.option("--failed", is_flag=True, help="再試行可能かつ待機時間を過ぎた失敗だけを再投入。")
@click.option("--dry-run", is_flag=True)
@model_options
@click.pass_context
def retry(ctx, failed, dry_run, provider_name, model, interactive):
    """一時障害を回数上限と指数バックオフ付きで再試行。"""
    selection = configure(ctx, provider_name, model, interactive, dry_run=dry_run)
    execute(ctx, retry=True, dry_run=dry_run, provider_name=selection["provider"])


@app.command()
@click.pass_context
def status(ctx):
    """DBの工程別件数、失敗、直近成功を表示。"""
    with Store(ctx.obj["config"].storage.database, readonly=True) as store:
        emit(
            ctx,
            dict(
                stages=store.rows("SELECT state,COUNT(*) AS count FROM papers GROUP BY state"),
                missing_from_source=store.rows(
                    "SELECT COUNT(*) AS count FROM papers WHERE source_present=0"
                )[0]["count"],
                failures=store.rows(
                    "SELECT paper_id,state,error_type,retry_count,next_retry_at "
                    "FROM papers WHERE error_type IS NOT NULL"
                ),
                recent_runs=store.rows(
                    "SELECT provider,status,started_at,elapsed_seconds "
                    "FROM analysis_runs ORDER BY started_at DESC LIMIT 5"
                ),
            ),
        )


@app.command()
@click.option("--min-priority", type=click.IntRange(1, 5), default=5, show_default=True)
@click.option(
    "--write", is_flag=True, help="承認済み設定で専用Reports/Inboxに保存。省略時は表示のみ。"
)
@click.pass_context
def report(ctx, min_priority, write):
    """読書優先度と要確認一覧をMarkdownで生成。"""
    config = ctx.obj["config"]
    if write:
        if config.destination.layout == "llm_wiki":
            from paperflow.wiki.bundle import apply, sha
            from paperflow.wiki.export import prepare_reports

            with local_lock(config.storage.data_dir), Store(config.storage.database) as store:
                bundle = config.storage.data_dir / "wiki-bundles" / uuid.uuid4().hex[:12]
                _, outputs = prepare_reports(config, store, min_priority, bundle)
                result = apply(config, bundle)
                with store.transaction():
                    for name in outputs:
                        store.db.execute(
                            "INSERT OR REPLACE INTO managed_files VALUES (?,?,?)",
                            (name, sha(config.destination.vault_dir / name), now()),
                        )
                emit(ctx, result)
            return
        writer = SafeWriter(config)
        writer.preflight()
        with local_lock(config.storage.data_dir), Store(config.storage.database) as store:
            results = []
            for path, text in (
                ("Reports/top-rated.md", make_report(store, min_priority)),
                ("Inbox/review-needed.md", review_report(store)),
            ):
                old = store.rows("SELECT generated_hash FROM managed_files WHERE path=?", (path,))
                state, hash_ = writer.write(path, text, old[0]["generated_hash"] if old else None)
                with store.transaction():
                    store.db.execute(
                        "INSERT OR REPLACE INTO managed_files VALUES (?,?,?)", (path, hash_, now())
                    )
                results.append(dict(path=path, status=state))
            emit(ctx, results)
    else:
        with Store(config.storage.database, readonly=True) as store:
            text = make_report(store, min_priority, config) + "\n" + review_report(store)
            if ctx.obj["json"]:
                emit(ctx, dict(markdown=text))
            else:
                click.echo(text)


@app.command()
@click.option("--interval", type=click.IntRange(min=1))
@click.option("--once", is_flag=True, help="1回だけ実行して終了（運用確認用）。")
@model_options
@click.pass_context
def watch(ctx, interval, once, provider_name, model, interactive):
    """手動起動中のみポーリング。タスクスケジューラには登録しません。"""
    config = ctx.obj["config"]
    SafeWriter(config).preflight()
    selection = configure(ctx, provider_name, model, interactive)
    partial = False
    while True:
        try:
            data = Pipeline(config).run()
            data["model_selection"] = selection
            partial |= bool(data["failures"])
            emit(ctx, data)
            retry_data = Pipeline(config).run(retry=True)
            partial |= bool(retry_data["failures"])
        except (OSError, WriteConflict) as exc:
            partial = True
            emit(
                ctx,
                dict(error=type(exc).__name__, message="Drive接続・Writerロックを確認してください"),
            )
        if once:
            if partial:
                ctx.exit(3)
            return
        time.sleep(interval or config.sync.interval_seconds)


def main():
    try:
        exit_code = app(standalone_mode=False)
        if isinstance(exit_code, int):
            sys.exit(exit_code)
    except SettingsError as exc:
        click.echo(f"設定エラー: {exc}", err=True)
        sys.exit(2)
    except click.ClickException as exc:
        exc.show()
        sys.exit(exc.exit_code)
    except click.exceptions.Exit as exc:
        sys.exit(exc.exit_code)
    except (OSError, ValueError, WriteConflict, SyncError) as exc:
        click.echo(f"処理エラー: {exc}", err=True)
        sys.exit(1)
    except (KeyboardInterrupt, click.Abort):
        click.echo("中断しました。次回の実行で再開できます。", err=True)
        sys.exit(130)
