"""Windowsデスクトップ版。処理はワーカーで実行し、画面更新はメインスレッドに限定する。"""

import argparse
import logging
import os
import queue
import sys
import time
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from importlib.resources import files
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from paperflow.config import SettingsError
from paperflow.gui_config import config_values, save_changes
from paperflow.gui_recovery import RecoveryPanel
from paperflow.gui_service import GuiService
from paperflow.gui_sync import SyncPanel
from paperflow.library import FILTERS, filter_rows

PROVIDERS = {
    "Claude Code CLI": "claude",
    "Codex CLI": "codex",
    "Antigravity CLI（agy）": "agy",
    "Fake（オフライン試験）": "fake",
}
STATES = {
    "not_started": "未実行",
    "running": "実行中",
    "complete": "完了",
    "partial": "未完了あり",
    "stopped": "停止",
    "interrupted": "中断",
    "blocked": "要対応・停止",
}
SECTIONS = {
    "source": "入力元",
    "destination": "Obsidian",
    "storage": "作業データ",
    "analysis": "AI・読解",
    "sync": "処理・監視",
}
LABELS = {
    "paperpile_dir": "Paperpileフォルダ",
    "bibtex_file": "BibTeXファイル",
    "recurse": "サブフォルダも検索",
    "read_only": "原本を読み取り専用で扱う",
    "vault_dir": "Obsidian Vault",
    "namespace": "旧形式の出力名",
    "layout": "保存形式",
    "copy_pdf": "PDFをVaultへ凍結コピー",
    "include_visuals": "要約に図表を添付",
    "visual_max_items": "添付する図表の上限（件）",
    "allow_vault_write": "Vaultへの保存を許可",
    "require_wiki_marker": "Vaultマーカーを確認",
    "data_dir": "作業フォルダ",
    "database": "処理履歴DB",
    "research_context": "研究分野・関心・評価方針",
    "default_provider": "既定のProvider",
    "policy": "解析対象",
    "allow_external_fulltext": "PDFの外部送信を許可",
    "require_visual_reading": "PDF・図表の視覚読解を必須にする",
    "visual_max_pages": "PDFのページ上限",
    "visual_max_pdf_mb": "PDF容量上限（MiB）",
    "visual_max_image_mb": "全ページ画像の容量上限（MiB）",
    "language": "生成言語",
    "max_chars": "補助テキストの文字数上限",
    "max_concurrency": "同時解析数",
    "timeout_seconds": "1論文の待ち時間上限（秒）",
    "max_retries": "自動再試行の上限（回）",
    "max_cli_calls_per_run": "1区切りのCLI解析上限（回）",
    "retry_base_seconds": "再試行の待ち時間（秒）",
    "taxonomy_file": "主題分類の語彙ファイル",
    "prompt_version": "解析指示の版",
    "model": "既定モデルID（空欄で未指定）",
    "claude_command": "Claude CLI",
    "codex_command": "Codex CLI",
    "agy_command": "Antigravity CLI（agy）",
    "max_budget_usd": "Claudeの1論文予算（USD）",
    "max_output_bytes": "CLI応答の容量上限（バイト）",
    "interval_seconds": "監視の間隔（秒）",
    "stable_scan_count": "PDFの安定確認回数",
    "max_papers_per_run": "1区切りの論文本数",
}
CHOICES = {
    "default_provider": {value: label for label, value in PROVIDERS.items()},
    "policy": {"abstract_only": "抄録のみ", "fulltext_external": "PDF全文・図表"},
    "layout": {"llm_wiki": "LLM-Wiki", "paperflow": "PaperFlow単独"},
}
FIXED = {"namespace", "read_only", "language", "max_concurrency", "prompt_version"}


