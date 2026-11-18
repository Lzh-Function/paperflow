"""選択論文の再要約と、正式版・候補版の比較画面。"""

import json
import tkinter as tk
from tkinter import messagebox, ttk


class SummaryDialog:
    def __init__(self, app, paper_ids):
        from paperflow.gui import PROVIDERS

        self.app, self.service, self.paper_ids = app, app.service, list(paper_ids)
        self.providers = PROVIDERS
        self.data = None
        self.window = tk.Toplevel(app.root)
        self.window.title("再要約・要約の版管理")
        self.window.geometry("1100x800")
        self.window.minsize(820, 600)
        self.window.transient(app.root)
        frame = ttk.Frame(self.window, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(
            frame,
            text=f"選択中：{len(paper_ids)}本　／　再要約は比較候補として保存されます。正式版は切り替えるまで保持します。",
            wraplength=1050,
        ).pack(anchor="w")
        model_row = ttk.Frame(frame)
        model_row.pack(fill="x", pady=10)
        self.provider = tk.StringVar(value=app.provider.get())
        self.model = tk.StringVar(value=app.model_code() or "")
        self.choices = {}
        provider = ttk.Combobox(
            model_row,
            textvariable=self.provider,
            values=list(PROVIDERS),
            state="readonly",
            width=23,
        )
        provider.pack(side="left")
        provider.bind("<<ComboboxSelected>>", lambda e: self.change_provider())
        self.model_box = ttk.Combobox(model_row, textvariable=self.model, width=36)
        self.model_box.pack(side="left", padx=8, fill="x", expand=True)
        self._button(model_row, "モデル一覧を取得", self.refresh_models)
        self._button(model_row, "再要約を開始", self.generate)
        self.stop_button = ttk.Button(
            model_row, text="この論文の完了後に停止", command=self.service.stop_event.set
        )
        self.stop_button.pack(side="left", padx=5)
        self.set_models(self.service.models(PROVIDERS[self.provider.get()]))
        self.result = tk.StringVar(
            value="再要約では選択した論文のPDFを指定モデルへ送ります。本文と図表を版ごとに保存します。"
        )
        ttk.Label(frame, textvariable=self.result, wraplength=1050).pack(anchor="w", pady=(0, 8))
        self.paper_choices = {
            f"{i + 1}. {app.library_by_id[p]['title']}": p for i, p in enumerate(paper_ids)
        }
        self.paper = tk.StringVar(value=next(iter(self.paper_choices)))
        papers = ttk.Combobox(
            frame, textvariable=self.paper, values=list(self.paper_choices), state="readonly"
        )
        papers.pack(fill="x")
        papers.bind("<<ComboboxSelected>>", lambda e: self.refresh())
        self.tree = ttk.Treeview(
            frame,
            columns=("status", "provider", "model", "date"),
            show="headings",
            height=4,
            selectmode="browse",
        )
        for key, title, width in [
            ("status", "版", 100),
            ("provider", "エンジン", 110),
            ("model", "モデル", 330),
            ("date", "生成日時", 180),
        ]:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width)
        self.tree.pack(fill="x", pady=8)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.show_texts())
        actions = ttk.Frame(frame)
        actions.pack(fill="x", pady=(0, 8))
        self._button(actions, "履歴を更新", self.refresh)
        self._button(actions, "図表付きで比較を開く", self.compare)
        self._button(actions, "選択版を正式版にする", self.adopt)
        self._button(actions, "保存済み回答から候補を復元", self.recover)
        self.consumption = tk.StringVar()
        ttk.Label(frame, textvariable=self.consumption, wraplength=1050).pack(
            anchor="w", pady=(0, 8)
        )
        pane = ttk.Panedwindow(frame, orient="horizontal")
        pane.pack(fill="both", expand=True)
        self.texts = []
        for title in ["現在の正式版", "選択した要約版"]:
            column = ttk.Frame(pane)
            pane.add(column, weight=1)
            ttk.Label(column, text=title).pack(anchor="w")
            text = tk.Text(column, wrap="word", width=40, font=("Yu Gothic UI", 10))
            bar = ttk.Scrollbar(column, command=text.yview)
            text.configure(yscrollcommand=bar.set, state="disabled")
            bar.pack(side="right", fill="y")
            text.pack(fill="both", expand=True)
            self.texts.append(text)
        self.refresh()

    def _button(self, parent, text, command):
        button = ttk.Button(parent, text=text, command=command)
        button.pack(side="left", padx=4)
        self.app.mutable.append((button, "normal"))
        return button

    def paper_id(self):
        return self.paper_choices[self.paper.get()]

    def selected(self):
        ids = self.tree.selection()
        return ids[0] if ids else None

    def set_models(self, catalog):
        if not self.window.winfo_exists():
            return
        self.choices = (
            {f"{m.display_name} — {m.model_id}": m.model_id for m in catalog.models}
            if catalog
            else {}
        )
        self.model_box["values"] = list(self.choices)

    def change_provider(self):
        self.model.set("")
        self.set_models(self.service.models(self.providers[self.provider.get()]))

    def refresh_models(self):
        provider = self.providers[self.provider.get()]
        self.app.submit(
            "models", lambda: self.service.models(provider, refresh=True), self.set_models
        )

    def refresh(self):
        paper_id = self.paper_id()
        self.app.submit(
            "summary_history", lambda: self.service.summary_history(paper_id), self.show_history
        )

    def show_history(self, data):
        if not self.window.winfo_exists():
            return
        self.data = data
        selected = self.selected()
        self.tree.delete(*self.tree.get_children())
        for row in data["versions"]:
            self.tree.insert(
                "",
                "end",
                iid=row["run_id"],
                values=(
                    "正式版" if row["run_id"] == data["current"] else "比較候補",
                    row["provider"],
                    row.get("model") or "CLI既定",
                    self.app.display_time(
                        row["started_at"] if "started_at" in row else row["created_at"]
                    ),
                ),
            )
        rows = self.tree.get_children()
        if rows:
            self.tree.selection_set(selected if selected in rows else rows[0])
        if data["failures"]:
            last = data["failures"][0]
            self.result.set(
                "前回の失敗・中断："
                + (last["error_message"] or last["status"])
                + "　現在の正式版は保持しています。"
            )
        if data.get("pending"):
            self.result.set(
                "AIの回答は保存済みです。「保存済み回答から候補を復元」で、再送せず本文・図表を作成できます。"
            )
        self.show_texts()

    def show_texts(self):
        if not self.data:
            return
        versions = {v["run_id"]: v for v in self.data["versions"]}
        for text, run_id in zip(self.texts, [self.data["current"], self.selected()], strict=True):
            text.configure(state="normal")
            text.delete("1.0", "end")
            text.insert(
                "1.0", versions.get(run_id, {}).get("note_text", "要約版を選んでください。")
            )
            text.configure(state="disabled")
        selected = versions.get(self.selected(), {})
        usage = json.loads(selected.get("usage_json") or "{}")
        incoming = usage.get("input_tokens")
        outgoing = usage.get("output_tokens")
        self.consumption.set(
            f"選択版の消費量：入力 {incoming if incoming is not None else '不明'} / 出力 {outgoing if outgoing is not None else '不明'} トークン　費用（CLI申告） {usage.get('cost_usd') if usage.get('cost_usd') is not None else '不明'} USD"
        )

    def progress(self, event):
        if self.window.winfo_exists():
            self.result.set(
                (
                    "再要約中："
                    if event["phase"] == "started"
                    else f"候補保存 {event['completed']}本／失敗 {event['failures']}本："
                )
                + event["title"]
            )

    def generate(self):
        provider = self.providers[self.provider.get()]
        model = self.choices.get(self.model.get(), self.model.get()).strip() or None
        titles = "\n".join(self.app.library_by_id[p]["title"] for p in self.paper_ids)
        message = f"選択した{len(self.paper_ids)}本を再要約します。\n{provider} / {model or 'CLI既定モデル'}\n\n{titles}\n\n"
        message += (
            "Fakeによる動作確認用のダミー要約を作成します（外部送信なし）。"
            if provider == "fake"
            else "選択した論文のPDF全文・図表を外部CLIへ送信します。"
        )
        message += "\n結果は比較候補として保存し、現在の正式ノートは変更しません。実行しますか？"
        if not messagebox.askyesno("再要約の実行", message, parent=self.window):
            return

        def done(result):
            if not self.window.winfo_exists():
                return
            message = f"候補を保存：{len(result['completed'])}本　失敗：{len(result['failures'])}本"
            if result["stopped"]:
                message += "　残りは未送信です。"
            if result["failures"]:
                message += "\n" + "\n".join(r["message"] for r in result["failures"])
            self.result.set(message)
            self.app.refresh_library()
            self.refresh()

        self.result.set("再要約中です。正式版は保持しています。")
        self.app.submit(
            "summary_generate",
            lambda: self.service.generate_summaries(
                self.paper_ids,
                provider,
                model,
                on_progress=lambda event: self.app.events.put(
                    ("summary_progress", self.progress, event)
                ),
            ),
            done,
        )

    def compare(self):
        run_id, paper_id = self.selected(), self.paper_id()
        if run_id:
            self.app.submit(
                "summary_compare",
                lambda: self.service.compare_summary(paper_id, run_id),
                lambda path: self.app.open_path(path),
            )

    def recover(self):
        paper_id = self.paper_id()

        def done(result):
            if self.window.winfo_exists():
                self.result.set(
                    f"保存済み回答から{len(result)}件の候補を復元しました。追加のAI送信はありません。"
                )
                self.refresh()

        self.app.submit("summary_recover", lambda: self.service.recover_summaries(paper_id), done)

    def adopt(self):
        run_id, paper_id = self.selected(), self.paper_id()
        if not run_id or not self.data or run_id == self.data["current"]:
            return
        row = next(v for v in self.data["versions"] if v["run_id"] == run_id)
        if not messagebox.askyesno(
            "正式版の切り替え",
            f"{self.data['title']}\n{row['provider']} / {row.get('model') or 'CLI既定モデル'}\n\nObsidianのPaperFlow管理Source Noteと統合提案を、この版へ切り替えます。以前の本文・図表は履歴に残ります。手動編集やWiki統合済みの場合は停止します。切り替えますか？",
            parent=self.window,
        ):
            return

        def done(result):
            if self.window.winfo_exists():
                self.result.set("正式版を切り替えました。以前の版にも戻せます。")
                self.refresh()
            self.app.refresh_library(reconcile=True)

        self.app.submit("summary_adopt", lambda: self.service.adopt_summary(paper_id, run_id), done)
