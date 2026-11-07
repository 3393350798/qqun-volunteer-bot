"""Shared calendar of confirmed and potentially written registrations."""
import json
from datetime import datetime
from pathlib import Path
from threading import RLock

from .models import TimeSlot
from .parser import document_id, message_id


class Schedule:
    def __init__(self, state_path):
        self.path = Path(state_path).with_name("schedule.json")
        self.lock = RLock()
        self.entries = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else []

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(self.entries, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    def blocked(self, url=None, allow_cross=False):
        with self.lock:
            return [TimeSlot(datetime.fromisoformat(e["start"]), datetime.fromisoformat(e["end"]))
                    for e in self.entries if not e["status"].startswith("已取消")
                    and (not allow_cross or url is None or document_id(e["url"]) == document_id(url))]

    def cancelled(self, url):
        with self.lock:
            return [TimeSlot(datetime.fromisoformat(e["start"]), datetime.fromisoformat(e["end"]))
                    for e in self.entries if e["status"].startswith("已取消") and document_id(e["url"]) == document_id(url)]

    def reconcile(self, activity, own_rows, snapshot):
        """Import exact student-ID matches; missing confirmed rows release occupancy."""
        with self.lock:
            before = json.dumps(self.entries, ensure_ascii=False)
            same = [e for e in self.entries if document_id(e["url"]) == document_id(activity.url)]
            present = set()
            for own in sorted(own_rows, key=lambda item: item["complete"]):
                slot = own["slot"]
                identifier = message_id(f"{document_id(activity.url)}:{slot.start}:{slot.end}")
                present.add(identifier)
                entry = next((e for e in self.entries if e["id"] == identifier), None)
                if entry is None:
                    entry = dict(id=identifier, title=activity.title, url=activity.url,
                                 start=slot.start.isoformat(), end=slot.end.isoformat())
                    self.entries.append(entry)
                entry.update(row=own["row"], location=own["location"],
                             status="已报名" if own["complete"] else "待确认（表格资料不完整）")
            # Incomplete scans must not erase previously confirmed registrations.
            if not snapshot["unresolved"] and snapshot["rows"]:
                for entry in same:
                    if entry["id"] in present or entry["status"].startswith(("已取消", "待确认（填写已开始）")):
                        continue
                    row = int(entry["row"]) if str(entry["row"]).isdigit() else -1
                    if snapshot["first"] <= row < snapshot["end"]:
                        # A removed record is remembered so monitoring will not sign it up again.
                        entry["status"] = "已取消（表格中已移除或变更）"
            if json.dumps(self.entries, ensure_ascii=False) != before:
                self.save()

    def known_urls(self):
        with self.lock:
            return list(dict.fromkeys(e["url"] for e in self.entries))

    def begin(self, activity, slot, row, location, allow_cross=False):
        with self.lock:
            if any(slot.overlaps(other) for other in [*self.blocked(activity.url, allow_cross), *self.cancelled(activity.url)]):
                return False
            self.entries.append(dict(id=message_id(f"{document_id(activity.url)}:{slot.start}:{slot.end}"),
                                     title=activity.title, url=activity.url, start=slot.start.isoformat(),
                                     end=slot.end.isoformat(), row=str(row), location=location,
                                     status="待确认（填写已开始）"))
            self.save()
            return True

    def confirm(self, activity, slot):
        with self.lock:
            for entry in self.entries:
                if document_id(entry["url"]) == document_id(activity.url) and entry["start"] == slot.start.isoformat() and entry["end"] == slot.end.isoformat():
                    entry["status"] = "已报名"
            self.save()

    def update(self, identifier, remove=False):
        with self.lock:
            if remove:
                self.entries = [e for e in self.entries if e["id"] != identifier]
            else:
                for entry in self.entries:
                    if entry["id"] == identifier:
                        entry["status"] = "已报名"
            self.save()

    def conflicts(self):
        with self.lock:
            result = {entry["id"]: [] for entry in self.entries}
            active = [e for e in self.entries if not e["status"].startswith("已取消")]
            for index, entry in enumerate(active):
                slot = TimeSlot(datetime.fromisoformat(entry["start"]), datetime.fromisoformat(entry["end"]))
                for other in active[index + 1:]:
                    if document_id(entry["url"]) != document_id(other["url"]) and slot.overlaps(TimeSlot(datetime.fromisoformat(other["start"]), datetime.fromisoformat(other["end"]))):
                        result[entry["id"]].append(dict(other))
                        result[other["id"]].append(dict(entry))
            return result
