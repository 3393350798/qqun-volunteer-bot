import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import pytest

from run import handle_message
from src.models import Activity, TimeSlot
from src.monitor import DocumentMonitor
from src.parser import document_id, extract_message
from src.schedule import Schedule
from src.state import DocumentRecords, SeenState
from test_monitor import Docs


def make_monitor(tmp_path, config=None):
    state = SeenState(str(tmp_path / "seen.json"))
    return DocumentMonitor({"runtime": {"dry_run": False, "monitor_documents": True, **(config or {})}},
                           state, DocumentRecords(state.path), Schedule(state.path), threading.Event(), handle_message, Docs)


def test_two_documents_enter_handler_simultaneously(tmp_path):
    monitor = make_monitor(tmp_path)
    barrier = threading.Barrier(2)
    entered = []
    def handler(message, *args, outcome, **kwargs):
        barrier.wait(3)
        entered.append(message.text)
        outcome.update(status="填写完成", finished=True)
    monitor.handler = handler
    monitor.add(extract_message("https://kdocs.cn/l/A https://kdocs.cn/l/B"))
    monitor.start_pending()
    for thread in monitor.threads:
        thread.join(4)
    assert len(entered) == 2 and not monitor.jobs
    assert all(not thread.is_alive() for thread in monitor.threads)


@pytest.mark.parametrize("allow_cross, expected", [(False, 1), (True, 2)])
def test_simultaneous_reservations_obey_cross_table_option(tmp_path, allow_cross, expected):
    schedule = Schedule(tmp_path / "seen.json")
    slot = TimeSlot(datetime(2099, 9, 10, 12), datetime(2099, 9, 10, 13))
    barrier = threading.Barrier(2)
    def reserve(name):
        barrier.wait(2)
        return schedule.begin(Activity(f"https://kdocs.cn/l/{name}", name, ()), slot, 4, "", allow_cross)
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(reserve, ["A", "B"])) == expected
    restored = Schedule(schedule.path)
    assert len(restored.entries) == expected
    if allow_cross:
        conflicts = restored.conflicts()
        assert all(len(value) == 1 for value in conflicts.values())
        assert not schedule.begin(Activity("https://kdocs.cn/l/A", "A", ()), slot, 5, "", True)


@pytest.mark.parametrize("now, closed", [(datetime(2099, 9, 9, 11, 59), False), (datetime(2099, 9, 9, 12), True)])
def test_deadline_uses_last_unfilled_start_minus_days(tmp_path, monkeypatch, now, closed):
    class Clock(datetime):
        @classmethod
        def now(cls): return now
    monkeypatch.setattr("run.datetime", Clock)
    slot = TimeSlot(datetime(2099, 9, 10, 12), datetime(2099, 9, 10, 13))
    url = "https://kdocs.cn/l/A"
    monkeypatch.setattr("run.parse_activity", lambda *a, **kw: Activity(url, "测试", (slot,)))
    monitor = make_monitor(tmp_path, {"document_deadline_days": 1})
    monitor.add(extract_message(url))
    key = document_id(url)
    docs = monitor.jobs[key]["docs"]
    monitor.check(key)
    assert (key not in monitor.jobs) == closed
    if closed:
        assert docs.connections == 0 and docs.closes == [False]
        assert "2099-09-09 12:00" in monitor.records.read()[-1]["reason"]
    else:
        assert monitor.jobs[key]["deadline"] == datetime(2099, 9, 9, 12)


def test_deadline_recalculates_after_latest_slot_is_filled(tmp_path, monkeypatch):
    now = datetime(2099, 9, 9, 10)
    class Clock(datetime):
        @classmethod
        def now(cls): return now
    monkeypatch.setattr("run.datetime", Clock)
    earlier = TimeSlot(datetime(2099, 9, 10, 9), datetime(2099, 9, 10, 10))
    latest = TimeSlot(datetime(2099, 9, 11, 12), datetime(2099, 9, 11, 13))
    url = "https://kdocs.cn/l/A"
    monkeypatch.setattr("run.parse_activity", lambda *a, **kw: Activity(url, "测试", (earlier, latest)))
    monitor = make_monitor(tmp_path, {"document_deadline_days": 1})
    monitor.add(extract_message(url))
    key = document_id(url)
    docs = monitor.jobs[key]["docs"]
    def submit(*args):
        assert docs.on_slot_start(latest, 4, "")
        docs.on_slot_done(latest)
        assert not docs.on_slot_start(earlier, 5, "")
        return 1
    docs.submit = submit
    monitor.check(key)
    assert key not in monitor.jobs and docs.closes == [False]
    assert len(monitor.schedule.entries) == 1


def test_wait_is_shortened_by_deadline(tmp_path):
    monitor = make_monitor(tmp_path, {"document_poll_seconds": 3600})
    def handler(*args, outcome, **kwargs):
        outcome.update(status="监测中，等待空位", deadline=datetime.now() + timedelta(seconds=2))
    monitor.handler = handler
    monitor.add(extract_message("https://kdocs.cn/l/A"))
    delays = []
    def wait(delay):
        delays.append(delay)
        return True
    monitor.stop_event.wait = wait
    monitor._document_loop(next(iter(monitor.jobs)))
    assert len(delays) == 1 and 0 < delays[0] <= 2


def test_parallel_history_and_state_writes_are_preserved(tmp_path):
    state = SeenState(str(tmp_path / "seen.json"))
    records = DocumentRecords(state.path)
    def write(index):
        state.add(f"live:document:{index}")
        records.add("live", f"https://kdocs.cn/l/{index}", "填写完成")
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(write, range(20)))
    assert len(SeenState(str(state.path)).values) == len(records.read()) == 20


def test_zero_interval_has_no_extra_wait(tmp_path):
    monitor = make_monitor(tmp_path)
    assert monitor.interval == 0
