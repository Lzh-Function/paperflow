"""デスクトップ画面から共通処理を呼び出す。GUI部品を参照しない。"""

import json
import threading
from pathlib import Path

from paperflow import batches, library
from paperflow.agents.catalog import model_id
from paperflow.config import SettingsError, load_config
from paperflow.model_selection import cached_catalog, get_catalog, preferences, remember
from paperflow.obsidian.writer import local_lock
from paperflow.pipeline import Pipeline
from paperflow.progress_sync import ProgressSync


class GuiService:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.stop_event = threading.Event()
        self._registry = None
        self._registry_binding = None
        self._registry_attempted = False
        self._registry_checked_at = ""
        self._registry_warning = ""
        self._sync_confirmation = None

    def _sync_binding(self, cfg):
        from paperflow.progress_sync import settings

        state = settings(cfg)
        return (
            str(cfg.storage.database),
            str(cfg.source.paperpile_dir),
            str(cfg.destination.vault_dir),
            state.get("workspace_id"),
            state.get("shared_folder"),
        )

    def progress_confirmed(self, cfg=None):
        return self._sync_confirmation == self._sync_binding(cfg or self.config())

    def cloud(self, cfg=None):
        cfg = cfg or self.config()
        return ProgressSync(cfg, confirmed=self.progress_confirmed(cfg))

    def confirm_progress(self):
        cfg = self.config()
        cloud = ProgressSync(cfg, confirmed=True)
        if not cloud.enabled:
            from paperflow.progress_sync import SyncError

            raise SyncError("先に共有フォルダを新規作成するか、既存の共有を引き継いでください")
        # モデル等が違うPCでも受信・Drive確認までは行い、設定の採用は利用者が選ぶ。
        from paperflow.portable import profile, profiles_compatible

        result = cloud.sync(publish=False)
        shared = cloud.local.get("shared_profile")
        if not shared or profiles_compatible(shared, profile(cfg)):
            result = cloud.sync()
        self._sync_confirmation = self._sync_binding(cfg)
        self.clear_sync_error()
        return result

    def join_progress_folder(self, folder):
        from paperflow.folder_sync import FolderTransport

        cfg = self.config()
        transport = FolderTransport(cfg, folder)
        result = ProgressSync(cfg, transport, confirmed=True).connect(transport.workspace())
        self._sync_confirmation = self._sync_binding(cfg)
        self.clear_sync_error()
        return result

    def create_progress_folder(self, folder, name):
        from paperflow.drive_paths import drive_path
        from paperflow.folder_sync import FolderTransport
        from paperflow.progress_sync import SyncError, settings
        from paperflow.sync_snapshot import capture, packed

        cfg = self.config()
        transport = FolderTransport(cfg, folder)
        with local_lock(cfg.storage.data_dir):
            state = settings(cfg)
            if state.get("enabled"):
                if (
                    state.get("head")
                    or not state.get("shared_folder")
                    or Path(drive_path(state["shared_folder"])).resolve() != transport.root
                ):
                    raise SyncError(
                        "共有は設定済みです。「共有ファイルを受信・更新」を使ってください。別の共有先に変える場合は先に解除してください"
                    )
                cloud = ProgressSync(cfg, transport, confirmed=True)
                result = cloud.sync(_lock_held=True)
            else:
                # 原本/Vaultへの参照と容量を先に検証し、失敗時に空の共有だけを残さない。
                packed(capture(cfg))
                workspace = transport.create_workspace(name)
                result = ProgressSync(cfg, transport, confirmed=True).connect(
                    workspace, create=True, _lock_held=True
                )
        self._sync_confirmation = self._sync_binding(cfg)
        self.clear_sync_error()
        return dict(
            result,
            message="共有データを作成しました："
            + str(transport.root)
            + "\n生成済み：paperflow-workspace.json / commits / objects。Driveへの送信完了はDriveアプリで確認してください。",
        )

    def record_sync_error(self, message):
        from paperflow.progress_sync import directory
        from paperflow.store import now
        from paperflow.wiki.bundle import json_write

        json_write(directory(self.config()) / "last-error.json", dict(at=now(), message=message))

    def sync_error(self):
        from paperflow.progress_sync import directory

        path = directory(self.config()) / "last-error.json"
        try:
            return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        except (OSError, ValueError):
            return None

    def clear_sync_error(self):
        from paperflow.progress_sync import directory

        (directory(self.config()) / "last-error.json").unlink(missing_ok=True)

    def config(self, provider=None, model=None):
        cfg = load_config(self.path)
        if provider is not None:
            cfg.analysis.default_provider = provider
            cfg.analysis.model = None if provider == "fake" or not model else model_id(model)
        return cfg

    def initial_selection(self):
        cfg = self.config()
        saved = preferences(cfg)
        provider = saved["last_provider"] or cfg.analysis.default_provider
        model = saved["models"].get(provider) or (
            cfg.analysis.model if provider == cfg.analysis.default_provider else None
        )
        return provider, model

    def models(self, provider, *, refresh=False):
        cfg = self.config()
        if provider == "fake":
            return None
        return get_catalog(cfg, provider) if refresh else cached_catalog(cfg, provider)

    def remember(self, provider, model):
        cfg = self.config(provider, model)
        remember(cfg, provider, cfg.analysis.model)

    def prepare(self, mode, count=None):
        cfg = self.config()
        cloud = self.cloud(cfg)
        if cloud.enabled:
            cloud.begin()
        # 同期の安定性の観測と一覧作成はローカル処理のみ。
        try:
            Pipeline(cfg).run(scan_only=True)
            options = dict(
                all_papers=mode == "all",
                count=count if mode == "count" else None,
                total=count if mode == "total" else None,
            )
            return batches.plan(cfg, **options)
        finally:
            if cloud.enabled:
                cloud.end()

    def load_plan(self, path):
        return batches.load(self.config(), Path(path))

    def run(self, path, provider, model, on_progress=None, on_paper=None):
        cfg = self.config(provider, model)
        if not cfg.destination.allow_vault_write:
            raise SettingsError("設定の「Vaultへの保存を許可」を有効にしてください")
        if provider != "fake" and not (
            cfg.analysis.allow_external_fulltext
            and cfg.analysis.policy == "fulltext_external"
            and cfg.analysis.require_visual_reading
        ):
            raise SettingsError("設定で全文解析・PDFの外部送信・視覚読解を有効にしてください")
        remember(cfg, provider, cfg.analysis.model)
        cloud = self.cloud(cfg)
        if cloud.enabled:
            cloud.begin()
        try:
            return batches.execute(
                cfg,
                Path(path),
                on_progress=on_progress,
                on_paper=on_paper,
                should_stop=self.stop_event.is_set,
                save_each_paper=True,
                checkpoint=cloud.checkpoint if cloud.enabled else None,
            )
        finally:
            if cloud.enabled:
                cloud.end()

    def report(self, path):
        return batches.report(self.config(), Path(path))

    def live_report(self, path):
        return batches.live_report(self.config(), Path(path))

    def library(self, *, reconcile=False):
        from paperflow.store import now

        cfg = self.config()
        binding = batches.binding(cfg)
        if binding != self._registry_binding:
            self._registry = None
            self._registry_attempted = False
            self._registry_checked_at = ""
            self._registry_warning = ""
            self._registry_binding = binding
        if cfg.destination.layout == "llm_wiki" and (reconcile or not self._registry_attempted):
            self._registry_attempted = True
            try:
                registry = batches.Registry(cfg)
            except (OSError, ValueError) as exc:
                self._registry_warning = "Vaultとの照合を完了できませんでした：" + str(exc)
            else:
                self._registry = registry
                self._registry_checked_at = now()
                self._registry_warning = ""
        result = library.snapshot(cfg, self._registry)
        result.update(registry_checked_at=self._registry_checked_at, warning=self._registry_warning)
        return result

    def sync_library(self):
        cfg = self.config()
        cloud = self.cloud(cfg)
        if cloud.enabled:
            cloud.begin()
        try:
            return Pipeline(cfg).run(scan_only=True)
        finally:
            if cloud.enabled:
                cloud.end()

    def summary_history(self, paper_id):
        from paperflow.summary_versions import history

        return history(self.config(), paper_id)

    def generate_summaries(self, paper_ids, provider, model, on_progress=None):
        from paperflow.summary_versions import generate

        cfg = self.config(provider, model)
        # 再要約のモデルはこのジョブだけの指定。通常バッチの共有設定を変更しない。
        cloud = self.cloud(self.config())
        if cloud.enabled:
            cloud.begin()
        self.stop_event.clear()
        try:
            return generate(
                cfg,
                paper_ids,
                checkpoint=cloud.checkpoint if cloud.enabled else None,
                should_stop=self.stop_event.is_set,
                on_progress=on_progress,
            )
        finally:
            if cloud.enabled:
                cloud.end()

    def adopt_summary(self, paper_id, run_id):
        from paperflow.summary_versions import adopt

        cfg = self.config()
        cloud = self.cloud(cfg)
        if cloud.enabled:
            cloud.begin()
        try:
            result = adopt(
                cfg, paper_id, run_id, checkpoint=cloud.checkpoint if cloud.enabled else None
            )
            self._registry_attempted = False
            return result
        finally:
            if cloud.enabled:
                cloud.end()

    def compare_summary(self, paper_id, run_id):
        from paperflow.summary_versions import comparison

        return comparison(self.config(), paper_id, run_id)

    def recover_summaries(self, paper_id):
        from paperflow.summary_versions import recover

        cfg = self.config()
        cloud = self.cloud(cfg)
        if cloud.enabled:
            cloud.begin()
        try:
            return recover(cfg, paper_id, checkpoint=cloud.checkpoint if cloud.enabled else None)
        finally:
            if cloud.enabled:
                cloud.end()

    def revalidate_responses(self, paper_ids, provider=None, model=None):
        from paperflow.response_recovery import revalidate_selected

        cfg = self.config(provider, model)
        cloud = self.cloud(self.config())
        if cloud.enabled:
            cloud.begin()
        try:
            return revalidate_selected(cfg, paper_ids)
        finally:
            if cloud.enabled:
                cloud.end()

    def progress_sync(self):
        cloud = self.cloud()
        result = cloud.sync(publish=cloud.confirmed)
        self.clear_sync_error()
        return result

    def progress_action(self, action):
        if action not in {"takeover", "acknowledge", "share_profile"}:
            raise ValueError("不明な同期操作です")
        result = getattr(self.cloud(), action)()
        self.clear_sync_error()
        return result

    def recovery_overview(self):
        from paperflow import recovery

        self.library()
        if self._registry is None:
            raise SettingsError("Vaultとの照合が必要です。接続と保存先を確認してください")
        return dict(
            recovery.overview(self.config(), self._registry), jobs=recovery.history(self.config())
        )

    def prepare_recovery(self, paper_ids, mode, provider=None, model=None):
        from paperflow.recovery import prepare_job

        return prepare_job(self.config(), paper_ids, mode, provider=provider, model=model)

    def run_recovery(self, job, on_progress=None):
        from paperflow.recovery import execute

        cfg = self.config()
        cloud = self.cloud(cfg)
        if cloud.enabled:
            cloud.begin()
        self.stop_event.clear()
        try:
            return execute(
                cfg,
                job,
                checkpoint=cloud.checkpoint if cloud.enabled else None,
                should_stop=self.stop_event.is_set,
                on_progress=on_progress,
            )
        finally:
            self._registry_attempted = False
            if cloud.enabled:
                cloud.end()

    def load_recovery(self, identifier):
        from paperflow.recovery import load_job

        return load_job(self.config(), identifier)

    def prepare_saved_result(self, folder):
        from paperflow.saved_result import prepare_import

        return prepare_import(self.config(), folder)

    def history(self):
        root = self.config().storage.data_dir / "batches"
        items = []
        if not root.exists():
            return items
        for path in sorted(root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            if path.stat().st_size > 16 * 1024 * 1024:
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if data.get("type") != "paperflow-batch":
                    continue
                progress = path.with_name(path.stem + ".progress.json")
                state = (
                    json.loads(progress.read_text(encoding="utf-8")).get("status", "not_started")
                    if progress.exists()
                    else "not_started"
                )
                items.append(
                    dict(
                        path=str(path),
                        created_at=data.get("created_at", ""),
                        count=data.get("selected_count", 0),
                        status=state,
                    )
                )
            except (OSError, ValueError):
                continue
        return items
