"""A time-based weekly timetable for the local Tkinter calendar."""
import hashlib
import tkinter as tk
from tkinter import ttk
from datetime import datetime, timedelta


def display_range(start="08:00", end="23:00"):
    def minutes(value):
        parts = str(value).strip().replace("：", ":").split(":")
        if len(parts) != 2:
            raise ValueError("日程表时间请填写 HH:MM，例如 08:00")
        hour, minute = map(int, parts)
        if not (0 <= hour <= 24 and 0 <= minute < 60) or (hour == 24 and minute):
            raise ValueError("日程表时间应在 00:00 至 24:00 之间")
        return hour * 60 + minute
    first, last = minutes(start), minutes(end)
    if first >= last:
        raise ValueError("日程表结束时间必须晚于起始时间")
    return first, last


class WeekCalendar(ttk.Frame):
    def __init__(self, parent, on_select):
        super().__init__(parent)
        self.on_select = on_select
        self.entries, self.selected = {}, None
        self.week = datetime.now()
        self.first_minute, self.last_minute = 8 * 60, 23 * 60
        self.header = tk.Canvas(self, height=56, background="#f8f9fd", highlightthickness=0)
        self.header.grid(row=0, column=0, sticky="ew")
        self.canvas = tk.Canvas(self, height=470, background="white", highlightthickness=0)
        self.canvas.grid(row=1, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        scroll.grid(row=1, column=1, sticky="ns")
        self.canvas.configure(yscrollcommand=scroll.set)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        self.canvas.bind("<Configure>", lambda _: self.draw())
        self.canvas.bind("<MouseWheel>", self._scroll)

    def _scroll(self, event):
        self.canvas.yview_scroll(-int(event.delta / 120), "units")
        return "break"

    def selection(self):
        return (self.selected,) if self.selected in self.entries else ()

    def set_range(self, start, end):
        first, last = display_range(start, end)
        if (first, last) != (self.first_minute, self.last_minute):
            self.first_minute, self.last_minute = first, last
            self.draw()
            self.canvas.yview_moveto(0)

    def set_entries(self, week, entries):
        changed_week = self.week != week
        self.week, self.entries = week, entries
        if self.selected not in entries:
            self.selected = None
        self.draw()
        if changed_week:
            self.canvas.yview_moveto(0)

    def choose(self, key):
        self.selected = key
        self.draw()
        self.on_select()

    def draw(self):
        c, h = self.canvas, self.header
        c.delete("all")
        h.delete("all")
        width = max(c.winfo_width(), 700)
        left, hour_height = 52, 72
        day_width = (width - left) / 7
        height = (self.last_minute - self.first_minute) / 60 * hour_height
        c.configure(scrollregion=(0, 0, width, height + 20))
        now = datetime.now()
        h.create_text(26, 28, text=f"{self.week:%m}月", fill="#727c94", font=("Microsoft YaHei UI", 10))
        for day in range(7):
            date = self.week + timedelta(days=day)
            x = left + day * day_width
            today = date.date() == now.date()
            if today:
                c.create_rectangle(x, 0, x + day_width, height, fill="#f6f7ff", outline="")
                h.create_rectangle(x + 4, 3, x + day_width - 4, 53, fill="#ecebff", outline="")
            h.create_text(x + day_width / 2, 17, text="周" + "一二三四五六日"[day], fill="#6254ce" if today else "#34405a", font=("Microsoft YaHei UI", 10, "bold"))
            h.create_text(x + day_width / 2, 38, text=date.strftime("%m/%d") + (" · 今天" if today else ""), fill="#6254ce" if today else "#8991a3", font=("Microsoft YaHei UI", 9))
            c.create_line(x, 0, x, height, fill="#f0f1f7")
        ticks = sorted({self.first_minute, self.last_minute, *range((self.first_minute // 60 + 1) * 60, self.last_minute, 60)})
        for minute in ticks:
            y = (minute - self.first_minute) / 60 * hour_height
            c.create_line(left, y, width, y, fill="#eaeef5")
            c.create_text(left - 7, y + 9, anchor="e", text=f"{minute // 60:02d}:{minute % 60:02d}", fill="#929bad", font=("Segoe UI", 9))
        palette = [("#e9e4ff", "#57408e"), ("#dff1ed", "#246b5b"), ("#e0edff", "#305e98"), ("#ffecd9", "#93652e")]
        for day in range(7):
            midnight = self.week + timedelta(days=day)
            start = midnight + timedelta(minutes=self.first_minute)
            end = midnight + timedelta(minutes=self.last_minute)
            segments = []
            for key, entry in self.entries.items():
                a, b = max(start, entry["start"]), min(end, entry["end"])
                if a < b:
                    segments.append(dict(key=key, a=a, b=b, entry=entry))
            # Overlaps get separate columns, including imported classes and overnight events.
            groups = []
            for segment in sorted(segments, key=lambda s: (s["a"], s["b"])):
                if not groups or segment["a"] >= max(s["b"] for s in groups[-1]):
                    groups.append([])
                groups[-1].append(segment)
            for group in groups:
                lanes = []
                for segment in group:
                    lane = next((i for i, until in enumerate(lanes) if until <= segment["a"]), len(lanes))
                    if lane == len(lanes):
                        lanes.append(segment["b"])
                    else:
                        lanes[lane] = segment["b"]
                    segment["lane"] = lane
                for segment in group:
                    entry, key = segment["entry"], segment["key"]
                    lane_width = day_width / len(lanes)
                    x = left + day * day_width + segment["lane"] * lane_width + 3
                    y = (segment["a"] - start).total_seconds() / 3600 * hour_height + 2
                    bottom = (segment["b"] - start).total_seconds() / 3600 * hour_height - 2
                    bottom = max(bottom, y + 8)
                    seed = entry.get("url") or entry["title"]
                    fill, ink = palette[int(hashlib.sha256(seed.encode()).hexdigest()[:4], 16) % len(palette)]
                    if entry["status"] == "忙碌":
                        fill, ink = "#edf0f5", "#646f83"
                    if entry["status"].startswith("待确认"):
                        fill, ink = "#fff0cf", "#906413"
                    if entry.get("conflicts"):
                        fill, ink = "#ffe2e5", "#a83248"
                    if entry["status"].startswith("已取消"):
                        fill, ink = "#f3f3f3", "#a0a0a0"
                    tag = f"event-{key}"
                    right = x + lane_width - 6
                    radius = min(8, (bottom - y) / 3, (right - x) / 3)
                    c.create_polygon(x + radius, y, right - radius, y, right, y, right, y + radius,
                                     right, bottom - radius, right, bottom, right - radius, bottom,
                                     x + radius, bottom, x, bottom, x, bottom - radius, x, y + radius, x, y,
                                     smooth=True, fill=fill, outline="#6654cf" if key == self.selected else "", width=2, tags=tag)
                    text_width = max(8, lane_width - 16)
                    chars = max(1, int(text_width / 13))
                    title = entry["title"].strip("【】")
                    title = title[:chars] + ("…" if len(title) > chars else "")
                    lines = [title]
                    if bottom - y > 40:
                        lines.append(f"{entry['start']:%H:%M}–{entry['end']:%H:%M}")
                    if bottom - y > 64 and entry.get("location"):
                        lines.append(entry["location"][:chars])
                    if bottom - y > 86 and entry.get("conflicts"):
                        lines.append("时间冲突")
                    if bottom - y >= 22:
                        c.create_text(x + 5, y + 4, anchor="nw", text="\n".join(lines), font=("Microsoft YaHei UI", 9), fill=ink, width=text_width, tags=tag)
                    c.tag_bind(tag, "<Button-1>", lambda _, key=key: self.choose(key))
        minute_now = now.hour * 60 + now.minute
        if self.week.date() <= now.date() < (self.week + timedelta(days=7)).date() and self.first_minute <= minute_now <= self.last_minute:
            x = left + (now.date() - self.week.date()).days * day_width
            y = (minute_now - self.first_minute) / 60 * hour_height
            c.create_line(x, y, x + day_width, y, fill="#f06b83", width=2)
