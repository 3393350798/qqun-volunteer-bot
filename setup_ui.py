from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from io import StringIO
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from icalendar import Calendar
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.error import YAMLError

from src.docs_adapter import TencentDocsAdapter
from src.parser import document_id, extract_message, parse_activity, parse_semester_start
from src.state import SeenState, DocumentRecords
from src.time_rules import available_slots, unavailable_slots, ics_entries
from src.schedule import Schedule
from src.week_calendar import WeekCalendar, display_range

PROJECT_DIR = Path(__file__).resolve().parent
WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


class ConfigApp:
    def __init__(self, root: tk.Tk, path: Path):
        self.root, self.path = root, path.resolve()
        self.yaml = YAML()
        self.yaml.preserve_quotes = True
        self.yaml.indent(mapping=2, sequence=4, offset=2)
        from src.config import ensure_config
        ensure_config(self.path)
        self.config = self.yaml.load(self.path.read_text(encoding="utf-8"))
        self.bindings = []
        self.process = None
        self.busy = self.closing = False
        self.output_queue = queue.Queue()
        root.title("Q群志愿机器人控制台")
        root.geometry("1080x820")
        root.minsize(900, 680)
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure(".", font=("Microsoft YaHei UI", 10))
        style.configure("TButton", padding=(12, 6))
        style.configure("TNotebook.Tab", padding=(14, 8))
        style.configure("Treeview", rowheight=28)
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 17, "bold"))
        style.configure("Status.TLabel", foreground="#146b4b")
        header = ttk.Frame(root, padding=(16, 12))
        header.pack(fill="x")
        ttk.Label(header, text="Q群志愿机器人", style="Title.TLabel").pack(side="left")
        self.status = tk.StringVar(value="未启动")
        ttk.Label(header, textvariable=self.status, style="Status.TLabel").pack(side="right")
        toolbar = ttk.Frame(root, padding=(12, 0, 12, 8))
        toolbar.pack(fill="x")
        self.start_button = ttk.Button(toolbar, text="启动监听", command=self.start)
        self.start_button.pack(side="left", padx=4)
        self.stop_button = ttk.Button(toolbar, text="停止监听", command=self.stop, state="disabled")
        self.stop_button.pack(side="left", padx=4)
        self.scan_button = ttk.Button(toolbar, text="扫描 QQ 一次（演练）", command=self.scan_once)
        self.scan_button.pack(side="left", padx=4)
        ttk.Button(toolbar, text="保存配置", command=self.save).pack(side="right", padx=4)
        self.reload_button = ttk.Button(toolbar, text="重新载入", command=self.reload)
        self.reload_button.pack(side="right", padx=4)
        panes = ttk.Panedwindow(root, orient="vertical")
        panes.pack(fill="both", expand=True, padx=16, pady=(0, 8))
        self.tabs = ttk.Notebook(panes)
        panes.add(self.tabs, weight=4)
        logs = ttk.Frame(panes)
        panes.add(logs, weight=1)
        log_bar = ttk.Frame(logs)
        log_bar.pack(fill="x", pady=(6, 3))
        ttk.Label(log_bar, text="运行日志").pack(side="left")
        ttk.Button(log_bar, text="日志文件", command=lambda: self.open_local("runtime.log_file")).pack(side="right")
        ttk.Button(log_bar, text="清空显示", command=self.clear_log).pack(side="right", padx=4)
        self.log = ScrolledText(logs, height=8, wrap="word", state="disabled", bg="#f8faf9",
                                fg="#24312c", font=("Microsoft YaHei UI", 9), relief="flat")
        self.log.pack(fill="both", expand=True)
        self.log.tag_configure("error", foreground="#ac302b")
        self.log.tag_configure("warning", foreground="#896000")
        self.summary = tk.StringVar()
        ttk.Label(root, textvariable=self.summary, padding=(16, 4)).pack(fill="x")
        self._build_tabs()
        self._update_summary()
        root.after(120, self._drain)
        root.after(5000, self._refresh_visible_schedule)
        root.protocol("WM_DELETE_WINDOW", self._close)
        root.bind_all("<MouseWheel>", self._scroll)
        self._write("控制台已就绪")

    def _scroll(self, event):
        widget = self.root.winfo_containing(event.x_root, event.y_root)
        while widget is not None:
            if isinstance(widget, (tk.Text, ttk.Treeview, ttk.Combobox, ttk.Spinbox)):
                return
            if isinstance(widget, tk.Canvas):
                if widget.yview() != (0.0, 1.0):
                    widget.yview_scroll(-int(event.delta / 120), "units")
                return "break"
            widget = widget.master

    def _get(self, path, default=""):
        value = self.config
        for key in path.split("."):
            if not isinstance(value, dict) or key not in value:
                return default
            value = value[key]
        return value

    def _tab(self, title):
        outer = ttk.Frame(self.tabs)
        self.tabs.add(outer, text=title)
        canvas = tk.Canvas(outer, highlightthickness=0, bg="#eeeeee")
        scroll = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        scroll.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        canvas.configure(yscrollcommand=scroll.set)
        body = ttk.Frame(canvas, padding=14)
        window = canvas.create_window((0, 0), window=body, anchor="nw")
        body.bind("<Configure>", lambda _: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window, width=event.width))
        return body

    def _field(self, parent, label, path, default="", kind=str, choices=None, browse=None):
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=5)
        ttk.Label(row, text=label, width=23).pack(side="left")
        var = tk.StringVar(value=str(self._get(path, default)))
        if choices:
            widget = ttk.Combobox(row, textvariable=var, values=choices, state="readonly")
        elif kind in (int, float):
            widget = ttk.Spinbox(row, textvariable=var, from_=0, to=100000, increment=1)
        else:
            widget = ttk.Entry(row, textvariable=var)
        widget.pack(side="left", fill="x", expand=True)
        self.bindings.append((path, lambda: kind(var.get().strip()), label))
        if browse:
            def choose():
                selected = filedialog.askdirectory(parent=self.root) if browse == "dir" else filedialog.askopenfilename(parent=self.root)
                if selected:
                    var.set(selected)
            ttk.Button(row, text="浏览", command=choose).pack(side="left", padx=(6, 0))
        return var

    def _check(self, parent, label, path, default=False):
        var = tk.BooleanVar(value=bool(self._get(path, default)))
        ttk.Checkbutton(parent, text=label, variable=var).pack(anchor="w", pady=5)
        self.bindings.append((path, var.get, label))
        return var

    def _lines(self, parent, label, path, height=3):
        ttk.Label(parent, text=label).pack(anchor="w", pady=(10, 4))
        widget = ScrolledText(parent, height=height, wrap="none", font=("Microsoft YaHei UI", 10))
        widget.pack(fill="x")
        widget.insert("1.0", "\n".join(str(item) for item in self._get(path, [])))
        self.bindings.append((path, lambda: [line.strip() for line in widget.get("1.0", "end").splitlines() if line.strip()], label))
        return widget

    def _tree(self, parent, columns, height=5):
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True, pady=6)
        tree = ttk.Treeview(frame, columns=tuple(columns), show="headings", height=height)
        for key, label in columns.items():
            tree.heading(key, text=label)
            tree.column(key, width=120, minwidth=70)
        scroll = ttk.Scrollbar(frame, command=tree.yview)
        scroll.pack(side="right", fill="y")
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(fill="both", expand=True)
        return tree

    def _build_tabs(self):
        self._operations()
        self._profile()
        self._qq()
        self._unavailable()
        self._docs()
        self._runtime()
        self._records()
        self._schedule()
        advanced = self._tab("高级配置")
        self.advanced_button = ttk.Button(advanced, text="编辑完整 config.yaml", command=self.edit_yaml)
        self.advanced_button.pack(anchor="w", pady=6)
        ttk.Button(advanced, text="打开项目目录", command=lambda: os.startfile(self.path.parent)).pack(anchor="w", pady=6)

    def _operations(self):
        tab = self._tab("操作台")
        actions = ttk.Frame(tab)
        actions.pack(fill="x")
        self.edge_button = ttk.Button(actions, text="打开 Edge 登录会话", command=self.open_edge)
        self.edge_button.pack(side="left", padx=(0, 8))
        self.test_button = ttk.Button(actions, text="打开金山文档", command=lambda: self.open_edge("https://www.kdocs.cn"))
        self.test_button.pack(side="left")
        ttk.Label(tab, text="活动消息").pack(anchor="w", pady=(14, 4))
        self.message_text = ScrolledText(tab, height=5, font=("Microsoft YaHei UI", 10))
        self.message_text.pack(fill="x")
        ttk.Button(tab, text="预览可用时段", command=self.preview).pack(anchor="w", pady=6)
        self.preview_tree = self._tree(tab, {"date": "日期", "start": "开始", "end": "结束", "state": "时段状态"}, 4)
        self.preview_tree.tag_configure("blocked", foreground="#ac302b")
        self.preview_tree.tag_configure("available", foreground="#146b4b")

    def _profile(self):
        tab = self._tab("个人资料")
        for key, label in (("name", "姓名"), ("college", "学院 / 院系"), ("student_id", "学号"), ("phone", "手机号")):
            self._field(tab, label, f"profile.{key}")

    def _qq(self):
        tab = self._tab("QQ 监听")
        self._lines(tab, "监听群名", "qq.group_names")
        self._lines(tab, "监听群号", "qq.groups", 2)
        self._check(tab, "不限制群名或群号（QQ 可见会话）", "qq.scan_all_groups")
        self._field(tab, "QQ 进程名称", "qq.process_name", "QQ.exe")
        self._field(tab, "窗口标题包含", "qq.message_window_title")
        self._check(tab, "识别窗口中的二维码", "qq.scan_qr_images", True)
        self._check(tab, "自动申请加入 QQ 群 / 打开微信邀请", "qq.auto_join_groups")
        self._field(tab, "加群验证消息", "qq.join_verification_message", "")

    def _unavailable(self):
        tab = self._tab("不可用时间")
        actions = ttk.Frame(tab)
        actions.pack(fill="x")
        ttk.Label(actions, text="ICS 日历").pack(side="left")
        ttk.Button(actions, text="导入 ICS", command=self.choose_ics).pack(side="left", padx=8)
        ttk.Button(actions, text="移除所选", command=self.remove_ics).pack(side="left")
        self.ics = self._tree(tab, {"name": "文件名", "events": "事件数", "path": "文件路径"}, 3)
        self.ics.column("name", width=200)
        self.ics.column("events", width=80, stretch=False)
        self.ics.column("path", width=420)
        for filename in self._get("unavailable.ics_files", []):
            try:
                count = self._ics_event_count(filename)
            except (OSError, ValueError):
                count = "不可读"
            self.ics.insert("", "end", values=(Path(filename).name, count, filename))
        self.bindings.append(("unavailable.ics_files", self.ics_values, "ICS 日历"))
        ttk.Label(tab, text="每周固定时段").pack(anchor="w")
        self.weekly = self._tree(tab, {"weekday": "星期", "start": "开始", "end": "结束"}, 3)
        for item in self._get("unavailable.weekly", []):
            self.weekly.insert("", "end", values=(WEEKDAYS[int(item["weekday"])], item["start"], item["end"]))
        row = ttk.Frame(tab)
        row.pack(fill="x")
        for label, command in (("添加时段", self.add_weekly), ("编辑时段", self.edit_weekly), ("删除时段", self.delete_weekly)):
            ttk.Button(row, text=label, command=command).pack(side="left", padx=(0, 6))
        self.weekly.bind("<Double-1>", lambda _: self.edit_weekly())
        self.bindings.append(("unavailable.weekly", self.weekly_values, "每周固定时段"))
        self._lines(tab, "文本时间规则", "unavailable.text_rules", 2)

    def weekly_values(self):
        values = []
        for item in self.weekly.get_children():
            day, start, end = self.weekly.item(item, "values")
            rule = CommentedMap(weekday=WEEKDAYS.index(day), start=start, end=end)
            for key, comment in (("weekday", "星期编号，0=周一"), ("start", "开始时间"), ("end", "结束时间")):
                rule.yaml_add_eol_comment(comment, key)
            values.append(rule)
        return values

    def add_weekly(self, item=None):
        dialog = tk.Toplevel(self.root)
        dialog.title("每周不可用时段")
        dialog.transient(self.root)
        dialog.resizable(False, False)
        dialog.grab_set()
        values = self.weekly.item(item, "values") if item else (WEEKDAYS[0], "18:00", "21:00")
        variables = [tk.StringVar(value=value) for value in values]
        for index, label in enumerate(("星期", "开始时间", "结束时间")):
            ttk.Label(dialog, text=label).grid(row=index, column=0, padx=14, pady=8)
            control = (ttk.Combobox(dialog, values=WEEKDAYS, textvariable=variables[index], state="readonly", width=12)
                       if index == 0 else ttk.Entry(dialog, textvariable=variables[index], width=14))
            control.grid(row=index, column=1, padx=14, pady=8)

        def apply():
            try:
                day = variables[0].get()
                start, end = [datetime.strptime(var.get().strip(), "%H:%M") for var in variables[1:]]
                result = (day, start.strftime("%H:%M"), end.strftime("%H:%M"))
                if item:
                    self.weekly.item(item, values=result)
                else:
                    self.weekly.insert("", "end", values=result)
                dialog.destroy()
            except ValueError as exc:
                messagebox.showerror("时间格式错误", str(exc), parent=dialog)
        ttk.Button(dialog, text="确定", command=apply).grid(row=3, column=0, columnspan=2, pady=12)

    def edit_weekly(self):
        selected = self.weekly.selection()
        if selected:
            self.add_weekly(selected[0])

    def delete_weekly(self):
        self.weekly.delete(*self.weekly.selection())

    def choose_ics(self):
        files = filedialog.askopenfilenames(parent=self.root, title="导入 ICS 日历", filetypes=[("iCalendar 日历", "*.ics")])
        if not files:
            return
        existing = {os.path.normcase(str((self.path.parent / filename).resolve())) for filename in self.ics_values()}
        imported = 0
        for filename in files:
            path = Path(filename).resolve()
            key = os.path.normcase(str(path))
            if key in existing:
                self._write(f"日历已导入：{path.name}")
                continue
            try:
                count = self._ics_event_count(path)
            except (OSError, ValueError) as exc:
                self._write(f"日历导入失败：{path.name}，{exc}")
                continue
            self.ics.insert("", "end", values=(path.name, count, str(path)))
            existing.add(key)
            imported += 1
            self._write(f"已读取日历：{path.name}，{count} 个事件")
        if imported and self.save():
            self._write(f"已导入并保存 {imported} 份日历" + ("，重启监听后生效" if self.process else ""))

    def _ics_event_count(self, filename):
        content = (self.path.parent / filename).read_text(encoding="utf-8-sig")
        return len(Calendar.from_ical(content).walk("VEVENT"))

    def ics_values(self):
        return [self.ics.item(item, "values")[2] for item in self.ics.get_children()]

    def remove_ics(self):
        selected = self.ics.selection()
        if selected:
            self.ics.delete(*selected)
            if self.save():
                self._write(f"已移除 {len(selected)} 份日历引用，原文件保留")

    def _docs(self):
        tab = self._tab("Edge / 表格")
        self._field(tab, "Edge 调试地址", "tencent_docs.debugger_address", "127.0.0.1:9222")
        self._check(tab, "端口未运行时自动启动 Edge", "tencent_docs.auto_start_debugger", True)
        self._field(tab, "Edge 用户目录", "tencent_docs.debug_user_data_dir", browse="dir")
        self._field(tab, "Edge 驱动路径", "tencent_docs.driver_path", browse="file")
        self._field(tab, "页面超时（秒）", "tencent_docs.page_timeout", 15, float)
        self._check(tab, "覆盖未填完整的报名行（取消勾选=不覆盖）", "tencent_docs.overwrite_incomplete_rows", True)
        self._field(tab, "表头行号", "tencent_docs.selectors.header_row", 2, int)
        self._field(tab, "数据起始行号", "tencent_docs.selectors.data_start_row", 3, int)
        self._field(tab, "最大扫描行数", "tencent_docs.selectors.scan_max_rows", 200, int)
        self._field(tab, "提交模式", "tencent_docs.submit_mode", "autosave", choices=("autosave", "button"))
        self._lines(tab, "表头扫描列", "tencent_docs.selectors.header_scan_columns", 2)
        ttk.Button(tab, text="编辑表头别名", command=self.edit_aliases).pack(anchor="w", pady=8)

    def edit_aliases(self):
        if not self.save():
            return
        dialog = tk.Toplevel(self.root)
        dialog.title("动态表头别名")
        dialog.transient(self.root)
        dialog.grab_set()
        aliases = self._get("tencent_docs.selectors.header_aliases", {})
        fields = {}
        for row, (key, values) in enumerate(aliases.items()):
            ttk.Label(dialog, text=key).grid(row=row, column=0, padx=12, pady=5, sticky="w")
            var = tk.StringVar(value="，".join(values))
            fields[key] = var
            ttk.Entry(dialog, textvariable=var, width=42).grid(row=row, column=1, padx=12, pady=5)

        def apply():
            config = deepcopy(self.config)
            for key, var in fields.items():
                values = [value.strip() for value in var.get().replace("，", ",").split(",") if value.strip()]
                self._assign(config, f"tencent_docs.selectors.header_aliases.{key}", values)
            try:
                self._persist(config)
                dialog.destroy()
            except OSError as exc:
                messagebox.showerror("保存失败", str(exc), parent=dialog)
        ttk.Button(dialog, text="保存别名", command=apply).grid(row=len(fields), column=0, columnspan=2, pady=10)

    def _runtime(self):
        tab = self._tab("运行设置")
        self._check(tab, "演练模式", "runtime.dry_run", True)
        self.semester_start = self._field(tab, "学期起始日（周一）", "activity.semester_start_date",
                                           kind=lambda value: parse_semester_start(value).isoformat() if value else "")
        self.semester_week = tk.StringVar()
        ttk.Label(tab, textvariable=self.semester_week).pack(anchor="w", pady=(0, 6))
        self.semester_start.trace_add("write", lambda *_: self._update_semester_week())
        self._update_semester_week()
        self._field(tab, "轮询间隔（秒）", "runtime.poll_seconds", 1.0, float)
        self._field(tab, "无空位屏蔽次数", "runtime.no_slot_retry_limit", 3, int)
        self._field(tab, "打开失败额外重试次数（0=不重试）", "runtime.document_open_retries", 2, int)
        self._check(tab, "持续监测表格空位（开启时不按无空位次数屏蔽）", "runtime.monitor_documents", True)
        self._field(tab, "检查后等待（秒，0=持续检查）", "runtime.document_poll_seconds", 0, float)
        self._field(tab, "提前截止（天，0=不提前）", "runtime.document_deadline_days", 0, float)
        self._field(tab, "异常线程重启等待（秒）", "runtime.thread_restart_seconds", 2, float)
        self._field(tab, "已关闭表格日程同步间隔（秒，至少 1 秒）", "runtime.schedule_sync_seconds", 60, float)
        self._check(tab, "允许跨表时间冲突（在日程表标注）", "runtime.allow_cross_document_conflicts", False)
        self._field(tab, "时区", "runtime.timezone", "Asia/Shanghai")
        self._field(tab, "状态文件", "runtime.state_file", "data/seen_messages.json")
        self._field(tab, "日志文件", "runtime.log_file", "logs/volunteer_bot.log")
        ttk.Label(tab, text="活动默认星期").pack(anchor="w", pady=(10, 4))
        row = ttk.Frame(tab)
        row.pack(fill="x")
        days = []
        for index, day in enumerate(WEEKDAYS):
            var = tk.BooleanVar(value=index in self._get("activity.default_weekdays", [0, 1, 2, 3, 4]))
            days.append(var)
            ttk.Checkbutton(row, text=day, variable=var).pack(side="left", padx=(0, 8))
        self.bindings.append(("activity.default_weekdays", lambda: [i for i, var in enumerate(days) if var.get()], "活动默认星期"))

    def _update_semester_week(self):
        try:
            start = parse_semester_start(self.semester_start.get().strip())
            week = (datetime.now().date() - start).days // 7 + 1
            self.semester_week.set(f"当前：第 {week} 周" if week > 0 else "当前：学期尚未开始")
        except ValueError:
            self.semester_week.set("起始日未设置" if not self.semester_start.get().strip() else "起始日无效（YYYY-MM-DD，周一）")

    def _records(self):
        tab = self._tab("文档记录")
        self.records = self._tree(tab, {"mode": "模式", "time": "最近处理时间", "url": "文档链接 / 旧编号", "state": "处理状态", "attempts": "打开 / 无空位次数"}, 6)
        self.records.column("url", width=280)
        self.records.column("mode", width=70)
        self.records.bind("<<TreeviewSelect>>", self.show_record)
        self.record_detail = ScrolledText(tab, height=5, wrap="word", state="disabled")
        self.record_detail.pack(fill="x")
        bar = ttk.Frame(tab)
        bar.pack(fill="x")
        ttk.Button(bar, text="刷新记录", command=self.refresh_records).pack(side="left", padx=(0, 6))
        self.forget_button = ttk.Button(bar, text="允许所选文档重试", command=self.forget_document)
        self.forget_button.pack(side="left")
        ttk.Button(bar, text="复制所选链接", command=self.copy_record_url).pack(side="left", padx=6)
        entry = ttk.Frame(tab)
        entry.pack(fill="x", pady=12)
        ttk.Label(entry, text="文档链接").pack(side="left", padx=(0, 8))
        self.block_url = tk.StringVar()
        ttk.Entry(entry, textvariable=self.block_url).pack(side="left", fill="x", expand=True)
        self.block_mode = tk.StringVar(value="正式")
        ttk.Combobox(entry, textvariable=self.block_mode, values=("正式", "演练"), state="readonly", width=7).pack(side="left", padx=6)
        self.block_button = ttk.Button(entry, text="屏蔽链接", command=self.block_document)
        self.block_button.pack(side="left")
        self.refresh_records()

    def _state(self):
        return SeenState(str(self.path.parent / self._get("runtime.state_file", "data/seen_messages.json")))

    def _schedule(self):
        tab = self._tab("日程表")
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        self.agenda_week = today - timedelta(days=today.weekday())
        bar = ttk.Frame(tab)
        bar.pack(fill="x")
        self.agenda_caption = tk.StringVar()
        ttk.Button(bar, text="上一周", command=lambda: self.shift_week(-1)).pack(side="left")
        ttk.Button(bar, text="下一周", command=lambda: self.shift_week(1)).pack(side="left", padx=6)
        ttk.Label(bar, textvariable=self.agenda_caption).pack(side="left", padx=12)
        ttk.Button(bar, text="刷新日程", command=self.refresh_schedule).pack(side="right")
        ttk.Button(bar, text="回到本周", command=self.agenda_today).pack(side="right", padx=6)
        hours = ttk.Frame(tab)
        hours.pack(fill="x", pady=(8, 0))
        for key, label, default in (("start_time", "显示起始时间", "08:00"), ("end_time", "结束时间", "23:00")):
            ttk.Label(hours, text=label).pack(side="left", padx=(0, 6))
            var = tk.StringVar(value=str(self._get(f"calendar.{key}", default)))
            ttk.Entry(hours, textvariable=var, width=8).pack(side="left", padx=(0, 14))
            self.bindings.append((f"calendar.{key}", lambda var=var: var.get().strip().replace("：", ":"), label))
        ttk.Button(hours, text="保存并应用", command=self.save).pack(side="left")
        self.agenda_tree = WeekCalendar(tab, self.show_agenda)
        self.agenda_tree.pack(fill="both", expand=True, pady=(12, 6))
        legend = ttk.Frame(tab)
        legend.pack(fill="x")
        ttk.Label(legend, text="彩色色块：活动 / ICS 课程    灰色：不可用    黄色：待确认    红色：跨表冲突").pack(side="left")
        self.show_cancelled = tk.BooleanVar(value=False)
        ttk.Checkbutton(legend, text="显示已取消", variable=self.show_cancelled, command=self.refresh_schedule).pack(side="right")
        self.agenda_detail = tk.StringVar()
        ttk.Label(tab, textvariable=self.agenda_detail, wraplength=950).pack(anchor="w", pady=6)
        actions = ttk.Frame(tab)
        actions.pack(fill="x")
        ttk.Button(actions, text="复制本事项及冲突链接", command=self.copy_agenda_link).pack(side="left")
        self.confirm_agenda_button = ttk.Button(actions, text="确认所选已报名", command=lambda: self.edit_agenda(False))
        self.confirm_agenda_button.pack(side="left", padx=6)
        self.remove_agenda_button = ttk.Button(actions, text="移除所选本地占用", command=lambda: self.edit_agenda(True))
        self.remove_agenda_button.pack(side="left")
        ttk.Label(tab, text="跨表冲突标红，选中可查看重叠时间及双方链接；由你决定保留哪项报名。\n监听运行时按学号同步已知表格；线上取消后释放占用并保留取消记录，避免自动重报。\n移除只改本地日程，线上仍有报名则下次同步会重新导入。周历、文本和 ICS 在“不可用时间”编辑。", wraplength=950).pack(anchor="w", pady=8)
        self.refresh_schedule()

    def agenda_today(self):
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        self.agenda_week = today - timedelta(days=today.weekday())
        self.refresh_schedule()

    def shift_week(self, delta):
        self.agenda_week += timedelta(weeks=delta)
        self.refresh_schedule()

    def _refresh_visible_schedule(self):
        if self.tabs.tab(self.tabs.select(), "text") == "日程表":
            self.refresh_schedule()
        if not self.closing:
            self.root.after(5000, self._refresh_visible_schedule)

    def refresh_schedule(self):
        self.agenda_tree.set_range(self._get("calendar.start_time", "08:00"), self._get("calendar.end_time", "23:00"))
        start, end = self.agenda_week, self.agenda_week + timedelta(days=7)
        self.agenda_caption.set(f"{start:%Y-%m-%d} 至 {end - timedelta(days=1):%Y-%m-%d}")
        semester = self._get("activity.semester_start_date", "")
        if semester:
            try:
                number = (start.date() - parse_semester_start(semester)).days // 7 + 1
                self.agenda_caption.set(self.agenda_caption.get() + (f" · 第 {number} 周" if number > 0 else " · 开学前"))
            except ValueError:
                pass
        self.agenda_items = {}
        self.agenda_detail.set("点击色块查看完整时间、地点及报名链接")
        try:
            entries = []
            schedule = Schedule(self._state().path)
            conflicts = schedule.conflicts()
            for item in schedule.entries:
                entry = dict(item, start=datetime.fromisoformat(item["start"]), end=datetime.fromisoformat(item["end"]), conflicts=conflicts.get(item["id"], []))
                if entry["start"] < end and entry["end"] > start:
                    entries.append(entry)
            config = deepcopy(self.config)
            paths = config.get("unavailable", {}).get("ics_files", [])
            config.setdefault("unavailable", {})["ics_files"] = []
            entries.extend(dict(start=slot.start, end=slot.end, title="不可用时间（周历 / 文本规则）", status="忙碌", url="")
                           for slot in unavailable_slots(config, start, end))
            try:
                entries.extend(ics_entries(paths, start, end, config.get("runtime", {}).get("timezone", "Asia/Shanghai")))
            except (OSError, ValueError, KeyError) as exc:
                self._write(f"日程表读取 ICS 失败：{exc}")
            for entry in sorted(entries, key=lambda item: item["start"]):
                if entry["status"].startswith("已取消") and not self.show_cancelled.get():
                    continue
                key = entry.get("id") or f"{entry['start'].isoformat()}|{entry['end'].isoformat()}|{entry['title']}|{entry.get('url', '')}"
                self.agenda_items[key] = entry
            self.agenda_tree.set_entries(start, self.agenda_items)
            self.show_agenda()
        except (OSError, ValueError) as exc:
            self._write(f"读取报名日程失败：{exc}")

    def show_agenda(self, event=None):
        selected = self.agenda_tree.selection()
        if selected:
            entry = self.agenda_items[selected[0]]
            self.agenda_detail.set(f"{entry['title']}\n{entry['start']:%Y-%m-%d %H:%M} — {entry['end']:%Y-%m-%d %H:%M}；{entry['status']}；报名行：{entry.get('row', '—')}\n{entry['url']}")
            for other in entry.get("conflicts", []):
                start = max(entry["start"], datetime.fromisoformat(other["start"]))
                end = min(entry["end"], datetime.fromisoformat(other["end"]))
                self.agenda_detail.set(self.agenda_detail.get() + f"\n与 {other['title']} 冲突：{start:%Y-%m-%d %H:%M} — {end:%H:%M}\n{other['url']}")

    def copy_agenda_link(self):
        selected = self.agenda_tree.selection()
        if selected:
            entry = self.agenda_items[selected[0]]
            url = "\n".join(dict.fromkeys([entry["url"], *(other["url"] for other in entry.get("conflicts", []))]))
            if url:
                self.root.clipboard_clear()
                self.root.clipboard_append(url)

    def edit_agenda(self, remove):
        if self.process or self.busy:
            return
        selected = self.agenda_tree.selection()
        if selected:
            identifier = self.agenda_items[selected[0]].get("id")
            if identifier:
                Schedule(self._state().path).update(identifier, remove)
                self.refresh_schedule()

    def refresh_records(self):
        self.records.delete(*self.records.get_children())
        self.record_keys = {}
        self.record_history = {}
        try:
            state = self._state()
            for record in DocumentRecords(state.path).read():
                try:
                    key = (record["mode"], document_id(record["url"]))
                except ValueError:
                    continue
                self.record_history.setdefault(key, []).append(record)
            pairs = {(parts[0], parts[2]) for key in state.values for parts in [key.split(":")]
                     if len(parts) >= 3 and parts[1] in ("document", "no-slot", "manual-block")}
            pairs.update(self.record_history)
            for mode, identifier in sorted(pairs, key=lambda key: self.record_history.get(key, [{}])[-1].get("time", ""), reverse=True):
                attempts = state.count_prefix(f"{mode}:no-slot:{identifier}:")
                last = self.record_history.get((mode, identifier), [{}])[-1]
                if state.contains(f"{mode}:manual-block:{identifier}"):
                    label = "手动屏蔽"
                elif state.contains(f"{mode}:document:{identifier}"):
                    label = last.get("status", "已处理 / 无空位屏蔽" if attempts else "已处理")
                else:
                    label = last.get("status", "可重试")
                    if label == "打开失败，已跳过" or label == "无空位，已屏蔽":
                        label = "已解除，可重试"
                row = self.records.insert("", "end", values=("演练" if mode == "drill" else "正式", last.get("time", ""), last.get("url", identifier), label, f"{last.get('attempts', 0)} / {attempts}"))
                self.record_keys[row] = (mode, identifier)
        except (OSError, ValueError) as exc:
            self._write(f"读取记录失败：{exc}")

    def show_record(self, event=None):
        selected = self.records.selection()
        if not selected:
            return
        history = self.record_history.get(self.record_keys[selected[0]], [])
        if history:
            self.block_url.set(history[-1]["url"])
        text = "\n\n".join(f"{r['time']}  {r['status']}  打开 {r['attempts']} 次\n{r['url']}\n{r['reason']}" for r in reversed(history))
        self.record_detail.configure(state="normal")
        self.record_detail.delete("1.0", "end")
        self.record_detail.insert("1.0", text or "旧记录只保存了文档编号，没有原始链接和详细结果。")
        self.record_detail.configure(state="disabled")

    def copy_record_url(self):
        selected = self.records.selection()
        if selected:
            history = self.record_history.get(self.record_keys[selected[0]], [])
            if history:
                self.root.clipboard_clear()
                self.root.clipboard_append(history[-1]["url"])

    def forget_document(self):
        if self.process or self.busy:
            return
        selected = self.records.selection()
        if not selected:
            return
        try:
            state = self._state()
            for row in selected:
                state.forget_document(*self.record_keys[row])
            self.refresh_records()
            self._write("所选文档已允许重试；已重置对应模式的消息指纹，其他文档去重记录保留")
        except (OSError, ValueError) as exc:
            self._write(f"更新记录失败：{exc}")

    def block_document(self):
        if self.process or self.busy:
            return
        try:
            identifier = document_id(self.block_url.get().strip())
            mode = "live" if self.block_mode.get() == "正式" else "drill"
            state = self._state()
            state.add(f"{mode}:manual-block:{identifier}")
            state.add(f"{mode}:document:{identifier}")
            self.refresh_records()
            self._write(f"已屏蔽文档：{identifier}")
        except (ValueError, OSError) as exc:
            messagebox.showerror("屏蔽失败", str(exc), parent=self.root)

    @staticmethod
    def _assign(config, path, value):
        target = config
        keys = path.split(".")
        for key in keys[:-1]:
            target = target.setdefault(key, {})
        key = keys[-1]
        old = target.get(key)
        if old == value:
            return
        # Preserve YAML item comments and flow style when editing lists.
        if isinstance(old, list) and isinstance(value, list):
            for index, item in enumerate(value):
                if index < len(old):
                    if isinstance(old[index], dict) and isinstance(item, dict):
                        old[index].update(item)
                    else:
                        old[index] = item
                else:
                    old.append(item)
            del old[len(value):]
        else:
            target[key] = value

    def collect(self):
        config = deepcopy(self.config)
        for path, getter, label in self.bindings:
            try:
                value = getter()
            except (ValueError, tk.TclError) as exc:
                raise ValueError(f"{label}：请输入正确格式的值") from exc
            self._assign(config, path, value)
        display_range(config.get("calendar", {}).get("start_time", "08:00"), config.get("calendar", {}).get("end_time", "23:00"))
        return config

    def _persist(self, config):
        stream = StringIO()
        self.yaml.dump(config, stream)
        content = stream.getvalue()
        # Reload comment tokens after dumping; emitted copies must not be reused.
        loaded = self.yaml.load(content)
        self.path.write_text(content, encoding="utf-8")
        self.config = loaded
        self._update_summary()
        self._write("配置已保存" + ("，下次启动监听生效" if self.process else ""))

    def save(self):
        try:
            self._persist(self.collect())
            self.refresh_schedule()
            return True
        except (ValueError, OSError, YAMLError) as exc:
            messagebox.showerror("保存失败", str(exc), parent=self.root)
            return False

    def reload(self):
        if self.process or self.busy:
            return
        try:
            self.config = self.yaml.load(self.path.read_text(encoding="utf-8"))
        except (OSError, YAMLError) as exc:
            messagebox.showerror("载入失败", str(exc), parent=self.root)
            return
        selected = self.tabs.index(self.tabs.select())
        for tab in self.tabs.winfo_children():
            tab.destroy()
        self.bindings.clear()
        self._build_tabs()
        self.tabs.select(selected)
        self._update_summary()
        self._write("已从 config.yaml 重新载入")

    def edit_yaml(self):
        if self.process or self.busy or not self.save():
            return
        dialog = tk.Toplevel(self.root)
        dialog.title("完整配置 / config.yaml")
        dialog.geometry("820x680")
        dialog.transient(self.root)
        dialog.grab_set()
        editor = ScrolledText(dialog, wrap="none", font=("Consolas", 11))
        editor.pack(fill="both", expand=True, padx=10, pady=10)
        editor.insert("1.0", self.path.read_text(encoding="utf-8"))

        def apply():
            try:
                content = editor.get("1.0", "end-1c")
                config = self.yaml.load(content)
                if not isinstance(config, dict):
                    raise ValueError("配置根节点必须是映射")
                for key in ("profile", "qq", "runtime", "tencent_docs", "unavailable", "activity"):
                    if not isinstance(config.get(key, {}), dict):
                        raise ValueError(f"{key} 必须是映射")
                self.path.write_text(content + "\n", encoding="utf-8")
                dialog.destroy()
                self.reload()
            except Exception as exc:
                messagebox.showerror("保存失败", str(exc), parent=dialog)
        ttk.Button(dialog, text="保存并载入", command=apply).pack(pady=(0, 10))

    def _update_summary(self):
        mode = "演练" if self._get("runtime.dry_run", True) else "正式填写"
        names = self._get("qq.group_names", []) or self._get("qq.groups", [])
        groups = "可见会话" if self._get("qq.scan_all_groups", False) else "、".join(map(str, names))
        self.summary.set(f"已保存：{mode}    |    监听：{groups[:65] or '未指定'}    |    config.yaml")

    def open_local(self, key):
        try:
            os.startfile(self.path.parent / self._get(key))
        except OSError as exc:
            self._write(f"无法打开文件：{exc}")

    def preview(self):
        self.preview_tree.delete(*self.preview_tree.get_children())
        try:
            config = self.collect()
            message = extract_message(self.message_text.get("1.0", "end").strip())
            if message is None:
                raise ValueError("消息中没有识别到文档链接")
            activity = parse_activity(message,
                                      default_weekdays=tuple(config.get("activity", {}).get("default_weekdays", range(5))),
                                      semester_start_date=config.get("activity", {}).get("semester_start_date"))
            if not activity.slots:
                raise ValueError("消息中没有解析到日期或星期对应的时段")
            files = config.get("unavailable", {}).get("ics_files", [])
            config.setdefault("unavailable", {})["ics_files"] = [str(self.path.parent / file) for file in files]
            blocked = unavailable_slots(config, min(slot.start for slot in activity.slots), max(slot.end for slot in activity.slots))
            blocked.extend(Schedule(self.path.parent / config.get("runtime", {}).get("state_file", "data/seen_messages.json")).blocked(activity.url, config.get("runtime", {}).get("allow_cross_document_conflicts", False)))
            available = available_slots(list(activity.slots), blocked)
            for slot in activity.slots:
                ok = slot in available
                self.preview_tree.insert("", "end", values=(slot.start.strftime("%Y-%m-%d"), slot.start.strftime("%H:%M"),
                                         slot.end.strftime("%H:%M"), "可用" if ok else "时间冲突"), tags=("available" if ok else "blocked",))
            self._write(f"消息预览：可用 {len(available)}/{len(activity.slots)} 个时段；未打开文档或写入报名状态")
        except (ValueError, OSError, KeyError) as exc:
            self._write(f"预览失败：{exc}")

    def _set_busy(self, running):
        for button in (self.start_button, self.scan_button, self.edge_button, self.test_button,
                       self.forget_button, self.block_button, self.reload_button, self.advanced_button,
                       self.confirm_agenda_button, self.remove_agenda_button):
            button.configure(state="disabled" if running else "normal")
        self.stop_button.configure(state="normal" if self.process else "disabled")

    def start(self):
        self._launch([])

    def scan_once(self):
        self._launch(["--once", "--dry-run"])

    def _launch(self, args):
        if self.process or self.busy or not self.save():
            return
        try:
            env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
            self.process = subprocess.Popen(
                [sys.executable, "-u", str(PROJECT_DIR / "run.py"), str(self.path), "--gui", *args],
                cwd=self.path.parent, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", env=env, bufsize=1,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            threading.Thread(target=self._read, args=(self.process,), daemon=True).start()
            self.status.set("QQ 单次演练中" if args else "监听中 / " + ("演练" if self._get("runtime.dry_run") else "正式填写"))
            self._set_busy(True)
        except OSError as exc:
            self._write(f"启动失败：{exc}")

    def _read(self, process):
        for line in process.stdout:
            self.output_queue.put(("log", line.rstrip()))
        process.stdout.close()
        code = process.wait()
        process.stdin.close()
        self.output_queue.put(("exit", code))

    def stop(self):
        if self.process and self.process.poll() is None:
            try:
                self.process.stdin.write("stop\n")
                self.process.stdin.flush()
            except (OSError, ValueError):
                pass
            self.status.set("正在停止 / 等待当前任务完成")
            self.stop_button.configure(state="disabled")

    def open_edge(self, url="https://www.kdocs.cn"):
        if self.process or self.busy or not self.save():
            return
        config = deepcopy(self.config.get("tencent_docs", {}))
        self.busy = True
        self.status.set("正在打开 Edge")
        self._set_busy(True)

        def open_browser():
            try:
                TencentDocsAdapter(config).open_debug_browser(url)
                self.output_queue.put(("log", "Edge 调试会话已打开"))
            except Exception as exc:
                self.output_queue.put(("log", f"Edge 打开失败：{exc}"))
            finally:
                self.output_queue.put(("browser_done", None))
        threading.Thread(target=open_browser, daemon=True).start()

    def _write(self, text):
        tag = "error" if "ERROR" in text or "失败" in text else "warning" if "WARNING" in text else ""
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n", tag)
        self.log.see("end")
        self.log.configure(state="disabled")

    def clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _drain(self):
        for _ in range(200):
            try:
                kind, value = self.output_queue.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                self._write(value)
            elif kind == "exit":
                self.process = None
                self.status.set("已停止" if value == 0 else f"运行失败 / 退出码 {value}")
                self._write(f"监听进程已退出，代码={value}")
                self._set_busy(False)
                self.refresh_records()
                self.refresh_schedule()
            elif kind == "browser_done":
                self.busy = False
                self.status.set("未启动")
                self._set_busy(False)
        if self.closing and not self.process and not self.busy:
            self.root.destroy()
            return
        self.root.after(120, self._drain)

    def _close(self):
        self.closing = True
        self.stop()
        if not self.process and not self.busy:
            self.root.destroy()


def main():
    root = tk.Tk()
    try:
        ConfigApp(root, PROJECT_DIR / "config.yaml")
    except Exception as exc:
        root.withdraw()
        messagebox.showerror("控制台启动失败", str(exc), parent=root)
        root.destroy()
        raise SystemExit(1)
    root.mainloop()


if __name__ == "__main__":
    main()
