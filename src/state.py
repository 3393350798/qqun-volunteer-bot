import json
from datetime import datetime
from pathlib import Path
from threading import RLock


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


class DocumentRecords:
    """Local link history, stored beside the deduplication state."""

    def __init__(self, state_path):
        self.path = Path(state_path).with_name("document_records.json")
        self.lock = RLock()

    def read(self):
        with self.lock:
            return json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else []

    def add(self, mode, url, status, reason="", attempts=0):
        with self.lock:
            rows = self.read()
            previous = next((r for r in reversed(rows) if r["mode"] == mode and r["url"] == url), None)
            if previous and (previous["status"], previous["reason"], previous["attempts"]) == (status, reason, attempts):
                return
            rows.append(dict(time=datetime.now().isoformat(timespec="seconds"), mode=mode,
                             url=url, status=status, reason=reason, attempts=attempts))
            write_json(self.path, rows)


class SeenState:
    def __init__(self, path: str):
        self.path = Path(path)
        self.lock = RLock()
        self.values = set(json.loads(self.path.read_text(encoding="utf-8"))) if self.path.exists() else set()

    def contains(self, value: str) -> bool:
        with self.lock:
            return value in self.values

    def add(self, value: str) -> None:
        with self.lock:
            self.values.add(value)
            self.save()

    def save(self) -> None:
        with self.lock:
            write_json(self.path, sorted(self.values))

    def count_prefix(self, prefix: str) -> int:
        with self.lock:
            return sum(1 for value in self.values if value.startswith(prefix))

    def forget_document(self, mode: str, identifier: str) -> None:
        # Old records do not associate message hashes with document hashes.
        prefixes = (f"{mode}:no-slot:{identifier}:", f"{mode}:message:")
        self.values = {value for value in self.values
                       if value not in (f"{mode}:document:{identifier}", f"{mode}:manual-block:{identifier}")
                       and not value.startswith(prefixes)}
        self.save()
