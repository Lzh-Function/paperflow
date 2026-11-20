"""復旧の一覧と一括処理。読み取りと実行をGUIスレッドから分離する。"""

import copy
import tkinter as tk
from collections import Counter
from tkinter import filedialog, messagebox, ttk

from paperflow.recovery import LABELS, MODES

FILTERS = {"未解決すべて": None, **{label: group for group, label in LABELS.items()}}
STATES = {
    "pending": "未実行",
    "running": "処理中",
    "saved": "保存済み",
    "ready": "要約済み・未保存",
    "already_saved": "取り込み済み",
    "failed": "失敗",
    "needs_analysis": "再解析待ち",
    "complete": "完了",
    "partial": "未完了あり",
    "stopped": "停止",
    "interrupted": "中断",
    "blocked": "要対応・停止",
}


class RecoveryPanel:
    def _build_recovery(self):
        self.recovery_data = None
        self.recovery_current_job = None
        self.recovery_query = tk.StringVar()
        self.recovery_filter = tk.StringVar(value="未解決すべて")
        self.recovery_summary = tk.StringVar(value="画面を開くと失敗と未保存をまとめて照合します。")
        self.recovery_result = tk.StringVar()
        bar = ttk.Frame(self.recovery_tab)
        bar.pack(fill="x")
        ttk.Label(bar, text="失敗・未保存の一括後処理", font=("Yu Gothic UI", 13, "bold")).pack(
            side="left"
        )
        self.button(bar, "一覧を更新", self.refresh_recovery, during_run=True, side="right")
        self.button(bar, "処理可能をすべて選択", self.select_recovery_all, side="right", padx=8)
        ttk.Label(self.recovery_tab, textvariable=self.recovery_summary, wraplength=1050).pack(
            anchor="w", pady=8
        )
        filters = ttk.Frame(self.recovery_tab)
        filters.pack(fill="x")
        ttk.Label(filters, text="絞り込み").pack(side="left")
        box = ttk.Combobox(
            filters,
            textvariable=self.recovery_filter,
            values=list(FILTERS),
            state="readonly",
            width=29,
        )
        box.pack(side="left", padx=8)
        box.bind("<<ComboboxSelected>>", lambda e: self.filter_recovery())
        entry = ttk.Entry(filters, textvariable=self.recovery_query)
        entry.pack(side="left", fill="x", expand=True)
        entry.bind("<KeyRelease>", lambda e: self.filter_recovery())
        grid = ttk.Frame(self.recovery_tab)
        grid.pack(fill="both", expand=True, pady=8)
        self.recovery_tree = ttk.Treeview(
            grid,
            columns=("group", "title", "model", "error"),
            show="headings",
            selectmode="extended",
            height=7,
        )
        for key, title, width in [
            ("group", "必要な処理", 185),
            ("title", "論文", 370),
            ("model", "最後のモデル", 170),
            ("error", "失敗理由・処理結果", 310),
        ]:
            self.recovery_tree.heading(key, text=title)
            self.recovery_tree.column(
                key, width=width, minwidth=70, stretch=key in {"title", "error"}
            )
        vertical = ttk.Scrollbar(grid, command=self.recovery_tree.yview)
        horizontal = ttk.Scrollbar(grid, orient="horizontal", command=self.recovery_tree.xview)
        self.recovery_tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.recovery_tree.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        grid.rowconfigure(0, weight=1)
        grid.columnconfigure(0, weight=1)
        self.recovery_tree.bind("<<TreeviewSelect>>", lambda e: self.recovery_details())
        actions = ttk.Frame(self.recovery_tab)
        actions.pack(fill="x")
        for mode in ("validate", "save", "reanalyze"):
            self.button(
                actions,
                MODES[mode],
                lambda m=mode: self.prepare_recovery_action(m),
                side="left",
                padx=(0, 8),
            )
        self.recovery_stop_button = ttk.Button(
            actions, text="現在の論文の完了後に停止", command=lambda: self.service.stop_event.set()
        )
        self.recovery_stop_button.pack(side="right")
        ttk.Label(
            self.recovery_tab,
            text="再解析のAIとモデルは「取り込み」で選択したものを使用します。共有解析設定は変更しません。",
            wraplength=1050,
            style="Muted.TLabel",
        ).pack(anchor="w", pady=5)
        history = ttk.Frame(self.recovery_tab)
        history.pack(fill="x", pady=5)
        self.button(
            history,
            "別保存の成功結果を取り込む",
            self.import_saved_result,
            side="left",
            padx=(0, 8),
        )
        ttk.Label(history, text="前回の後処理").pack(side="left")
        self.recovery_history_value = tk.StringVar()
        self.recovery_history_box = ttk.Combobox(
            history, textvariable=self.recovery_history_value, state="readonly"
        )
        self.recovery_history_box.pack(side="left", fill="x", expand=True, padx=8)
        self.button(
            history, "結果を表示", self.show_saved_recovery, side="right", during_run=True, padx=8
        )
        self.button(history, "選んだ作業を再開", self.resume_recovery, side="right")
        ttk.Label(self.recovery_tab, textvariable=self.recovery_result, wraplength=1050).pack(
            anchor="w", pady=4
        )
        self.recovery_detail = tk.Text(
            self.recovery_tab, height=7, wrap="word", state="disabled", font=("Yu Gothic UI", 10)
        )
        self.recovery_detail.pack(fill="x")

    def refresh_recovery(self):
        if self._closed:
            return
        service = self.service

        def done(data):
            if self.service is not service:
                return
            self.recovery_data = data
            counts = data["counts"]
            labels = [f"{label} {counts.get(group, 0)}本" for group, label in LABELS.items()]
            bundles = data["bundles"]
            self.recovery_summary.set(
                "　／　".join(labels)
                + f"\nC:の未反映プレビュー {bundles.get('preview', 0)}件・反映途中 {bundles.get('applying', 0)}件。プレビューは本番登録ではありません。"
            )
            self.recovery_jobs = {
                f"{j['created_at'][:16]} / {MODES[j['mode']]} / {STATES.get(j['status'], j['status'])} / {j['job_id'][:8]}": j[
                    "job_id"
                ]
                for j in data["jobs"]
            }
            self.recovery_jobs_data = {j["job_id"]: j for j in data["jobs"]}
            self.recovery_history_box.configure(values=list(self.recovery_jobs))
            if self.recovery_history_value.get() not in self.recovery_jobs:
                self.recovery_history_value.set(next(iter(self.recovery_jobs), ""))
            self.filter_recovery()

        self.read_job(
            lambda: service.recovery_overview(),
            done,
            on_error=lambda message: self.recovery_result.set(
                "一覧を更新できませんでした：" + message
            ),
        )

    def reset_recovery(self):
        self.recovery_data = None
        self.recovery_current_job = None
        self.recovery_rows = {}
        self.recovery_jobs = {}
        self.recovery_jobs_data = {}
        self.recovery_tree.delete(*self.recovery_tree.get_children())
        self.recovery_history_box.configure(values=[])
        self.recovery_history_value.set("")
        self.recovery_summary.set("一覧を更新して現在の設定の状態を照合してください。")
        self.recovery_result.set("")
        self.recovery_details()

    def filter_recovery(self):
        if self.recovery_data is None:
            return
        selected = set(self.recovery_tree.selection())
        self.recovery_tree.delete(*self.recovery_tree.get_children())
        self.recovery_rows = {}
        group = FILTERS[self.recovery_filter.get()]
        terms = self.recovery_query.get().casefold().split()
        for row in self.recovery_data["rows"]:
            if (group is None and row["recovery_group"] == "resolved") or (
                group is not None and row["recovery_group"] != group
            ):
                continue
            if not all(
                t in (row["title"] + " " + row["error"] + " " + row["model"]).casefold()
                for t in terms
            ):
                continue
            self.recovery_rows[row["paper_id"]] = row
            self.recovery_tree.insert(
                "",
                "end",
                iid=row["paper_id"],
                values=(row["recovery_label"], row["title"], row["model"], row["error"]),
            )
        kept = selected & self.recovery_rows.keys()
        if kept:
            self.recovery_tree.selection_set(list(kept))
        if self.recovery_current_job:
            self.show_recovery_progress(self.recovery_current_job)

    def select_recovery_all(self):
        self.recovery_tree.selection_set(
            [
                i
                for i in self.recovery_tree.get_children()
                if self.recovery_rows[i]["recovery_group"] not in {"attention", "resolved"}
            ]
        )

    def recovery_details(self):
        selected = self.recovery_tree.selection()
        row = getattr(self, "recovery_rows", {}).get(selected[0]) if selected else None
        text = (
            "論文を選ぶと、失敗理由と保存済み回答の有無を表示します。Ctrl/Shiftで複数選択できます。"
        )
        if row:
            text = f"{row['title']}\n必要な処理：{row['recovery_label']}\n最後のAI・モデル：{row['provider']} / {row['model']}\n失敗履歴：{row['failed_runs']}回\n{row['error'] or '失敗理由なし'}"
            if row["diagnostic"]:
                text += "\n保存済みの不採用回答：" + row["diagnostic"]
            if row.get("artifact_folder"):
                text += (
                    "\n別保存の成功結果候補："
                    + row["artifact_folder"]
                    + "\n再送せず復旧・保存で入力と来歴を照合します。"
                )
        self.recovery_detail.configure(state="normal")
        self.recovery_detail.delete("1.0", "end")
        self.recovery_detail.insert("1.0", text)
        self.recovery_detail.configure(state="disabled")

    def prepare_recovery_action(self, mode):
        ids = list(self.recovery_tree.selection())
        if not ids:
            messagebox.showinfo("復旧・後処理", "処理する論文を選んでください。", parent=self.root)
            return
        provider, model = self.provider_code(), self.model_code()
        self.submit(
            "recovery_prepare",
            lambda: self.service.prepare_recovery(ids, mode, provider, model),
            self.confirm_recovery,
        )

    def confirm_recovery(self, job):
        mode = job["mode"]
        text = MODES[mode] + f"\n対象：{len(job['items'])}本\n"
        text += (
            f"PDFを {job['provider']} / {job['model'] or 'CLI既定'} へ送り、成功した要約をObsidianへ保存します。"
            if mode == "reanalyze"
            else "AIへは再送しません。"
            + (
                "検証を通った要約をObsidianへ保存します。"
                if mode in {"save", "import"}
                else "結果はC:にだけ保存します。"
            )
        )
        text += "\n\n" + "\n".join(i["title"] for i in job["items"][:12])
        if len(job["items"]) > 12:
            text += f"\nほか{len(job['items']) - 12}本（選択した論文のみ）"
        if messagebox.askyesno("対象と操作の確認", text, parent=self.root):
            self.recovery_current_job = job
            self.submit(
                "recovery_run",
                lambda: self.service.run_recovery(
                    job,
                    on_progress=lambda j: self.events.put(("recovery_progress", copy.deepcopy(j))),
                ),
                self.recovery_finished,
            )

    def resume_recovery(self):
        identifier = getattr(self, "recovery_jobs", {}).get(self.recovery_history_value.get())
        if identifier:
            self.submit(
                "recovery_prepare",
                lambda: self.service.load_recovery(identifier),
                self.confirm_recovery,
            )

    def show_saved_recovery(self):
        identifier = getattr(self, "recovery_jobs", {}).get(self.recovery_history_value.get())
        job = getattr(self, "recovery_jobs_data", {}).get(identifier)
        if job:
            self.show_recovery_progress(job)
            text = f"{MODES[job['mode']]} / {job['provider']} / {job['model'] or '既定'}\n"
            text += "\n".join(
                f"{STATES.get(i['status'], i['status'])}：{i['title']}\n  {i.get('message', '')} / 追加AI呼出し {i.get('external_calls', 0)}回"
                for i in job["items"]
            )
            self.recovery_detail.configure(state="normal")
            self.recovery_detail.delete("1.0", "end")
            self.recovery_detail.insert("1.0", text)
            self.recovery_detail.configure(state="disabled")

    def import_saved_result(self):
        folder = filedialog.askdirectory(
            title="PaperFlowの成功試験結果フォルダを選択", parent=self.root
        )
        if folder:
            self.submit(
                "recovery_prepare",
                lambda: self.service.prepare_saved_result(folder),
                self.confirm_recovery,
            )

    def show_recovery_progress(self, job):
        self.recovery_current_job = job
        counts = Counter(i["status"] for i in job["items"])
        self.recovery_result.set(
            f"{STATES.get(job['status'], job['status'])}："
            + "　".join(f"{STATES.get(k, k)} {v}本" for k, v in counts.items())
            + ("\n停止理由：" + job["halted"] if job.get("halted") else "")
        )
        for item in job["items"]:
            if self.recovery_tree.exists(item["paper_id"]):
                self.recovery_tree.set(
                    item["paper_id"], "group", STATES.get(item["status"], item["status"])
                )
                self.recovery_tree.set(item["paper_id"], "error", item.get("message", ""))

    def recovery_finished(self, job):
        self.show_recovery_progress(job)
        self.refresh_recovery()
        self.reset_library()
