"""共有フォルダの初期設定と引き継ぎ。クラウドの完了確認は利用者が行う。"""

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import yaml

from paperflow.folder_sync import FolderTransport
from paperflow.gui_config import save_changes
from paperflow.progress_sync import ProgressSync, SyncError, directory, save_settings


class SyncPanel:
    def _build_sync(self):
        from paperflow.gui import ScrollFrame

        self.cloud_scroll = ScrollFrame(self.cloud_tab)
        self.cloud_scroll.pack(fill="both", expand=True)
        frame = self.cloud_scroll.inner
        self.cloud_status = tk.StringVar()
        self.cloud_name = tk.StringVar(value="PaperFlow 共有進捗")
        state = ProgressSync(self.cfg).status()
        self.cloud_folder = tk.StringVar(
            value=state.get("shared_folder")
            or str(self.cfg.destination.vault_dir.parent / "PaperFlow-Sync")
        )
        self.drive_checked = tk.BooleanVar(value=False)
        self._cloud_seen_service = None
        self._cloud_seen_binding = None
        self.cloud_result = tk.StringVar(
            value="フォルダの選択だけでは保存されません。初回は「最初のPC：共有を新規作成」を押してください。"
        )
        self.cloud_paths = {}
        ttk.Label(frame, text="PC間同期", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            frame,
            text="初期設定：1の保存場所を設定した後、1台で使う場合は「取り込み」タブへ進みます。\n複数PCで続きを共有する場合は、2以降も設定してください。",
            wraplength=990,
        ).pack(anchor="w", pady=(6, 0))
        ttk.Label(frame, textvariable=self.cloud_status, wraplength=990).pack(
            anchor="w", pady=(8, 12)
        )
        ttk.Label(frame, text="1. このPCの保存場所を設定", style="Heading.TLabel").pack(anchor="w")
        paths = ttk.Frame(frame)
        paths.pack(fill="x", pady=8)
        paths.columnconfigure(1, weight=1)
        for row, (name, label, value) in enumerate(
            [
                ("source.paperpile_dir", "Paperpileフォルダ", self.cfg.source.paperpile_dir),
                ("source.bibtex_file", "BibTeXファイル", self.cfg.source.bibtex_file),
                ("destination.vault_dir", "Obsidian Vault", self.cfg.destination.vault_dir),
            ]
        ):
            var = tk.StringVar(value=str(value or ""))
            self.cloud_paths[name] = var
            ttk.Label(paths, text=label).grid(row=row, column=0, sticky="w", padx=(0, 12))
            self.mutable_widget(ttk.Entry(paths, textvariable=var)).grid(
                row=row, column=1, sticky="ew", pady=3
            )
            self.mutable_widget(
                ttk.Button(paths, text="参照…", command=lambda n=name: self.cloud_browse(n))
            ).grid(row=row, column=2, padx=6)
        row = ttk.Frame(frame)
        row.pack(fill="x")
        self.button(row, "このPCのパスを保存", self.cloud_save_paths, side="left")
        self.button(row, "空の作業DBへ切替", self.cloud_new_data, side="left", padx=6)
        self.button(row, "Claudeにログイン", lambda: self.cloud_cli_login("claude"), side="left")
        self.button(
            row, "Codexにログイン", lambda: self.cloud_cli_login("codex"), side="left", padx=6
        )
        self.button(row, "agyにログイン", lambda: self.cloud_cli_login("agy"), side="left", padx=6)
        ttk.Label(frame, text="2. Drive内の専用共有フォルダを選ぶ", style="Heading.TLabel").pack(
            anchor="w", pady=(16, 6)
        )
        ttk.Label(
            frame,
            text="両PCのGoogle Driveデスクトップ版で同じフォルダを使います。例：H:\\My Drive\\PaperFlow-Sync\nPaperpile・Vault・C:の作業データとは分離してください。Google CloudやOAuthの設定は不要です。",
            wraplength=990,
        ).pack(anchor="w")
        row = ttk.Frame(frame)
        row.pack(fill="x", pady=8)
        self.mutable_widget(ttk.Entry(row, textvariable=self.cloud_folder)).pack(
            side="left", fill="x", expand=True
        )
        self.button(row, "参照…", self.cloud_choose_folder, side="left", padx=6)
        self.button(
            row, "フォルダを開く", lambda: self.open_path(self.cloud_folder.get()), side="left"
        )
        row = ttk.Frame(frame)
        row.pack(fill="x", pady=4)
        ttk.Label(row, text="共有名").pack(side="left", padx=(0, 8))
        self.mutable_widget(ttk.Entry(row, textvariable=self.cloud_name, width=25)).pack(
            side="left"
        )
        self.button(row, "最初のPC：共有を新規作成", self.cloud_create, side="left", padx=8)
        self.button(row, "2台目：既存の共有を引き継ぐ", self.cloud_join, side="left")
        ttk.Label(frame, textvariable=self.cloud_result, wraplength=990).pack(anchor="w", pady=6)
        ttk.Label(
            frame, text="3. PCを切り替えたら、Driveの同期完了を確認", style="Heading.TLabel"
        ).pack(anchor="w", pady=(16, 6))
        ttk.Label(
            frame,
            text="Windows右下のDriveアイコンで、前PCからの送信とこのPCへの受信が完了していることを確認します。\nPaperFlowはクラウドの最新版や送信完了を自動判定しません。この確認は起動のたびに必要です。",
            wraplength=990,
        ).pack(anchor="w")
        self.mutable_widget(
            ttk.Checkbutton(
                frame,
                variable=self.drive_checked,
                text="Driveの同期完了を確認しました",
                command=self.cloud_uncheck,
            )
        ).pack(anchor="w", pady=8)
        row = ttk.Frame(frame)
        row.pack(fill="x", pady=4)
        self.button(row, "確認して取り込みを許可", self.cloud_confirm, primary=True, side="left")
        self.button(row, "共有ファイルを受信・更新", self.cloud_sync, side="left", padx=6)
        self.button(row, "共有フォルダへ保存して終了", self.cloud_save_exit, side="left")
        ttk.Label(
            frame,
            text="終了後にもDriveの同期完了を確認してから、別PCへ切り替えてください。\n「共有フォルダ保存済み」はクラウドへの送信完了を意味しません。",
            wraplength=990,
        ).pack(anchor="w", pady=8)
        row = ttk.Frame(frame)
        row.pack(fill="x", pady=4)
        self.button(row, "共有解析設定を適用", self.cloud_apply_profile, side="left")
        self.button(row, "現在の解析設定を共有", self.cloud_share_profile, side="left", padx=6)
        self.button(
            row,
            "手順を開く",
            lambda: self.open_path(self.service.path.parent / "docs/multi-pc-sync.md"),
            side="left",
        )
        row = ttk.Frame(frame)
        row.pack(fill="x", pady=4)
        self.button(row, "前PCの終了を確認して引き継ぐ", self.cloud_takeover, side="left")
        self.button(row, "結果未確認の再送停止を解除", self.cloud_acknowledge, side="left", padx=6)
        self.button(row, "このPCの同期を解除", self.cloud_disable, side="left")
        self.cloud_refresh()

    def cloud_refresh(self, result=None):
        cloud = self.service.cloud()
        state = cloud.status()
        binding = self.service._sync_binding(self.service.config())
        if not cloud.confirmed and (
            self._cloud_seen_service is not self.service or self._cloud_seen_binding != binding
        ):
            self.drive_checked.set(False)
        self._cloud_seen_service, self._cloud_seen_binding = self.service, binding
        if not state.get("enabled"):
            text = "未設定。このPCだけで取り込みを使用できます。"
        elif state.get("transport") != "folder":
            text = "旧API方式の設定が残っています。同期を解除して共有フォルダを設定してください。Googleへの接続は行いません。"
        else:
            text = "共有フォルダ：" + state["shared_folder"]
            text += "\n" + (
                "引き継ぎ確認済み・取り込み可能"
                if cloud.confirmed
                else "この起動では引き継ぎ未確認・閲覧のみ"
            )
            text += "　／　クラウド送信完了はDriveで確認してください。"
            text += "\n共有ファイルの最終確認：" + state.get("checked_at", "未確認")
            if state.get("owner") and state["owner"] != state["device_id"]:
                text += "　前PCの処理終了待ち"
            if state.get("pending"):
                text += "　共有フォルダへ未保存の進捗あり（C:に保存済み）"
            shared = state.get("shared_profile", {})
            text += (
                f"\n共有AI：{shared.get('provider', '未設定')} / {shared.get('model') or '未指定'}"
            )
            text += "　受信世代：" + str(state.get("head") or "未保存")[:12]
        if isinstance(result, str):
            text += "\n" + result
        error = self.service.sync_error()
        if error:
            self.cloud_result.set("前回停止した理由：" + error["message"])
        elif self.cloud_result.get().startswith("前回停止した理由："):
            self.cloud_result.set("共有ファイルの受信・更新が完了しました。")
        self.cloud_status.set(text)

    def cloud_done(self, result=None):
        self.cfg = self.service.config()
        self._reload_settings()
        self._load_selection()
        self.reset_library()
        self.cloud_refresh(result)
        if isinstance(result, dict) and result.get("message"):
            self.cloud_result.set(result["message"])
            self.drive_checked.set(bool(result.get("confirmed")))
        self.refresh_history()

    def cloud_browse(self, name):
        if name == "source.bibtex_file":
            value = filedialog.askopenfilename(
                parent=self.root, title="BibTeXファイル", filetypes=[("BibTeX", "*.bib")]
            )
        else:
            value = filedialog.askdirectory(parent=self.root, title="フォルダを選択")
        if value:
            self.cloud_paths[name].set(value)

    def cloud_save_paths(self):
        changes = {k: v.get().strip() or None for k, v in self.cloud_paths.items()}
        self.submit("settings", lambda: save_changes(self.service.path, changes), self.cloud_done)

    def cloud_cli_login(self, provider):
        import os
        import subprocess

        from paperflow.agents.process import resolve_command

        def start():
            if os.name != "nt":
                raise SyncError("CLIログイン画面はWindows版で開いてください")
            cfg = self.service.config()
            command = resolve_command(getattr(cfg.analysis, provider + "_command"))
            args = {"claude": ["auth", "login"], "codex": ["login"], "agy": ["acp", "--login"]}[
                provider
            ]
            subprocess.Popen(
                [command, *args],
                cwd=self.service.path.parent,
                creationflags=subprocess.CREATE_NEW_CONSOLE,
            )
            return "開いたログイン画面で認証を完了してください。"

        self.submit("cloud", start, self.cloud_refresh)

    def cloud_new_data(self):
        if not messagebox.askyesno(
            "空の作業DBへ切替",
            "現在のDB・解析結果は元の場所に残し、新しいC:内の作業フォルダを使います。切り替えますか？",
            parent=self.root,
        ):
            return

        def change():
            import uuid

            cfg = self.service.config()
            if ProgressSync(cfg).enabled:
                raise SyncError("先にこのPCの同期を解除してください")
            new_dir = cfg.storage.data_dir.parent / ("paperflow-pc-" + uuid.uuid4().hex[:8])
            save_changes(
                self.service.path,
                {"storage.data_dir": str(new_dir), "storage.database": str(new_dir / "state.db")},
            )
            self.service._sync_confirmation = None
            return "空の作業DBへ切り替えました。既存の共有フォルダを選択して引き継いでください。"

        self.submit("settings", change, self.cloud_done)

    def cloud_choose_folder(self):
        value = filedialog.askdirectory(
            parent=self.root, title="Drive内の専用共有フォルダ", mustexist=False
        )
        if value:
            self.cloud_folder.set(value)
            self.drive_checked.set(False)
            self.cloud_result.set(
                "選択中："
                + value
                + "\nまだ保存していません。初回は「最初のPC：共有を新規作成」を押してください。"
            )

    def _drive_confirmed(self):
        if not self.drive_checked.get():
            messagebox.showinfo(
                "Driveの同期完了確認",
                "Driveの同期完了を確認し、チェックを入れてください。PC切り替え時は前PCの送信完了も確認してください。",
                parent=self.root,
            )
            return False
        return True

    def cloud_create(self):
        folder, name = self.cloud_folder.get().strip(), self.cloud_name.get().strip()
        if not messagebox.askyesno(
            "共有進捗の作成",
            "このPCのDrive同期が完了していることを確認してください。\n次の専用フォルダへ文献DB・解析JSON・バッチ履歴を作成します：\n"
            + folder
            + "\n\n初回は手順3のチェック操作は不要です。この確認後に保存を開始します。\nPDF・認証情報・稼働中のSQLiteはコピーしません。開始しますか？",
            parent=self.root,
        ):
            return

        self.cloud_result.set("共有データを準備しています：" + folder)
        self.submit(
            "cloud", lambda: self.service.create_progress_folder(folder, name), self.cloud_done
        )

    def cloud_uncheck(self):
        if not self.drive_checked.get():
            self.service._sync_confirmation = None
            self.cloud_refresh()
            self._buttons()

    def cloud_join(self):
        if not self._drive_confirmed():
            return
        folder = self.cloud_folder.get().strip()

        def join():
            cfg = self.service.config()
            transport = FolderTransport(cfg, folder)
            result = ProgressSync(cfg, transport, confirmed=True).connect(transport.workspace())
            self.service._sync_confirmation = self.service._sync_binding(cfg)
            return result

        self.submit("cloud", join, self.cloud_done)

    def cloud_confirm(self):
        if self._drive_confirmed():
            self.submit("cloud", self.service.confirm_progress, self.cloud_done)

    def cloud_sync(self):
        self.submit("cloud", self.service.progress_sync, self.cloud_done)

    def cloud_save_exit(self):
        if self.service.cloud().enabled and not self.service.progress_confirmed():
            messagebox.showinfo(
                "引き継ぎ未確認",
                "Driveの同期完了を確認し、「確認して取り込みを許可」を押してから保存してください。",
                parent=self.root,
            )
            return
        self.close()

    def cloud_takeover(self):
        if messagebox.askyesno(
            "前PCの終了確認",
            "前PCのPaperFlow・AI解析が終了し、両PCのDrive同期が完了していますか？\n結果未確認の論文は自動再送を止めたまま引き継ぎます。",
            parent=self.root,
        ):
            self.submit("cloud", lambda: self.service.cloud().takeover(), self.cloud_done)

    def cloud_acknowledge(self):
        if messagebox.askyesno(
            "再送停止の解除",
            "前PCに未回収の回答がないことを確認しましたか？\n再実行するとAIへの送信・消費量が発生する場合があります。",
            parent=self.root,
        ):
            self.submit("cloud", lambda: self.service.cloud().acknowledge(), self.cloud_done)

    def cloud_disable(self):
        if not messagebox.askyesno(
            "同期解除",
            "このPCの同期を解除します。確認済みの場合は共有フォルダへ保存してから解除します。\n未確認または旧API設定の場合はC:のデータを保持して解除し、共有先へ書き込みません。",
            parent=self.root,
        ):
            return

        def disable():
            cloud = self.service.cloud()
            if cloud.local.get("transport") == "folder" and cloud.confirmed:
                cloud.sync()
                if cloud.local.get("owner"):
                    raise SyncError("処理中のPCがあります。先に終了または引き継ぎを行ってください")
            if cloud.enabled:
                from paperflow.wiki.bundle import json_write

                json_write(directory(cloud.cfg) / "settings-before-disable.json", cloud.local)
            cloud.local["enabled"] = False
            save_settings(cloud.cfg, cloud.local)
            self.service._sync_confirmation = None
            return "このPCの同期を解除しました。C:と共有フォルダのデータは保持しています。"

        self.submit("cloud", disable, self.cloud_done)

    def cloud_share_profile(self):
        if messagebox.askyesno(
            "解析設定の共有",
            "保存済みのProvider・モデル・解析設定を共有します。過去の解析は自動再生成しません。",
            parent=self.root,
        ):
            self.submit("cloud", lambda: self.service.cloud().share_profile(), self.cloud_done)

    def cloud_apply_profile(self):
        def apply():
            cloud = self.service.cloud()
            shared = cloud.local.get("shared_profile")
            if not shared:
                raise SyncError("先に共有ファイルを受信してください")
            cfg = cloud.cfg
            if (
                shared["analysis"]["policy"] == "fulltext_external"
                and not cfg.analysis.allow_external_fulltext
            ):
                raise SyncError(
                    "共有設定はPDF全文解析です。取り込み画面で外部送信の許可を確認して設定を保存してから適用してください"
                )
            changes = {"analysis." + k: v for k, v in shared["analysis"].items()}
            changes.update(
                {
                    "analysis.default_provider": shared["provider"],
                    "analysis.model": shared["model"],
                    "destination.visual_max_items": shared["visuals"],
                }
            )
            path = directory(cfg) / "shared-taxonomy.yaml"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                yaml.safe_dump(dict(topics=shared["taxonomy"]), allow_unicode=True),
                encoding="utf-8",
            )
            changes["analysis.taxonomy_file"] = str(path)
            saved = save_changes(self.service.path, changes)
            self.service.remember(saved.analysis.default_provider, saved.analysis.model)
            return "共有解析設定を適用しました。PCごとのパス・CLI・送信/保存の許可は個別設定のままです。"

        self.submit("cloud", apply, self.cloud_done)
