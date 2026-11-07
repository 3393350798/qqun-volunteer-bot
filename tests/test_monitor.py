import json
import threading
from datetime import datetime

import pytest

from run import handle_message
from src.docs_adapter import TencentDocsAdapter, TencentDocsError
from src.models import Activity, TimeSlot
from src.monitor import DocumentMonitor
from src.parser import DOC_RE, document_id, extract_message
from src.schedule import Schedule
from src.state import SeenState, DocumentRecords


def slot(start=9, end=10):
    return TimeSlot(datetime(2099, 9, 14, start), datetime(2099, 9, 14, end))


class Docs:
    def __init__(self, config):
        self.full = True
        self.connected = False
        self.connections = 0
        self.written = []
        self.closes = []
        self.fail_during_write = False
        self.on_slot_start = self.on_slot_done = None

    def open(self, url):
        if not self.connected:
            self.connections += 1
            self.connected = True

    def submit(self, activity, slots, profile, dry_run):
        if self.full:
            raise TencentDocsError("找不到匹配且为空的报名行")
        for s in slots:
            if self.on_slot_start(s, 4, "测试点位"):
                if self.fail_during_write:
                    raise RuntimeError("写入中断")
                self.written.append(s)
                self.on_slot_done(s)
        return len(slots)

    def close(self, keep_tab=False, complete=False):
        self.closes.append(keep_tab)
        self.connected = False


@pytest.fixture
def monitor(tmp_path, monkeypatch):
    state = SeenState(str(tmp_path / "seen.json"))
    monkeypatch.setattr("run.parse_activity", lambda message, **kw: Activity(DOC_RE.search(message.text).group(0), "测试志愿", (slot(),)))
    return DocumentMonitor({"runtime": {"dry_run": False, "monitor_documents": True, "no_slot_retry_limit": 1}},
                           state, DocumentRecords(state.path), Schedule(state.path), threading.Event(), handle_message, Docs)


def add(monitor, name):
    url = f"https://kdocs.cn/l/{name}"
    monitor.add(extract_message(f"测试报名 {url}"))
    key = document_id(url)
    return key, monitor.jobs[key]["docs"]


def test_full_document_stays_open_then_fills_vacancy(monitor):
    key, docs = add(monitor, "A")
    monitor.check(key)
    assert key in monitor.jobs and not docs.closes
    assert not monitor.state.contains(f"live:document:{key}")
    assert monitor.records.read()[-1]["status"] == "监测中，等待空位"
    docs.full = False
    monitor.check(key)
    assert docs.connections == 1 and docs.written == [slot()]
    assert monitor.schedule.entries[0]["status"] == "已报名"
    assert key not in monitor.jobs and docs.closes == [False]
    assert docs.written == [slot()]


def test_second_document_cannot_take_registered_time(monitor):
    first, a = add(monitor, "A")
    second, b = add(monitor, "B")
    a.full = b.full = False
    monitor.check(first)
    monitor.check(second)
    assert a.written == [slot()] and b.written == []
    assert len(monitor.schedule.entries) == 1


def test_two_document_links_in_one_message_are_both_monitored(monitor):
    monitor.add(extract_message("测试报名 https://kdocs.cn/l/A https://kdocs.cn/l/B"))
    assert set(monitor.jobs) == {document_id("https://kdocs.cn/l/A"), document_id("https://kdocs.cn/l/B")}
    assert all(len(DOC_RE.findall(job["message"].text)) == 1 for job in monitor.jobs.values())


def test_interrupted_write_stays_blocked_across_restart(monitor):
    first, a = add(monitor, "A")
    a.full, a.fail_during_write = False, True
    monitor.check(first)
    restored = Schedule(monitor.state.path)
    assert restored.entries[0]["status"].startswith("待确认")
    monitor.schedule = restored
    second, b = add(monitor, "B")
    b.full = False
    monitor.check(second)
    assert not b.written


def test_adjacent_slots_are_allowed_but_overlapping_are_not(tmp_path):
    schedule = Schedule(tmp_path / "seen.json")
    a = Activity("https://kdocs.cn/l/A", "志愿 A", ())
    b = Activity("https://kdocs.cn/l/B", "志愿 B", ())
    assert schedule.begin(a, slot(9, 11), 4, "点位 A")
    assert not schedule.begin(b, slot(10, 12), 5, "点位 B")
    assert schedule.begin(b, slot(11, 12), 6, "点位 B")


def test_watched_list_restores_and_retains_schedule(monitor):
    key, docs = add(monitor, "A")
    monitor.check(key)
    saved = json.loads(monitor.path.read_text(encoding="utf-8"))
    assert saved[0]["text"].endswith("/A")
    from src.models import Message
    monitor.jobs.clear()
    monitor.add(Message(**saved[0]))
    assert key in monitor.jobs


def test_qq_producer_can_queue_during_browser_work(monitor):
    entered, release = threading.Event(), threading.Event()
    def handler(*args, outcome, **kwargs):
        entered.set()
        release.wait(3)
        outcome["status"] = "监测中，等待空位"
    monitor.handler = handler
    monitor.queue.put(extract_message("报名 https://kdocs.cn/l/A"))
    thread = threading.Thread(target=monitor.run)
    thread.start()
    try:
        assert entered.wait(2)
        monitor.queue.put(extract_message("报名 https://kdocs.cn/l/B"))
        assert monitor.queue.qsize() == 1
    finally:
        monitor.stop_event.set()
        release.set()
        thread.join(3)
    assert not thread.is_alive()


def test_existing_tab_reused_and_reselected_without_navigation(monkeypatch):
    class Switch:
        def __init__(self, driver): self.driver = driver
        def window(self, handle): self.driver.current_window_handle = handle
        def new_window(self, kind): raise AssertionError("must reuse existing tab")
    class Driver:
        window_handles = ["other", "registration"]
        current_window_handle = "other"
        def __init__(self): self.switch_to = Switch(self)
        @property
        def current_url(self):
            return "https://kdocs.cn/l/A?from=qq" if self.current_window_handle == "registration" else "https://example.org"
        def get(self, url): raise AssertionError("must not reload existing document")
        def close(self): raise AssertionError("must retain tab")
    driver = Driver()
    monkeypatch.setattr("selenium.webdriver.Edge", lambda **kw: driver)
    adapter = TencentDocsAdapter({"debugger_address": "127.0.0.1:9222", "reuse_document_tabs": True})
    adapter.open("https://kdocs.cn/l/A")
    driver.current_window_handle = "other"
    adapter.open("https://kdocs.cn/l/A")
    assert driver.current_window_handle == "registration"
    adapter.close(keep_tab=True)