class ScrollFrame(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        self.canvas = tk.Canvas(self, bg="white", highlightthickness=0)
        bar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas, style="Card.TFrame", padding=18)
        window = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind(
            "<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        )
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(window, width=e.width))
        self.canvas.configure(yscrollcommand=bar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")

    def _wheel(self, event):
        widget = self.winfo_containing(event.x_root, event.y_root)
        while widget:
            if widget is self:
                self.canvas.yview_scroll(-int(event.delta / 120), "units")
                return
            widget = getattr(widget, "master", None)


def comments(path):
    result = {}
    section = ""
    pending = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if line.startswith("  # "):
            pending.append(line[4:])
            continue
        if line and not line.startswith(" ") and line.endswith(":"):
            section = line[:-1]
        if line.startswith("  ") and not line.startswith("    ") and ":" in line:
            key = line.strip().split(":", 1)[0]
            result[section + "." + key] = " ".join(pending)
        if line.strip():
            pending = []
    return result


class PaperFlowApp(SyncPanel, RecoveryPanel):
    def __init__(self, root, config_path, *, auto_load=True):
        self.root = root
        self.service = GuiService(config_path)
        self.cfg = self.service.config()
        self.events = queue.Queue()
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.reader = ThreadPoolExecutor(max_workers=1, thread_name_prefix="paperflow-report")
        self.library_reader = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="paperflow-library"
        )
        self.library_pending = False
        self.library_token = 0
        self.library_again = False
        self.library_reconcile_again = False
        self.library_loaded = False
        self.library_rows = []
        self.library_sort_key = "title"
        self.library_sort_reverse = False
        self.read_pending = False
        self.viewed_path = None
        self.view_running = False
        self.busy = False
        self._closed = False
        self.action = None
        self.plan_path = None
        self.plan = None
        self.paper_states = {}
        self.registered_count = 0
        self.close_when_idle = False
        self.mutable = []
        self.model_choices = {}
        self.fields = {}
        self.job_started = 0
        self.root.title("PaperFlow")
        self.root.geometry("1160x860")
        self.root.minsize(980, 720)
        self.root.configure(background="#f3f6f4")
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.report_callback_exception = self.callback_error
        self.style = ttk.Style()
        self.style.theme_use("clam")
        self.style.configure(
            ".", font=("Yu Gothic UI", 10), background="#f3f6f4", foreground="#243b31"
        )
        self.style.configure("TFrame", background="#f3f6f4")
        self.style.configure("Card.TFrame", background="white")
        self.style.configure("Card.TLabel", background="white")
        self.style.configure("Title.TLabel", font=("Yu Gothic UI", 24, "bold"))
        self.style.configure(
            "Heading.TLabel", font=("Yu Gothic UI", 12, "bold"), background="white"
        )
        self.style.configure("Muted.TLabel", foreground="#687a70", font=("Yu Gothic UI", 9))
        self.style.configure(
            "Hint.TLabel", foreground="#687a70", font=("Yu Gothic UI", 9), background="white"
        )
        self.style.configure("TButton", padding=(12, 7))
        self.style.configure(
            "Primary.TButton",
            background="#246b4d",
            foreground="white",
            font=("Yu Gothic UI", 10, "bold"),
        )
        self.style.map(
            "Primary.TButton",
            background=[("disabled", "#cbd7d0"), ("active", "#1a523b")],
            foreground=[("disabled", "#65766c")],
        )
        self.style.configure(
            "Treeview", background="white", fieldbackground="white", rowheight=30, borderwidth=0
        )
        self.style.configure("Treeview.Heading", font=("Yu Gothic UI", 10, "bold"), padding=6)
        self.style.map(
            "Treeview", background=[("selected", "#dceee3")], foreground=[("selected", "#163c29")]
        )
        self.style.configure("TNotebook", tabmargins=(0, 5, 0, 0))
        self.style.configure("TNotebook.Tab", padding=(22, 10))
        try:
            self.icon = tk.PhotoImage(
                file=str(files("paperflow.resources").joinpath("paperflow.png"))
            )
            self.root.iconphoto(True, self.icon)
        except (tk.TclError, FileNotFoundError):
            pass
        self.provider = tk.StringVar()
        self.model = tk.StringVar()
        self.mode = tk.StringVar(value="all")
        self.count = tk.StringVar()
        self.state_text = tk.StringVar(
            value="対象一覧を作成すると、選ばれた論文がここに表示されます。"
        )
        self.status = tk.StringVar(value="待機中")
        self.permission = tk.StringVar()
        self.external_allowed = tk.BooleanVar(value=self.cfg.analysis.allow_external_fulltext)
        self.vault_allowed = tk.BooleanVar(value=self.cfg.destination.allow_vault_write)
        self.stats = tk.StringVar(value="対象 — 本　・　登録済み — 本")
        self.plan_label = tk.StringVar(value="対象一覧：未選択")
        self.config_label = tk.StringVar(value=self.service.path.name)
        self._build()
        self.root.bind_all("<MouseWheel>", self._settings_wheel)
        self._load_selection()
        self._reload_settings()
        self._refresh_permission()
        self.root.after(80, self._drain)
        self.root.after(2000, self._poll_report)
        self.root.after(3000, self._poll_library)
        if auto_load:
            self.root.after(200, self.refresh_history)
            if (
                __import__("paperflow.progress_sync", fromlist=["settings"])
                .settings(self.cfg)
                .get("enabled")
            ):
                self.root.after(250, self.cloud_sync)

    def mutable_widget(self, widget, state="normal"):
        self.mutable.append((widget, state))
        return widget

    def button(self, parent, text, command, *, primary=False, during_run=False, **pack):
        b = ttk.Button(
            parent,
            text=text,
            command=command,
            style="Primary.TButton" if primary else "TButton",
        )
        if not during_run:
            self.mutable_widget(b)
        b.pack(**pack)
        return b

    def _build(self):
        head = ttk.Frame(self.root, padding=(25, 18, 25, 8))
        head.pack(fill="x")
        ttk.Label(head, text="PaperFlow", style="Title.TLabel").pack(side="left")
        ttk.Label(head, text="Paperpile → Obsidian", style="Muted.TLabel", padding=(16, 10)).pack(
            side="left"
        )
        self.button(head, "設定ファイルを切替", self.choose_config, side="right")
        ttk.Label(head, textvariable=self.config_label, style="Muted.TLabel", padding=10).pack(
            side="right"
        )
        self.tabs = ttk.Notebook(self.root)
        self.tabs.pack(fill="both", expand=True, padx=24, pady=(0, 12))
        self.import_tab = ttk.Frame(self.tabs, padding=(0, 16))
        self.library_tab = ttk.Frame(self.tabs, padding=16)
        self.history_tab = ttk.Frame(self.tabs, padding=16)
        self.recovery_tab = ttk.Frame(self.tabs, padding=16)
        self.settings_tab = ttk.Frame(self.tabs, padding=(0, 12))
        self.cloud_tab = ttk.Frame(self.tabs, padding=16)
        for frame, name in [
            (self.import_tab, "取り込み"),
            (self.library_tab, "論文一覧"),
            (self.history_tab, "履歴・消費量"),
            (self.recovery_tab, "復旧・後処理"),
            (self.settings_tab, "設定"),
            (self.cloud_tab, "PC間同期"),
        ]:
            self.tabs.add(frame, text=name)
        self._build_sync()
        self.tabs.bind("<<NotebookTabChanged>>", self._tab_changed)
        card = ttk.Frame(self.import_tab, style="Card.TFrame", padding=16)
        card.pack(fill="x")
        ttk.Label(card, text="AIとモデル", style="Heading.TLabel").grid(
            row=0, column=0, sticky="w", columnspan=4, pady=(0, 8)
        )
        self.provider_box = self.mutable_widget(
            ttk.Combobox(
                card, textvariable=self.provider, values=list(PROVIDERS), state="readonly", width=23
            ),
            "readonly",
        )
        self.provider_box.grid(row=1, column=0, sticky="ew", padx=(0, 10))
        self.provider_box.bind("<<ComboboxSelected>>", lambda e: self.provider_changed())
        self.model_box = self.mutable_widget(ttk.Combobox(card, textvariable=self.model, width=36))
        self.model_box.grid(row=1, column=1, sticky="ew", padx=(0, 10))
        b = self.mutable_widget(
            ttk.Button(card, text="モデル一覧を更新", command=self.refresh_models)
        )
        b.grid(row=1, column=2, padx=(0, 8))
        b = self.mutable_widget(ttk.Button(card, text="選択を記憶", command=self.remember_model))
        b.grid(row=1, column=3)
        ttk.Label(
            card, text="モデル欄が空ならCLIの既定モデルを使用します。", style="Hint.TLabel"
        ).grid(row=2, column=1, columnspan=3, sticky="w", pady=(7, 0))
        self.source_label = ttk.Label(card, text="", style="Hint.TLabel", wraplength=1020)
        self.source_label.grid(row=3, column=0, columnspan=4, sticky="w", pady=(12, 0))
        consent = ttk.Frame(card, style="Card.TFrame")
        consent.grid(row=4, column=0, columnspan=4, sticky="w", pady=(10, 0))
        for text, variable in [
            ("PDFを外部送信して解析する", self.external_allowed),
            ("Obsidianへ保存する", self.vault_allowed),
        ]:
            self.mutable_widget(ttk.Checkbutton(consent, text=text, variable=variable)).pack(
                side="left", padx=(0, 18)
            )
        self.button(consent, "設定を保存", self.save_quick_settings, side="left")
        ttk.Label(card, textvariable=self.permission, style="Hint.TLabel").grid(
            row=5, column=0, columnspan=4, sticky="w", pady=(6, 0)
        )
        card.columnconfigure(1, weight=1)
        selection = ttk.Frame(self.import_tab, padding=(0, 14))
        selection.pack(fill="x")
        for value, label in [
            ("all", "未取り込みを全件"),
            ("count", "追加する本数"),
            ("total", "累計の目標"),
        ]:
            b = self.mutable_widget(
                ttk.Radiobutton(
                    selection,
                    text=label,
                    variable=self.mode,
                    value=value,
                    command=self.mode_changed,
                )
            )
            b.pack(side="left", padx=(0, 18))
        self.count_entry = self.mutable_widget(
            ttk.Entry(selection, textvariable=self.count, width=8)
        )
        self.count_entry.pack(side="left")
        ttk.Label(selection, text="本").pack(side="left", padx=5)
        self.button(selection, "対象一覧を作成", self.prepare_plan, primary=True, side="right")
        self.button(selection, "保存済みの一覧を開く", self.choose_plan, side="right", padx=8)
        info = ttk.Frame(self.import_tab)
        info.pack(fill="x", pady=(0, 8))
        ttk.Label(info, textvariable=self.stats, font=("Yu Gothic UI", 13, "bold")).pack(
            side="left"
        )
        ttk.Label(
            self.import_tab, textvariable=self.state_text, style="Muted.TLabel", wraplength=1040
        ).pack(anchor="w", pady=(0, 8))
        area = ttk.Frame(self.import_tab)
        area.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(
            area,
            columns=("title", "year", "pages", "state"),
            show="headings",
            selectmode="browse",
            height=3,
        )
        for key, label, width in [
            ("title", "論文", 650),
            ("year", "年", 65),
            ("pages", "ページ", 75),
            ("state", "状態", 150),
        ]:
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, minwidth=50, stretch=key == "title")
        bar = ttk.Scrollbar(area, command=self.tree.yview)
        self.tree.configure(yscrollcommand=bar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        bottom = ttk.Frame(self.import_tab)
        bottom.pack(side="bottom", fill="x", before=area)
        ttk.Label(bottom, textvariable=self.plan_label, style="Muted.TLabel", wraplength=1020).pack(
            anchor="w", pady=(8, 4)
        )
        self.progress = ttk.Progressbar(bottom, mode="determinate")
        self.progress.pack(fill="x", pady=6)
        actions = ttk.Frame(bottom)
        actions.pack(fill="x", pady=(8, 0))
        self.start_button = ttk.Button(
            actions, text="要約して保存", style="Primary.TButton", command=self.start
        )
        self.start_button.pack(side="right")
        self.stop_button = ttk.Button(
            actions, text="現在の論文の完了後に停止", command=self.stop, state="disabled"
        )
        self.stop_button.pack(side="right", padx=10)
        self.button(actions, "要約フォルダを開く", self.open_notes, during_run=True, side="left")
        self.report_button = self.button(
            actions, "レポートを開く", self.open_report, during_run=True, side="left", padx=8
        )
        self._build_library()
        self._build_recovery()
        self._build_history()
        self._build_settings()
        foot = ttk.Frame(self.root, padding=(25, 0, 25, 14))
        foot.pack(side="bottom", fill="x", before=self.tabs)
        ttk.Label(foot, textvariable=self.status, style="Muted.TLabel").pack(side="left")
        self.elapsed_label = ttk.Label(foot, text="", style="Muted.TLabel")
        self.elapsed_label.pack(side="right")
        self.mode_changed()
        self._buttons()

    def _build_library(self):
        top = ttk.Frame(self.library_tab)
        top.pack(fill="x")
        self.library_sync_button = self.button(
            top, "Paperpileと同期", self.sync_library, primary=True, side="right"
        )
        self.button(
            top,
            "保存状況を照合",
            lambda: self.refresh_library(reconcile=True),
            during_run=True,
            side="right",
            padx=8,
        )
        self.library_refresh_button = self.button(
            top, "表示を更新", self.refresh_library, during_run=True, side="right"
        )
        ttk.Label(top, text="論文データベース", font=("Yu Gothic UI", 13, "bold")).pack(side="left")
        ttk.Label(
            self.library_tab,
            text="Google Driveの同期後に「Paperpileと同期」を押すと、新着や削除を反映します。AI解析は始まりません。",
            style="Muted.TLabel",
            wraplength=1040,
        ).pack(anchor="w", pady=(7, 12))
        filters = ttk.Frame(self.library_tab)
        filters.pack(fill="x")
        ttk.Label(filters, text="検索").pack(side="left")
        self.library_query = tk.StringVar()
        self.library_search = ttk.Entry(filters, textvariable=self.library_query)
        self.library_search.pack(side="left", fill="x", expand=True, padx=(8, 16))
        self.library_search.bind("<KeyRelease>", lambda e: self.filter_library())
        ttk.Label(filters, text="状態").pack(side="left")
        self.library_filter = tk.StringVar(value="すべて")
        self.library_filter_box = ttk.Combobox(
            filters,
            textvariable=self.library_filter,
            values=list(FILTERS),
            state="readonly",
            width=29,
        )
        self.library_filter_box.pack(side="left", padx=(8, 0))
        self.library_filter_box.bind("<<ComboboxSelected>>", lambda e: self.filter_library())
        self.library_count = tk.StringVar(value="一覧を読み込むと件数が表示されます。")
        ttk.Label(self.library_tab, textvariable=self.library_count, wraplength=1040).pack(
            anchor="w", pady=(12, 5)
        )
        self.library_info = tk.StringVar(
            value="取り込み済みは、要約の生成だけでなくObsidianへの保存を確認した文献です。"
        )
        ttk.Label(
            self.library_tab, textvariable=self.library_info, style="Muted.TLabel", wraplength=1040
        ).pack(anchor="w", pady=(0, 8))
        pane = ttk.Panedwindow(self.library_tab, orient="vertical")
        pane.pack(fill="both", expand=True)
        grid = ttk.Frame(pane)
        pane.add(grid, weight=3)
        self.library_tree = ttk.Treeview(
            grid,
            columns=("status", "title", "year", "authors", "pdf_status", "model", "last_attempt"),
            show="headings",
            selectmode="extended",
            height=6,
        )
        self.library_headings = {}
        for key, title, width in [
            ("status", "状態", 125),
            ("title", "論文", 420),
            ("year", "年", 55),
            ("authors", "著者", 165),
            ("pdf_status", "PDF", 80),
            ("model", "モデル", 155),
            ("last_attempt", "最終処理", 150),
        ]:
            self.library_headings[key] = title
            self.library_tree.heading(
                key, text=title, command=lambda field=key: self.sort_library(field)
            )
            self.library_tree.column(key, width=width, minwidth=50, stretch=key == "title")
        vertical = ttk.Scrollbar(grid, command=self.library_tree.yview)
        horizontal = ttk.Scrollbar(grid, orient="horizontal", command=self.library_tree.xview)
        self.library_tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.library_tree.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        grid.rowconfigure(0, weight=1)
        grid.columnconfigure(0, weight=1)
        self.library_tree.tag_configure("failed", foreground="#a33d35")
        self.library_tree.tag_configure("processed", foreground="#246b4d")
        self.library_tree.tag_configure("removed", foreground="#7c8580")
        self.library_tree.bind("<<TreeviewSelect>>", lambda e: self.library_details())
        detail = ttk.Frame(pane, padding=(0, 10, 0, 0))
        pane.add(detail, weight=2)
        buttons = ttk.Frame(detail)
        buttons.pack(fill="x", pady=(0, 6))
        ttk.Label(buttons, text="選択した論文", font=("Yu Gothic UI", 11, "bold")).pack(side="left")
        self.library_pdf_button = self.button(
            buttons,
            "PDFを開く",
            lambda: self.open_library_file("pdf_path"),
            during_run=True,
            side="right",
        )
        self.library_note_button = self.button(
            buttons,
            "要約ノートを開く",
            lambda: self.open_library_file("note_path"),
            during_run=True,
            side="right",
            padx=8,
        )
        self.summary_button = self.button(
            buttons,
            "選択論文を再要約・版を比較",
            self.open_summary_versions,
            side="right",
            padx=8,
        )
        self.revalidate_button = self.button(
            buttons, "保存済み回答を再検証", self.revalidate_responses, side="right", padx=8
        )
        body = ttk.Frame(detail)
        body.pack(fill="both", expand=True)
        self.library_detail = tk.Text(
            body,
            height=7,
            wrap="word",
            font=("Yu Gothic UI", 10),
            relief="flat",
            background="white",
            foreground="#243b31",
            padx=12,
            pady=8,
        )
        bar = ttk.Scrollbar(body, command=self.library_detail.yview)
        self.library_detail.configure(yscrollcommand=bar.set)
        self.library_detail.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        self.library_details()

    @staticmethod
    def display_time(value):
        if not value:
            return "—"
        try:
            return datetime.fromisoformat(value).astimezone().strftime("%Y-%m-%d %H:%M")
        except ValueError:
            return value

    def _tab_changed(self, event=None):
        if hasattr(self, "library_tree") and self.tabs.select() == str(self.library_tab):
            self.refresh_library()
        elif hasattr(self, "recovery_tree") and self.tabs.select() == str(self.recovery_tab):
            self.refresh_recovery()

    def refresh_library(self, *, reconcile=False):
        if self._closed:
            return
        if self.library_pending:
            self.library_again = True
            self.library_reconcile_again |= reconcile
            return
        self.library_pending = True
        self.library_token += 1
        token, service = self.library_token, self.service
        if not self.library_loaded or reconcile:
            self.library_info.set(
                "論文一覧を読み込んでいます。初回はVaultの既登録文献も照合します。"
            )

        def worker():
            try:
                self.events.put(("library_done", token, service.library(reconcile=reconcile)))
            except Exception as exc:
                self.events.put(("library_error", token, str(exc)))

        self.library_reader.submit(worker)

    def _poll_library(self):
        if self._closed:
            return
        if self.tabs.select() == str(self.library_tab):
            self.refresh_library()
        elif hasattr(self, "recovery_tree") and self.tabs.select() == str(self.recovery_tab):
            self.refresh_recovery()
        self.root.after(3000, self._poll_library)

    def reset_library(self):
        self.library_token += 1
        self.library_pending = False
        self.library_again = self.library_reconcile_again = False
        self.library_loaded = False
        self.library_rows = []
        self.library_by_id = {}
        self.library_counts = {}
        self.library_tree.delete(*self.library_tree.get_children())
        self.library_count.set("一覧は未読み込みです。")
        self.library_info.set("論文一覧を開くとデータを読み込みます。")
        self.library_details()

    def show_library(self, data):
        self.library_loaded = True
        self.library_rows = data["rows"]
        self.library_counts = data["counts"]
        self.filter_library()
        self.library_info.set(
            data.get("warning")
            or f"表示更新 {self.display_time(data['updated_at'])}　／　Paperpile同期 {self.display_time(data['last_sync'])}　／　Vault照合 {self.display_time(data.get('registry_checked_at'))}"
        )

    def filter_library(self):
        selected = self.library_tree.selection()
        focused = self.library_tree.focus()
        yview = self.library_tree.yview()
        xview = self.library_tree.xview()
        rows = filter_rows(
            self.library_rows, self.library_query.get(), FILTERS[self.library_filter.get()]
        )
        key = self.library_sort_key
        rows.sort(
            key=lambda row: (
                int(row[key])
                if key == "year" and str(row[key]).isdigit()
                else -1
                if key == "year"
                else str(row[key]).casefold()
            ),
            reverse=self.library_sort_reverse,
        )
        self.library_tree.delete(*self.library_tree.get_children())
        self.library_by_id = {r["paper_id"]: r for r in self.library_rows}
        for row in rows:
            self.library_tree.insert(
                "",
                "end",
                iid=row["paper_id"],
                values=(
                    row["status"],
                    row["title"],
                    row["year"],
                    "; ".join(row["authors"]),
                    row["pdf_status"],
                    row["model"] or "—",
                    self.display_time(row["last_attempt"]),
                ),
                tags=(row["group"] if row["in_source"] else "removed",),
            )
        retained = [identifier for identifier in selected if self.library_tree.exists(identifier)]
        if retained:
            self.library_tree.selection_set(retained)
        if focused and self.library_tree.exists(focused):
            self.library_tree.focus(focused)
        if yview:
            self.library_tree.yview_moveto(yview[0])
        if xview:
            self.library_tree.xview_moveto(xview[0])
        counts = getattr(self, "library_counts", {})
        self.library_count.set(
            f"表示 {len(rows)} 件　／　現在のPaperpile {counts.get('total', 0)} 件　・　取り込み済み {counts.get('imported', 0)} 件　・　未取り込み {counts.get('not_imported', 0)} 件"
        )
        self.library_details()

    def sort_library(self, key):
        self.library_sort_reverse = (
            not self.library_sort_reverse if self.library_sort_key == key else False
        )
        self.library_sort_key = key
        for field, title in self.library_headings.items():
            self.library_tree.heading(
                field,
                text=title
                + ((" ▼" if self.library_sort_reverse else " ▲") if field == key else ""),
            )
        self.filter_library()

    def library_details(self):
        selected = self.library_tree.selection()
        row = getattr(self, "library_by_id", {}).get(selected[0]) if selected else None
        self.library_pdf_button.configure(state="normal" if row and row["pdf_path"] else "disabled")
        self.library_note_button.configure(
            state="normal" if row and row["note_path"] else "disabled"
        )
        text = "論文を選ぶと、書誌情報・失敗理由・保存された要約を表示します。"
        if row:
            analysis_label = "採用中の要約" if row["imported"] else "最終解析"
            analysis_time = row["last_success"] if row["imported"] else row["last_attempt"]
            text = f"{row['title']}\n著者：{'; '.join(row['authors'])}\n年：{row['year'] or '不明'}　　状態：{row['status']}（{row['registration']}）\nPaperpile：{'存在' if row['in_source'] else '現在の書誌にない'}　　PDF：{row['pdf_status']}\nDOI：{row['doi'] or '—'}　　arXiv：{row['arxiv_id'] or '—'}\n{analysis_label}：{row['provider'] or '—'} / {row['model'] or '—'}　　{self.display_time(analysis_time)}"
            if row["error"]:
                text += "\n\n失敗・要確認の記録：\n" + row["error"]
            if row["summary"]:
                text += (
                    "\n\n保存された要約（採用中の版）：\n" + row["summary"]
                    if row["imported"]
                    else "\n\n保存された要約（最終成功分）：\n" + row["summary"]
                )
            if row["abstract"]:
                text += "\n\n抄録：\n" + row["abstract"]
            if row["note_path"]:
                text += "\n\n要約ノート：" + row["note_path"]
        identifier = row["paper_id"] if row else None
        if text == getattr(self, "_library_detail_text", None):
            return
        position = (
            self.library_detail.yview()[0]
            if identifier == getattr(self, "_library_detail_id", None)
            else 0
        )
        self.library_detail.configure(state="normal")
        self.library_detail.delete("1.0", "end")
        self.library_detail.insert("1.0", text)
        self.library_detail.configure(state="disabled")
        self.library_detail.yview_moveto(position)
        self._library_detail_text = text
        self._library_detail_id = identifier

    def revalidate_responses(self):
        ids = list(self.library_tree.selection())
        if not ids:
            messagebox.showinfo(
                "保存済み回答の再検証", "再検証する論文を選んでください。", parent=self.root
            )
            return
        if not messagebox.askyesno(
            "保存済み回答の再検証",
            f"選択した{len(ids)}本の保存済み回答を再検証します。\nAIへは送信せず、Obsidianには保存しません。\n検証を通過した回答は要約済み・未保存になります。続けますか？",
            parent=self.root,
        ):
            return

        def done(result):
            self.status.set(
                f"{len(result)}本の回答が検証を通過しました。取り込みを再開すると保存されます。追加のAI送信はありません。"
            )
            self.refresh_library()

        provider, model = self.provider_code(), self.model_code()
        self.submit(
            "response_revalidate",
            lambda: self.service.revalidate_responses(ids, provider, model),
            done,
        )

    def open_library_file(self, key):
        selected = self.library_tree.selection()
        row = self.library_by_id.get(selected[0]) if selected else None
        if not row or not row.get(key):
            return
        path = Path(row[key]).resolve()
        root = (
            self.cfg.source.paperpile_dir if key == "pdf_path" else self.cfg.destination.vault_dir
        )
        if not path.is_relative_to(root.resolve()) or any(
            p.casefold() == ".obsidian" for p in path.parts
        ):
            messagebox.showerror(
                "場所", "設定した原本・Vaultの範囲外なので開けません。", parent=self.root
            )
            return
        self.open_path(path)

    def sync_library(self):
        service = self.service

        def done(result):
            self.status.set(f"Paperpileと同期しました：{result.get('papers', 0)}件")
            self.refresh_library(reconcile=True)

        self.submit("library_sync", service.sync_library, done)

    def open_summary_versions(self):
        from paperflow.gui_summaries import SummaryDialog

        ids = list(self.library_tree.selection())
        if not ids:
            messagebox.showinfo(
                "再要約",
                "論文一覧で取り込み済みの論文を選んでください。Ctrl/Shiftで複数選択できます。",
                parent=self.root,
            )
            return
        if any(not self.library_by_id[p]["imported"] for p in ids):
            messagebox.showinfo(
                "再要約", "取り込み済みの論文だけを選んでください。", parent=self.root
            )
            return
        self.summary_dialog = SummaryDialog(self, ids)

    def _build_history(self):
        top = ttk.Frame(self.history_tab)
        top.pack(fill="x", pady=(0, 12))
        self.button(top, "履歴を更新", self.refresh_history, during_run=True, side="right")
        self.button(top, "結果を更新", self.refresh_report, during_run=True, side="right", padx=8)
        self.button(top, "選んだ一覧を開く", self.open_history_plan, side="right", padx=8)
        self.history_tree = ttk.Treeview(
            self.history_tab, columns=("name", "date", "count", "status"), show="headings", height=8
        )
        for key, label, width in [
            ("name", "対象一覧", 330),
            ("date", "作成日時", 250),
            ("count", "本数", 80),
            ("status", "状態", 150),
        ]:
            self.history_tree.heading(key, text=label)
            self.history_tree.column(key, width=width)
        self.history_tree.pack(fill="x")
        self.history_tree.bind("<<TreeviewSelect>>", lambda e: self.history_report())
        self.history_paths = {}
        self.usage_text = tk.Text(
            self.history_tab,
            height=16,
            wrap="word",
            background="white",
            foreground="#243b31",
            font=("Yu Gothic UI", 11),
            relief="flat",
            padx=18,
            pady=15,
            state="disabled",
        )
        ttk.Label(
            self.history_tab,
            text="実行中も結果を閲覧できます。表示中の結果は約2秒ごとに更新します。",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(12, 0))
        self.usage_text.pack(fill="both", expand=True, pady=(6, 0))
        self._set_usage("一覧を選ぶと、登録本数・トークン数・CLI表示費用を確認できます。")

    def _build_settings(self):
        head = ttk.Frame(self.settings_tab)
        head.pack(fill="x", pady=(0, 12))
        self.button(head, "設定を保存", self.save_settings, primary=True, side="right")
        self.button(head, "読み込み直す", self.reload_settings, side="right", padx=8)
        self.button(head, "ファイルを開く", lambda: self.open_path(self.service.path), side="left")
        ttk.Label(
            head, text="保存した設定は次の処理から使用します。", style="Muted.TLabel", padding=10
        ).pack(side="left")
        self.setting_tabs = ttk.Notebook(self.settings_tab)
        self.setting_tabs.pack(fill="both", expand=True)

    def _reload_settings(self):
        for tab in self.setting_tabs.tabs():
            self.setting_tabs.forget(tab)
        # 古い入力部品は設定の再構築時に破棄する。
        self.mutable = [
            (w, s)
            for w, s in self.mutable
            if w.winfo_exists() and not str(w).startswith(str(self.setting_tabs) + ".")
        ]
        for child in self.setting_tabs.winfo_children():
            child.destroy()
        self.fields = {}
        values = config_values(self.service.path)
        hints = comments(self.service.path)
        self.settings_original = self.service.path.read_text(encoding="utf-8-sig")
        for section, title in SECTIONS.items():
            scroll = ScrollFrame(self.setting_tabs)
            self.setting_tabs.add(scroll, text=title)
            frame = scroll.inner
            frame.columnconfigure(1, weight=1)
            for row, (key, value) in enumerate(values[section].items()):
                dotted = section + "." + key
                ttk.Label(frame, text=LABELS.get(key, key), style="Card.TLabel").grid(
                    row=row * 2, column=0, sticky="nw", padx=(0, 20), pady=(9, 0)
                )
                if key == "research_context":
                    widget = tk.Text(
                        frame,
                        height=5,
                        width=50,
                        wrap="word",
                        font=("Yu Gothic UI", 10),
                        relief="solid",
                        borderwidth=1,
                    )
                    widget.insert("1.0", value)

                    def getter(w=widget):
                        return w.get("1.0", "end-1c")
                elif isinstance(value, bool):
                    var = tk.BooleanVar(value=value)
                    widget = ttk.Checkbutton(frame, variable=var, text="有効")
                    getter = var.get
                else:
                    var = tk.StringVar(value="" if value is None else str(value))
                    if key in CHOICES:
                        var.set(CHOICES[key][value])
                        widget = ttk.Combobox(
                            frame,
                            textvariable=var,
                            values=list(CHOICES[key].values()),
                            state="readonly",
                        )
                    else:
                        widget = ttk.Entry(frame, textvariable=var)

                    def getter(v=var, initial=value, name=key):
                        text = v.get().strip()
                        if name in CHOICES:
                            return next(k for k, label in CHOICES[name].items() if label == text)
                        if name in {"model", "bibtex_file"} and not text:
                            return None
                        try:
                            if type(initial) is int:
                                return int(text)
                            if type(initial) is float:
                                return float(text)
                        except ValueError as exc:
                            raise SettingsError(
                                LABELS.get(name, name) + "は数値で入力してください"
                            ) from exc
                        return text

                state = "disabled" if key in FIXED else "readonly" if key in CHOICES else "normal"
                widget.configure(state=state)
                self.mutable_widget(widget, state)
                widget.grid(row=row * 2, column=1, sticky="ew", pady=(7, 0))
                self.fields[dotted] = getter
                hint = hints.get(dotted, "")
                if hint:
                    ttk.Label(frame, text=hint, style="Hint.TLabel", wraplength=690).grid(
                        row=row * 2 + 1, column=1, sticky="w", pady=(3, 9)
                    )
        self._refresh_permission()

    def _settings_wheel(self, event):
        self.cloud_scroll._wheel(event)
        for child in self.setting_tabs.winfo_children():
            child._wheel(event)

    def save_quick_settings(self):
        provider, model = self.provider_code(), self.model_code()
        external = self.external_allowed.get()
        changes = {
            "analysis.default_provider": provider,
            "analysis.model": None if provider == "fake" else model,
            "analysis.policy": "fulltext_external" if external else "abstract_only",
            "analysis.allow_external_fulltext": external,
            "analysis.require_visual_reading": True,
            "destination.allow_vault_write": self.vault_allowed.get(),
        }
        original = self.settings_original

        def save():
            cfg = save_changes(self.service.path, changes, expected_text=original)
            self.service.remember(provider, model)
            return cfg

        def saved(cfg):
            self.cfg = cfg
            self._reload_settings()
            self.state_text.set("設定を保存しました。対象一覧を確認してから実行してください。")

        self.submit("settings", save, saved)

    def _load_selection(self):
        provider, model = self.service.initial_selection()
        self.provider.set(next(label for label, value in PROVIDERS.items() if value == provider))
        self.model.set(model or "")
        self._cached_models(provider)

    def provider_code(self):
        return PROVIDERS[self.provider.get()]

    def model_code(self):
        text = self.model.get().strip()
        return self.model_choices.get(text, text) or None

    def _cached_models(self, provider):
        catalog = self.service.models(provider)
        self._set_models(catalog, select_default=False)
        if provider == "fake":
            self.model.set("")
            self.model_box.configure(state="disabled")
        else:
            self.model_box.configure(state="normal")

    def _set_models(self, catalog, select_default=False):
        previous = self.model_code()
        self.model_choices = {}
        if catalog:
            self.model_choices = {
                f"{choice.display_name} — {choice.model_id}": choice.model_id
                for choice in catalog.models
            }
            self.model_box["values"] = list(self.model_choices)
            selected = next(
                (label for label, value in self.model_choices.items() if value == previous), None
            )
            if selected:
                self.model.set(selected)
            elif select_default and not previous:
                default = next((choice for choice in catalog.models if choice.is_default), None)
                if default:
                    self.model.set(
                        next(
                            label
                            for label, value in self.model_choices.items()
                            if value == default.model_id
                        )
                    )
            if catalog.warning:
                self.state_text.set(catalog.warning)
        else:
            self.model_box["values"] = []

    def provider_changed(self):
        self.model.set("")
        self._cached_models(self.provider_code())
        self._buttons()

    def mode_changed(self):
        self.count_entry.configure(
            state="disabled" if self.busy or self.mode.get() == "all" else "normal"
        )

    def _refresh_permission(self):
        cfg = self.service.config()
        self.cfg = cfg
        self.external_allowed.set(cfg.analysis.allow_external_fulltext)
        self.vault_allowed.set(cfg.destination.allow_vault_write)
        self.permission.set(
            "PDFの外部送信："
            + ("許可" if cfg.analysis.allow_external_fulltext else "無効")
            + "　／　Vault保存："
            + ("許可" if cfg.destination.allow_vault_write else "無効")
        )
        self.source_label.configure(
            text=f"入力：{cfg.source.paperpile_dir}\n保存先：{cfg.destination.vault_dir}"
        )
        self._buttons()

    def _buttons(self):
        allowed = self.cfg.destination.allow_vault_write and (
            self.provider.get() == "Fake（オフライン試験）"
            or (
                self.cfg.analysis.allow_external_fulltext
                and self.cfg.analysis.policy == "fulltext_external"
                and self.cfg.analysis.require_visual_reading
            )
        )
        ready = not self.busy and self.plan is not None and bool(self.plan.get("items")) and allowed
        from paperflow.progress_sync import settings

        if settings(self.cfg).get("enabled") and not self.service.progress_confirmed(self.cfg):
            ready = False
        self.start_button.configure(
            state="normal" if ready else "disabled",
            text=f"この{len(self.plan['items'])}本を要約して保存" if self.plan else "要約して保存",
        )
        self.stop_button.configure(
            state="normal" if self.busy and self.action == "run" else "disabled"
        )

    def _set_busy(self, value, action=None):
        self.busy = value
        self.action = action
        for widget, state in self.mutable:
            if widget.winfo_exists():
                widget.configure(state="disabled" if value else state)
        self.mode_changed()
        self._buttons()
        if not value and self.provider_code() == "fake":
            self.model_box.configure(state="disabled")

    def submit(self, action, job, done):
        if self.busy:
            return
        self._set_busy(True, action)
        self.job_started = time.monotonic()
        self.status.set(
            {
                "prepare": "PDFと書誌を照合しています",
                "models": "モデル一覧を取得しています",
                "run": "取り込み中",
                "history": "履歴を読み込んでいます",
                "settings": "設定を保存しています",
                "library_sync": "Paperpileの書誌とPDFを同期しています",
                "summary_generate": "選択した論文を再要約しています。正式版は保持しています",
                "summary_adopt": "選択した要約版をObsidianの正式版へ切り替えています",
                "summary_compare": "本文と図表の比較を準備しています",
                "summary_history": "要約の版を読み込んでいます",
                "response_revalidate": "保存済み回答を再検証しています。AI送信・Vault保存は行いません",
                "cloud": "共有フォルダの進捗を確認しています",
                "cloud_close": "共有フォルダへ保存してから終了します",
            }.get(action, "処理中")
        )

        def worker():
            try:
                self.events.put(("done", done, job()))
            except BaseException as exc:
                if action.startswith("cloud"):
                    try:
                        self.service.record_sync_error(str(exc))
                    except (OSError, ValueError):
                        pass
                self.events.put(("error", str(exc), type(exc).__name__))

        self.executor.submit(worker)

    def _drain(self):
        try:
            for _ in range(100):
                event = self.events.get_nowait()
                kind = event[0]
                if kind == "done":
                    self._set_busy(False)
                    self.status.set("完了")
                    if self.close_when_idle:
                        self.close_when_idle = False
                        self.close()
                        return
                    event[1](event[2])
                elif kind == "summary_progress":
                    event[1](event[2])
                elif kind == "recovery_progress":
                    self.show_recovery_progress(event[1])
                elif kind == "error":
                    action = self.action
                    self._set_busy(False)
                    self.status.set("処理を停止しました：" + event[1])
                    if self.close_when_idle:
                        self.close_when_idle = False
                    self.cloud_refresh()
                    if action == "cloud_close":
                        if messagebox.askyesno(
                            "共有フォルダへの保存が未完了",
                            event[1]
                            + "\n\n進捗はC:に保存されています。別PCへ切り替える前に、このPCで共有フォルダへ保存する必要があります。保存せずに画面を閉じますか？",
                            parent=self.root,
                        ):
                            self._destroy()
                            return
                        continue
                    messagebox.showerror("PaperFlow", event[1], parent=self.root)
                elif kind == "paper":
                    self._paper_progress(event[1])
                elif kind == "read_done":
                    self.read_pending = False
                    event[1](event[2])
                elif kind == "read_error":
                    self.read_pending = False
                    if len(event) > 2 and event[2]:
                        event[2](event[1])
                    else:
                        self._set_usage(
                            "結果を読み込めませんでした。実行処理は継続しています。\n" + event[1]
                        )
                elif kind in {"library_done", "library_error"}:
                    if event[1] == self.library_token:
                        self.library_pending = False
                        if kind == "library_done":
                            self.show_library(event[2])
                        else:
                            self.library_info.set("一覧を更新できませんでした：" + event[2])
                        again, reconcile = self.library_again, self.library_reconcile_again
                        self.library_again = self.library_reconcile_again = False
                        if again:
                            self.refresh_library(reconcile=reconcile)
                elif kind == "progress":
                    data = event[1]
                    self.registered_count = data["registered"]
                    self._live_stats()
                    self.progress["value"] = data["registered"]
                    self._mark_registered(data.get("registered_ids", []))
        except queue.Empty:
            pass
        except Exception as exc:
            self.status.set("画面更新エラー：" + str(exc))
            logging.getLogger("paperflow.gui").exception("GUI progress update failed")
        finally:
            if not self._closed:
                self.root.after(80, self._drain)
        if self.busy:
            self.elapsed_label.configure(text=f"経過 {int(time.monotonic() - self.job_started)} 秒")

    def _paper_progress(self, data):
        phase = data["phase"]
        label = {"started": "読解中", "processed": "保存中", "failed": "失敗"}[phase]
        identifier = data["paper_id"]
        self.paper_states[identifier] = phase
        self._live_stats()
        if self.tree.exists(identifier):
            self.tree.set(identifier, "state", label)
            self.tree.see(identifier)
        self.state_text.set(
            label
            + "："
            + data["title"]
            + ((" — " + data["message"]) if data.get("message") else "")
        )

    def read_job(self, job, done, on_error=None):
        """閲覧は解析キュー・busy状態・停止要求を変更しない。"""
        if self.read_pending or self._closed:
            return
        self.read_pending = True

        def worker():
            try:
                self.events.put(("read_done", done, job()))
            except Exception as exc:
                self.events.put(("read_error", str(exc), on_error))

        self.reader.submit(worker)

    def _poll_report(self):
        if self._closed:
            return
        if self.viewed_path and (self.view_running or (self.busy and self.action == "run")):
            self.refresh_report()
        self.root.after(2000, self._poll_report)

    def request_report(self, path):
        path = Path(path)
        self.viewed_path = path
        service = self.service

        def loaded(summary):
            if self.viewed_path == path and self.service is service:
                self.view_running = summary.get("status") == "running"
                self.show_summary(summary, update_progress=path == self.plan_path)

        self.read_job(lambda: service.live_report(path), loaded)

    def refresh_report(self):
        path = self.viewed_path or self.plan_path
        if path:
            self.request_report(path)

    def _live_stats(self):
        if self.plan:
            done = sum(s == "processed" for s in self.paper_states.values())
            failed = sum(s == "failed" for s in self.paper_states.values())
            self.stats.set(
                f"対象 {len(self.plan['items'])} 本　・　要約済み {done} 本　・　失敗 {failed} 本　・　登録済み {self.registered_count} 本"
            )

    def _mark_registered(self, identifiers):
        for identifier in identifiers:
            if self.tree.exists(identifier):
                self.tree.set(identifier, "state", "登録済み")

    def refresh_models(self):
        provider = self.provider_code()
        if provider == "fake":
            return
        self.submit(
            "models",
            lambda: self.service.models(provider, refresh=True),
            lambda result: self._set_models(result, True),
        )

    def remember_model(self):
        provider, model = self.provider_code(), self.model_code()
        self.submit(
            "models",
            lambda: self.service.remember(provider, model),
            lambda result: self.state_text.set("Providerとモデルの選択を保存しました。"),
        )

    def prepare_plan(self):
        try:
            mode = self.mode.get()
            count = None if mode == "all" else int(self.count.get())
            if count is not None and count < 1:
                raise ValueError
        except ValueError:
            messagebox.showerror("本数", "1以上の本数を入力してください。", parent=self.root)
            return
        self.submit("prepare", lambda: self.service.prepare(mode, count), self.show_plan_result)

    def show_plan_result(self, data):
        self._show_plan(Path(data["plan"]), data)

    def _show_plan(self, path, data):
        self.plan_path = Path(path)
        self.plan = data
        self.paper_states = {}
        self.registered_count = 0
        self.tree.delete(*self.tree.get_children())
        for item in data["items"]:
            self.tree.insert(
                "",
                tk.END,
                iid=item["paper_id"],
                values=(item["title"], item.get("year", ""), item["pages"], "待機"),
            )
        self.stats.set(f"対象 {len(data['items'])} 本　・　登録済み — 本")
        excluded = sum(data.get("excluded", {}).values())
        shortage = data.get("shortage", 0)
        self.state_text.set(
            f"合計 {data.get('total_pages', 0):,} ページ。除外 {excluded} 本。"
            + (f"候補不足 {shortage} 本。" if shortage else "")
            + " この一覧の論文だけを処理します。"
        )
        self.plan_label.set("対象一覧：" + str(self.plan_path))
        self.progress.configure(maximum=max(1, len(data["items"])), value=0)
        self.tabs.select(self.import_tab)
        self._buttons()

    def choose_plan(self):
        name = filedialog.askopenfilename(
            parent=self.root,
            title="対象一覧を開く",
            initialdir=self.cfg.storage.data_dir / "batches",
            filetypes=[("対象一覧", "*.json")],
        )
        if name:
            self.open_plan(Path(name))

    def open_plan(self, path):
        def loaded(data):
            self._show_plan(path, data)
            self.request_report(path)

        self.submit("history", lambda: self.service.load_plan(path), loaded)

    def start(self):
        if not self.plan_path:
            return
        provider, model = self.provider_code(), self.model_code()
        path = self.plan_path
        self.viewed_path = path
        self.service.stop_event.clear()
        self.submit(
            "run",
            lambda: self.service.run(
                path,
                provider,
                model,
                on_progress=lambda d: self.events.put(("progress", d)),
                on_paper=lambda d: self.events.put(("paper", d)),
            ),
            self.run_finished,
        )

    def stop(self):
        self.service.stop_event.set()
        self.state_text.set("現在の論文を保存してから停止します。")
        self.stop_button.configure(state="disabled")

    def run_finished(self, summary):
        self.show_summary(summary)
        state = STATES.get(summary["status"], summary["status"])
        self.state_text.set(
            f"{state}：登録済み {summary['registered']} 本 / {summary['planned']} 本。残り {summary['remaining']} 本。"
        )
        self.status.set(state)
        if summary.get("halted"):
            self.state_text.set("後続の送信を停止しました：" + summary["halted"]["message"])
        if self.library_loaded:
            self.refresh_library()

    def _set_usage(self, text):
        self.usage_text.configure(state="normal")
        self.usage_text.delete("1.0", tk.END)
        self.usage_text.insert("1.0", text)
        self.usage_text.configure(state="disabled")

    def show_summary(self, summary, *, update_progress=True):
        if "registered" in summary and update_progress:
            self.registered_count = summary["registered"]
            if self.plan:
                for row in summary.get("usage", {}).get("runs", []):
                    self.paper_states[row["paper_id"]] = {
                        "success": "processed",
                        "failed": "failed",
                        "running": "started",
                    }.get(row["status"], "failed")
                self._live_stats()
            else:
                self.stats.set(
                    f"対象 {summary['planned']} 本　・　登録済み {summary['registered']} 本"
                )
            self.progress["value"] = summary["registered"]
            self._mark_registered(summary.get("registered_ids", []))
        usage = summary.get("usage")
        if not usage:
            self._set_usage("まだ実行されていません。")
            return

        def show(value):
            return "不明" if value is None else f"{value:,}"

        reported = usage["reported"]
        state = STATES.get(summary.get("status"), "不明")
        text = f"状態：{state}　　更新：{time.strftime('%H:%M:%S')}\n登録済み {summary['registered']} / {summary['planned']} 本　　残り {summary['remaining']} 本\n\n外部AI呼出し：{usage['external_calls']}回\n入力トークン：{show(reported['input_tokens'])}\n出力トークン：{show(reported['output_tokens'])}\nCLI表示費用（USD）：{show(reported['cost_usd'])}\n解析時間（取得済み合計）：{usage.get('elapsed_analysis_seconds', 0):.1f}秒\n\n未取得の消費量は不明です。費用表示は利用枠の残量や請求確定額ではありません。"
        running = [r for r in usage.get("runs", []) if r["status"] == "running"]
        if running:
            text += "\n\n解析中：\n" + "\n".join(r["title"] for r in running)
        failed = [r for r in usage.get("runs", []) if r["status"] not in {"success", "running"}]
        if failed:
            text += "\n\n失敗の記録：\n" + "\n".join(
                r["title"]
                + "："
                + str(r.get("error_message") or r.get("error_type") or r["status"])
                for r in failed
            )
        self._set_usage(text)

    def refresh_history(self):
        service = self.service
        self.read_job(
            service.history,
            lambda rows: self.show_history(rows) if self.service is service else None,
        )

    def show_history(self, items):
        self.history_tree.delete(*self.history_tree.get_children())
        self.history_paths = {}
        for i, item in enumerate(items):
            key = str(i)
            self.history_paths[key] = Path(item["path"])
            self.history_tree.insert(
                "",
                tk.END,
                iid=key,
                values=(
                    Path(item["path"]).name,
                    item["created_at"],
                    item["count"],
                    STATES.get(item["status"], item["status"]),
                ),
            )

    def history_report(self):
        selected = self.history_tree.selection()
        if selected:
            self.request_report(self.history_paths[selected[0]])

    def open_history_plan(self):
        selected = self.history_tree.selection()
        if selected:
            self.open_plan(self.history_paths[selected[0]])

    def reload_settings(self):
        try:
            self._reload_settings()
            self.reset_library()
            self.reset_recovery()
            self.status.set("設定を読み込みました")
        except (OSError, ValueError) as exc:
            messagebox.showerror("設定", str(exc), parent=self.root)

    def save_settings(self):
        try:
            changes = {key: getter() for key, getter in self.fields.items()}
        except (ValueError, SettingsError) as exc:
            messagebox.showerror("設定", str(exc), parent=self.root)
            return
        path = self.service.path
        original = self.settings_original

        def saved(cfg):
            self.cfg = cfg
            self._reload_settings()
            self._load_selection()
            self.reset_library()
            self.reset_recovery()
            self.plan = None
            self.plan_path = None
            self.tree.delete(*self.tree.get_children())
            self.stats.set("対象 — 本　・　登録済み — 本")
            self.plan_label.set("対象一覧：未選択")
            self.state_text.set(
                "設定を保存しました。対象一覧を作成するか、保存済みの一覧を開いてください。"
            )
            self._buttons()

        self.submit("settings", lambda: save_changes(path, changes, expected_text=original), saved)

    def choose_config(self):
        name = filedialog.askopenfilename(
            parent=self.root,
            title="設定ファイルを選択",
            initialdir=self.service.path.parent,
            filetypes=[("設定ファイル", "*.yaml *.yml")],
        )
        if not name:
            return
        try:
            service = GuiService(name)
            cfg = service.config()
        except (OSError, ValueError) as exc:
            messagebox.showerror("設定", str(exc), parent=self.root)
            return
        self.service = service
        self.reset_library()
        self.reset_recovery()
        self.viewed_path = None
        self.view_running = False
        self.cfg = cfg
        self.config_label.set(service.path.name)
        self.plan = None
        self.plan_path = None
        self.tree.delete(*self.tree.get_children())
        self._reload_settings()
        self._load_selection()
        self._buttons()
        self.refresh_history()
        self.cloud_refresh()
        from paperflow.progress_sync import settings

        if settings(cfg).get("enabled"):
            self.cloud_sync()

    def open_path(self, path):
        try:
            path = Path(path)
            if not path.exists():
                raise FileNotFoundError("まだ作成されていません：" + str(path))
            if os.name == "nt":
                os.startfile(str(path))
            else:
                messagebox.showinfo("場所", str(path), parent=self.root)
        except OSError as exc:
            messagebox.showerror("開けません", str(exc), parent=self.root)

    def open_notes(self):
        self.open_path(self.cfg.destination.vault_dir / "10_Papers/20_Source_Notes")

    def open_report(self):
        if self.plan_path:
            self.tabs.select(self.history_tab)
            self.request_report(self.plan_path)
        else:
            messagebox.showinfo("レポート", "対象一覧を開いてください。", parent=self.root)

    def callback_error(self, kind, value, traceback):
        logging.getLogger("paperflow.gui").error("GUI error", exc_info=(kind, value, traceback))
        messagebox.showerror("PaperFlow", str(value), parent=self.root)

    def close(self):
        if self.busy:
            if messagebox.askyesno(
                "処理中", "現在の処理が終了してから画面を閉じますか？", parent=self.root
            ):
                self.close_when_idle = True
                self.service.stop_event.set()
                self.state_text.set("処理の終了後に画面を閉じます。")
            return
        from paperflow.progress_sync import settings

        if settings(self.service.config()).get("enabled") and self.service.progress_confirmed():

            def saved(result):
                messagebox.showinfo(
                    "共有フォルダへ保存しました",
                    "別PCへ切り替える前に、Windows右下のGoogle Driveで同期完了を確認してください。\nPaperFlowはクラウドへの送信完了を自動判定しません。",
                    parent=self.root,
                )
                self._destroy()

            self.submit("cloud_close", self.service.progress_sync, saved)
            return
        self._destroy()

    def _destroy(self):
        self._closed = True
        self.executor.shutdown(wait=False)
        self.reader.shutdown(wait=False, cancel_futures=True)
        self.library_reader.shutdown(wait=False, cancel_futures=True)
        self.root.destroy()


def main():
    parser = argparse.ArgumentParser(description="PaperFlow デスクトップ")
    parser.add_argument("--config", type=Path, default=Path("config.production.yaml"))
    parser.add_argument("--setup", action="store_true", help="初期設定画面を開く")
    args = parser.parse_args()
    # pythonwでは標準出力がNoneになる。ライブラリの診断出力は画面を開かず捨てる。
    for name in ["stdout", "stderr"]:
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
    root = tk.Tk()
    root.withdraw()
    try:
        app = PaperFlowApp(root, args.config)
        if args.setup:
            app.tabs.select(app.cloud_tab)
        root.deiconify()
        root.mainloop()
        return app
    except Exception as exc:
        messagebox.showerror("PaperFlowを起動できません", str(exc), parent=root)
        root.destroy()
        return None


if __name__ == "__main__":
    main()
