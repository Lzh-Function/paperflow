"""共有フォルダの初期設定と引き継ぎ。クラウドの完了確認は利用者が行う。"""

import os
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import yaml

from paperflow.gui_config import save_changes
from paperflow.portable import profile, profiles_compatible
from paperflow.progress_sync import ProgressSync, SyncError, directory, save_settings


def profile_description(cfg, shared):
    """許可やローカルパスを含めず、共有対象の違いを表示する。"""
    current = profile(cfg)
    local_ai = f"{current['provider']} / {current['model'] or '未指定'}"
    if not shared:
        return False, "このPCのAI：" + local_ai + "\n共有解析設定はまだ受信していません。"
    shared_ai = f"{shared['provider']} / {shared['model'] or '未指定'}"
    text = f"このPCのAI：{local_ai}　／　共有AI：{shared_ai}"
    if profiles_compatible(shared, current):
        return True, text + "\n解析設定は共有と一致しています。"
    labels = {
        "research_context": "研究分野の説明",
        "policy": "解析方式",
        "require_visual_reading": "図表読解",
        "language": "出力言語",
        "max_chars": "補助テキスト上限",
        "visual_max_pages": "PDFページ上限",
        "visual_max_pdf_mb": "PDF容量上限",
        "visual_max_image_mb": "画像容量上限",
        "prompt_version": "プロンプト版",
    }
    differences = [
        label
        for key, label in labels.items()
        if shared.get("analysis", {}).get(key) != current["analysis"].get(key)
    ]
    differences.extend(
        label
        for key, label in [
            ("provider", "AI"),
            ("model", "モデル"),
            ("taxonomy", "主題タグ辞書"),
            ("visuals", "要約の図表数"),
        ]
        if shared.get(key) != current.get(key)
    )
    if any(shared.get(key) != current.get(key) for key in ["prompt", "schema", "parser"]):
        differences.append("アプリの解析仕様（両PCのPaperFlowを同じ版に更新してください）")
    return False, (
        text
        + "\n異なる項目："
        + "、".join(differences)
        + "\n前PCと同じ設定で続ける →「共有解析設定を適用」。このPCの設定を今後使う →「現在の解析設定を共有」。"
        + "\n適用・共有で過去の要約は再生成されません。PCごとのパス・CLI・送信/保存許可も変わりません。"
    )


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
        self._cloud_selected_folder = self.cloud_folder.get().strip()
        self.cloud_folder.trace_add("write", self.cloud_folder_changed)
        self._cloud_seen_service = None
        self._cloud_seen_binding = None
        self.cloud_result = tk.StringVar(value="")
        self.cloud_next = tk.StringVar()
        self.cloud_profile = tk.StringVar()
        self.cloud_setup_open = tk.BooleanVar(value=not state.get("enabled"))
        self.cloud_advanced_open = tk.BooleanVar(value=False)
        self.cloud_actions = {}
        self.cloud_paths = {}
        ttk.Label(frame, text="PC間同期", style="Title.TLabel").pack(anchor="w")
        ttk.Label(frame, textvariable=self.cloud_next, style="Heading.TLabel", wraplength=990).pack(
            anchor="w", pady=(8, 6)
        )
        ttk.Label(frame, textvariable=self.cloud_status, wraplength=990).pack(anchor="w", pady=4)
        ttk.Label(
            frame,
            text="PCを切り替えるとき：前PCで保存して終了 → 両PCのDrive同期完了を待つ → このPCで確認する",
            wraplength=990,
        ).pack(anchor="w", pady=(8, 4))
        self.mutable_widget(
            ttk.Checkbutton(
                frame,
                variable=self.drive_checked,
                text="Windows右下のDriveで同期完了を確認しました（PC切り替え時は前PCの送信も確認）",
                command=self.cloud_uncheck,
            )
        ).pack(anchor="w", pady=4)
        row = ttk.Frame(frame)
        row.pack(fill="x", pady=4)
        for key, label, command in [
            ("confirm", "確認して取り込みを許可", self.cloud_confirm),
            ("sync", "共有ファイルを受信・更新", self.cloud_sync),
            ("exit", "共有フォルダへ保存して終了", self.cloud_save_exit),
        ]:
            self.cloud_actions[key] = self.button(
                row, label, command, primary=key == "confirm", side="left", padx=(0, 6)
            )
        ttk.Label(
            frame,
            text="受信・更新だけではAI解析は始まりません。Drive確認は毎起動必要です。終了後もDriveの送信完了を待ってください。",
            wraplength=990,
        ).pack(anchor="w", pady=4)
        ttk.Label(frame, textvariable=self.cloud_result, wraplength=990).pack(anchor="w", pady=4)
        ttk.Label(frame, textvariable=self.cloud_profile, wraplength=990).pack(anchor="w", pady=4)
        row = ttk.Frame(frame)
        row.pack(fill="x", pady=4)
        for key, label, command in [
            ("apply", "共有解析設定を適用", self.cloud_apply_profile),
            ("share", "現在の解析設定を共有", self.cloud_share_profile),
        ]:
            self.cloud_actions[key] = self.button(row, label, command, side="left", padx=(0, 6))
        self.button(
            row,
            "手順を開く",
            lambda: self.open_path(self.service.path.parent / "docs/multi-pc-sync.md"),
            side="left",
            during_run=True,
        )
        ttk.Separator(frame).pack(fill="x", pady=12)
        ttk.Checkbutton(
            frame,
            text="初期設定・保存場所の変更を表示",
            variable=self.cloud_setup_open,
            command=self.cloud_toggle_sections,
        ).pack(anchor="w")
        self.cloud_setup_frame = ttk.Frame(frame)
        setup = self.cloud_setup_frame
        ttk.Label(setup, text="1. このPCの保存場所を設定", style="Heading.TLabel").pack(
            anchor="w", pady=(12, 0)
        )
        ttk.Label(
            setup,
            text="1台だけで使う場合は、保存場所とCLIログインを設定して「取り込み」へ進みます。",
            wraplength=990,
        ).pack(anchor="w", pady=4)
        paths = ttk.Frame(setup)
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
        row = ttk.Frame(setup)
        row.pack(fill="x")
        self.button(row, "このPCのパスを保存", self.cloud_save_paths, side="left")
        self.button(row, "Claudeにログイン", lambda: self.cloud_cli_login("claude"), side="left")
        self.button(
            row, "Codexにログイン", lambda: self.cloud_cli_login("codex"), side="left", padx=6
        )
        self.button(row, "agyにログイン", lambda: self.cloud_cli_login("agy"), side="left", padx=6)
        ttk.Label(setup, text="2. Drive内の専用共有フォルダを選ぶ", style="Heading.TLabel").pack(
            anchor="w", pady=(16, 6)
        )
        ttk.Label(
            setup,
            text="両PCのGoogle Driveデスクトップ版で同じフォルダを使います。例：H:\\My Drive\\PaperFlow-Sync\nPaperpile・Vault・C:の作業データとは分離してください。Google CloudやOAuthの設定は不要です。",
            wraplength=990,
        ).pack(anchor="w")
        row = ttk.Frame(setup)
        row.pack(fill="x", pady=8)
        self.mutable_widget(ttk.Entry(row, textvariable=self.cloud_folder)).pack(
            side="left", fill="x", expand=True
        )
        self.button(row, "参照…", self.cloud_choose_folder, side="left", padx=6)
        self.button(
            row, "フォルダを開く", lambda: self.open_path(self.cloud_folder.get()), side="left"
        )
        row = ttk.Frame(setup)
        row.pack(fill="x", pady=4)
        ttk.Label(row, text="共有名").pack(side="left", padx=(0, 8))
        self.mutable_widget(ttk.Entry(row, textvariable=self.cloud_name, width=25)).pack(
            side="left"
        )
        self.cloud_actions["create"] = self.button(
            row, "最初のPC：共有を新規作成", self.cloud_create, side="left", padx=8
        )
        self.cloud_actions["join"] = self.button(
            row, "既存の共有を引き継ぐ／場所を変更", self.cloud_join, side="left"
        )
        ttk.Label(
            setup,
            text="最初の1台だけ新規作成します。2台目・共有フォルダの移動は「既存の共有を引き継ぐ／場所を変更」を使います。\n引き継ぎ前に、上部のDrive確認へチェックを入れてください。",
            wraplength=990,
        ).pack(anchor="w", pady=6)
        self.cloud_advanced_toggle = ttk.Checkbutton(
            frame,
            text="トラブル時の操作を表示",
            variable=self.cloud_advanced_open,
            command=self.cloud_toggle_sections,
        )
        self.cloud_advanced_toggle.pack(anchor="w", pady=(12, 4))
        self.cloud_advanced_frame = ttk.Frame(frame)
        advanced = self.cloud_advanced_frame
        ttk.Label(
            advanced,
            text="通常のPC切り替えには使いません。前PCが異常終了した場合や、別々の文献DBを作った場合に使います。",
            wraplength=990,
        ).pack(anchor="w", pady=6)
        row = ttk.Frame(advanced)
        row.pack(fill="x", pady=4)
        self.cloud_actions["takeover"] = self.button(
            row, "前PCの終了を確認して引き継ぐ", self.cloud_takeover, side="left"
        )
        self.cloud_actions["acknowledge"] = self.button(
            row, "結果未確認の再送停止を解除", self.cloud_acknowledge, side="left", padx=6
        )
        row = ttk.Frame(advanced)
        row.pack(fill="x", pady=4)
        self.button(row, "空の作業DBへ切替", self.cloud_new_data, side="left")
        self.button(row, "このPCの同期を解除", self.cloud_disable, side="left", padx=6)
        self.cloud_toggle_sections()
        self.cloud_refresh()

    def cloud_toggle_sections(self):
        if self.cloud_setup_open.get():
            self.cloud_setup_frame.pack(fill="x", before=self.cloud_advanced_toggle)
        else:
            self.cloud_setup_frame.pack_forget()
        if self.cloud_advanced_open.get():
            self.cloud_advanced_frame.pack(fill="x")
        else:
            self.cloud_advanced_frame.pack_forget()

    def cloud_folder_is_saved(self, state):
        selected, saved = self.cloud_folder.get().strip(), state.get("shared_folder", "")
        return bool(selected and saved) and os.path.normcase(
            os.path.abspath(selected)
        ) == os.path.normcase(os.path.abspath(saved))

    def cloud_update_buttons(self):
        state = getattr(self, "_cloud_state", {})
        connected = state.get("enabled") and state.get("transport") == "folder"
        same = connected and self.cloud_folder_is_saved(state)
        confirmed = same and self.service.progress_confirmed()
        other_owner = state.get("owner") and state["owner"] != state.get("device_id")
        permissions = dict(
            create=bool(self.cloud_folder.get().strip())
            and (not state.get("enabled") or (connected and not state.get("head"))),
            join=bool(self.cloud_folder.get().strip()) and self.drive_checked.get(),
            confirm=same and self.drive_checked.get(),
            sync=same,
            exit=confirmed,
            apply=confirmed and bool(state.get("shared_profile")),
            share=confirmed,
            takeover=confirmed and other_owner,
            acknowledge=confirmed,
        )
        for key, button in self.cloud_actions.items():
            button.configure(state="normal" if permissions[key] and not self.busy else "disabled")

    def cloud_refresh(self, result=None):
        cloud = self.service.cloud()
        state, cfg = cloud.status(), cloud.cfg
        binding = self.service._sync_binding(cfg)
        new_service = self._cloud_seen_service is not self.service
        if self._cloud_seen_service is not None and new_service:
            for name, var in self.cloud_paths.items():
                section, key = name.split(".")
                var.set(str(getattr(getattr(cfg, section), key) or ""))
            # trace callbacks run only after the service switch is recorded below.
            self._cloud_seen_service = self.service
            self.cloud_folder.set(
                state.get("shared_folder")
                or str(cfg.destination.vault_dir.parent / "PaperFlow-Sync")
            )
            self.cloud_setup_open.set(not state.get("enabled"))
            self.cloud_result.set("")
            self.cloud_toggle_sections()
        if not cloud.confirmed and (new_service or self._cloud_seen_binding != binding):
            self.drive_checked.set(False)
        self._cloud_seen_service, self._cloud_seen_binding = self.service, binding
        self._cloud_state = state
        matches, description = profile_description(cfg, state.get("shared_profile"))
        self._cloud_profile_matches = matches
        self.cloud_profile.set(description if state.get("enabled") else "")
        error = self.service.sync_error()
        if not state.get("enabled"):
            text = "未設定。このPCだけで取り込みを使用できます。"
            next_step = (
                "複数PCで使う場合は「初期設定・保存場所の変更」で共有フォルダを設定してください。"
            )
        elif state.get("transport") != "folder":
            text = "旧API方式の設定が残っています。Googleへの接続は行いません。"
            next_step = "「トラブル時の操作」で同期を解除し、共有フォルダを設定してください。"
        else:
            same = self.cloud_folder_is_saved(state)
            other_owner = state.get("owner") and state["owner"] != state.get("device_id")
            ready = (
                same
                and cloud.confirmed
                and matches
                and not other_owner
                and not error
                and not state.get("pending")
            )
            text = "使用中の共有フォルダ：" + state["shared_folder"]
            text += "\n" + (
                "引き継ぎ確認済み・取り込み可能" if ready else "取り込み前の確認が必要です"
            )
            text += "　／　共有ファイルの最終確認：" + state.get("checked_at", "未確認")
            if not same:
                next_step = "共有先の変更は未保存です。「既存の共有を引き継ぐ／場所を変更」を押してください。"
            elif not cloud.confirmed:
                next_step = "Drive完了を確認 → チェックを入れる →「確認して取り込みを許可」を押してください。"
            elif error:
                next_step = "同期を完了できませんでした。下の理由を確認し、解消後に「共有ファイルを受信・更新」を押してください。"
            elif other_owner:
                next_step = "前PCが処理中です。終了済みなら「トラブル時の操作」で終了を確認して引き継いでください。"
            elif not matches:
                next_step = (
                    "解析設定が共有と異なります。下の差分を確認し、使う設定を選んでください。"
                )
            elif state.get("pending"):
                next_step = "このPCに共有先へ未保存の進捗があります。「共有ファイルを受信・更新」で保存してください。"
            else:
                next_step = "引き継ぎ確認済み。「取り込み」へ進めます。終わったら「共有フォルダへ保存して終了」。"
        if isinstance(result, str):
            text += "\n" + result
        if error:
            self.cloud_result.set("前回停止した理由：" + error["message"])
        elif self.cloud_result.get().startswith("前回停止した理由："):
            self.cloud_result.set("共有ファイルの受信・更新が完了しました。")
        self.cloud_status.set(text)
        self.cloud_next.set(next_step)
        self.cloud_update_buttons()

    def _require_saved_folder(self):
        state = self.service.cloud().status()
        if not state.get("enabled") or state.get("transport") != "folder":
            messagebox.showinfo(
                "共有フォルダの設定",
                "先に共有を新規作成するか、既存の共有を引き継いでください。",
                parent=self.root,
            )
            return False
        if not self.cloud_folder_is_saved(state):
            messagebox.showinfo(
                "共有先の変更は未保存です",
                "選択したフォルダはまだ使用していません。「既存の共有を引き継ぐ／場所を変更」を押してください。",
                parent=self.root,
            )
            return False
        return True

    def cloud_done(self, result=None):
        was_enabled = self._cloud_state.get("enabled")
        self.cfg = self.service.config()
        self._reload_settings()
        self._load_selection()
        self.reset_library()
        if isinstance(result, dict) and "confirmed" in result:
            self.drive_checked.set(bool(result["confirmed"]))
        self.cloud_refresh(result)
        if not was_enabled and self._cloud_state.get("enabled"):
            self.cloud_setup_open.set(False)
            self.cloud_toggle_sections()
        if isinstance(result, dict) and result.get("message"):
            self.cloud_result.set(result["message"])
        self._buttons()
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

    def cloud_folder_changed(self, *args):
        selected = self.cloud_folder.get().strip()
        if selected != self._cloud_selected_folder:
            self._cloud_selected_folder = selected
            self.drive_checked.set(False)
            self.service._sync_confirmation = None
            if hasattr(self, "cloud_actions"):
                self.cloud_next.set(
                    "共有先の変更は未保存です。「既存の共有を引き継ぐ／場所を変更」を押してください。"
                    if self._cloud_state.get("enabled")
                    else "フォルダはまだ保存していません。最初のPCは「最初のPC：共有を新規作成」、2台目は「既存の共有を引き継ぐ／場所を変更」を押してください。"
                )
                self.cloud_update_buttons()
                if hasattr(self, "start_button"):
                    self._buttons()

    def cloud_choose_folder(self):
        value = filedialog.askdirectory(
            parent=self.root, title="Drive内の専用共有フォルダ", mustexist=False
        )
        if value:
            self.cloud_folder.set(value)
            self.drive_checked.set(False)
            self.service._sync_confirmation = None
            self.cloud_result.set(
                "選択中："
                + value
                + "\nまだ保存していません。同じ共有の場所変更や別PCでの利用は「既存の共有を引き継ぐ／場所を変更」を押してください。新しい共有を始める場合だけ新規作成します。"
            )

    def _drive_confirmed(self):
        if not self.drive_checked.get():
            messagebox.showinfo(
                "Driveの同期完了確認",
                "Windows右下のGoogle Driveで同期完了を確認し、この画面上部の「Windows右下のDriveで同期完了を確認しました」にチェックを入れてください。PC切り替え時は前PCの送信完了も確認します。",
                parent=self.root,
            )
            return False
        return True

    def cloud_create(self):
        folder, name = self.cloud_folder.get().strip(), self.cloud_name.get().strip()
        if not folder:
            messagebox.showinfo(
                "共有フォルダの設定", "共有フォルダを選択してください。", parent=self.root
            )
            return
        if not messagebox.askyesno(
            "共有進捗の作成",
            "このPCのDrive同期が完了していることを確認してください。\n次の専用フォルダへ文献DB・解析JSON・バッチ履歴を作成します：\n"
            + folder
            + "\n\n初回は画面上部のチェック操作は不要です。この確認後に保存を開始します。\nPDF・認証情報・稼働中のSQLiteはコピーしません。開始しますか？",
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

        self.submit("cloud", lambda: self.service.join_progress_folder(folder), self.cloud_done)

    def cloud_confirm(self):
        if self._require_saved_folder() and self._drive_confirmed():
            self.submit("cloud", self.service.confirm_progress, self.cloud_done)

    def cloud_sync(self):
        if self._require_saved_folder():
            self.submit("cloud", self.service.progress_sync, self.cloud_done)

    def cloud_save_exit(self):
        if not self._require_saved_folder():
            return
        if self.service.cloud().enabled and not self.service.progress_confirmed():
            messagebox.showinfo(
                "引き継ぎ未確認",
                "Driveの同期完了を確認し、「確認して取り込みを許可」を押してから保存してください。",
                parent=self.root,
            )
            return
        self.close()

    def cloud_takeover(self):
        if not self._require_saved_folder():
            return
        if messagebox.askyesno(
            "前PCの終了確認",
            "前PCのPaperFlow・AI解析が終了し、両PCのDrive同期が完了していますか？\n結果未確認の論文は自動再送を止めたまま引き継ぎます。",
            parent=self.root,
        ):
            self.submit("cloud", lambda: self.service.progress_action("takeover"), self.cloud_done)

    def cloud_acknowledge(self):
        if not self._require_saved_folder():
            return
        if messagebox.askyesno(
            "再送停止の解除",
            "前PCに未回収の回答がないことを確認しましたか？\n再実行するとAIへの送信・消費量が発生する場合があります。",
            parent=self.root,
        ):
            self.submit(
                "cloud", lambda: self.service.progress_action("acknowledge"), self.cloud_done
            )

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
        if not self._require_saved_folder():
            return
        if messagebox.askyesno(
            "解析設定の共有",
            "保存済みのProvider・モデル・解析設定を共有します。過去の解析は自動再生成しません。",
            parent=self.root,
        ):
            self.submit(
                "cloud", lambda: self.service.progress_action("share_profile"), self.cloud_done
            )

    def cloud_apply_profile(self):
        if not self._require_saved_folder():
            return

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
            self.service.progress_sync()
            return "共有解析設定を適用しました。PCごとのパス・CLI・送信/保存の許可は個別設定のままです。"

        self.submit("cloud", apply, self.cloud_done)
