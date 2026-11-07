import logging
import threading
from datetime import datetime
from types import SimpleNamespace

import pytest

from run import handle_message
from src.docs_adapter import TencentDocsError
from src.models import Activity, Message, TimeSlot
from src.parser import document_id, extract_message
from src.qq_adapter import QQWindowReader
from src.schedule import Schedule
from src.state import SeenState
from src.table_schedule import dates_in, row_slots
from test_core import FakeCanvasDriver, _dynamic_adapter
from test_parallel_monitor import make_monitor


URL = "https://kdocs.cn/l/TEST"
PROFILE = dict(name="测试", college="学院", student_id="TEST01", phone="13800000000")


def sheet(extra=None):
    cells = {"A2": "时段", "B2": "地点", "C2": "日期", "F2": "姓名", "G2": "学院", "H2": "学号", "I2": "手机",
             "A3": "9:30-10:15", "B3": "测试岗位", "C3": "2099-09-20"}
    cells.update(extra or {})
    driver = FakeCanvasDriver(cells)
    adapter = _dynamic_adapter(driver)
    return adapter, driver


def rebuild(adapter, text=URL):
    return adapter.rebuild_activity(Activity(URL, "测试活动", ()), Message("test", text), "2026-08-31")


@pytest.mark.parametrize("overwrite, filled", [(True, True), (False, False)])
def test_partial_overwrite_switch(overwrite, filled):
    adapter, driver = sheet({"F3": "别人填写到一半"})
    adapter.config["overwrite_incomplete_rows"] = overwrite
    activity = rebuild(adapter)
    if filled:
        assert adapter.submit(activity, list(activity.slots), PROFILE, False) == 1
        assert driver.cells["H3"] == PROFILE["student_id"]
    else:
        with pytest.raises(TencentDocsError, match="找不到匹配"):
            adapter.submit(activity, list(activity.slots), PROFILE, False)
        assert driver.cells["F3"] == "别人填写到一半"


def test_completed_row_is_not_overwritten_even_when_changed_after_scan():
    adapter, driver = sheet()
    activity = rebuild(adapter)
    driver.cells.update(F3="别人", G3="学院", H3="OTHER", I3="13900000000")
    assert adapter.submit(activity, list(activity.slots), PROFILE, False) == 0
    assert driver.cells["H3"] == "OTHER"


def test_table_only_dates_and_times_ignore_message_schedule():
    adapter, _ = sheet({"A4": "18:00-19:00", "C4": "2099年9月21日"})
    activity = rebuild(adapter, "9月22日 10:00-11:00 " + URL)
    assert [(s.start.day, s.start.hour) for s in activity.slots] == [(20, 9), (21, 18)]
    assert len(adapter._table_snapshot["rows"]) == 2


def test_date_only_announcement_provides_anchor_not_neighbour_message():
    adapter, _ = sheet({"C2": "星期", "C3": "周日"})
    activity = rebuild(adapter, "2026年9月20日（周日）篮球志愿 " + URL)
    assert activity.slots[0].start == datetime(2026, 9, 20, 9, 30)


def test_sheet_title_week_reconstructs_without_announcement():
    adapter, _ = sheet({"A1": "2026第三周交通保障", "C2": "星期", "C3": "周一", "C4": "2", "A4": "17:30-18:15"})
    activity = rebuild(adapter)
    assert [(s.start.month, s.start.day) for s in activity.slots] == [(9, 14), (9, 15)]


def test_unknown_weekday_date_is_not_guessed():
    adapter, _ = sheet({"C2": "星期", "C3": "周一"})
    assert rebuild(adapter).slots == ()
    assert adapter._table_snapshot["unresolved"] == [3]


def test_time_range_and_link_parameters_are_not_dates():
    assert dates_in("9:30-10:15 17：30-18：15 " + URL + "?from=09-20", 2026) == []
    assert dates_in("2026-09-20", 2000)[0].year == 2026


def test_separate_excel_fraction_times_and_overnight():
    slots = row_slots(dict(date="2026/9/20", start="0.75", end="0.25"), Message("x", URL), Activity(URL, "", ()), None)
    assert slots == [TimeSlot(datetime(2026, 9, 20, 18), datetime(2026, 9, 21, 6))]


def test_sync_import_move_cancel_and_no_automatic_reenrol(tmp_path):
    adapter, driver = sheet(dict(F3=PROFILE["name"], G3=PROFILE["college"], H3=PROFILE["student_id"], I3=PROFILE["phone"]))
    schedule = Schedule(tmp_path / "seen.json")
    activity = rebuild(adapter)
    adapter.sync_schedule(schedule, activity, PROFILE)
    assert len(schedule.entries) == 1 and schedule.entries[0]["status"] == "已报名"
    for col in "FGHI":
        driver.cells[col + "4"] = driver.cells.pop(col + "3")
    adapter.sync_schedule(schedule, rebuild(adapter), PROFILE)
    assert len(schedule.entries) == 1 and schedule.entries[0]["row"] == "4"
    for col in "FGHI":
        driver.cells.pop(col + "4")
    adapter.sync_schedule(schedule, rebuild(adapter), PROFILE)
    assert schedule.entries[0]["status"].startswith("已取消")
    assert not schedule.blocked()
    assert not schedule.begin(activity, activity.slots[0], 3, "", True)


def test_sync_does_not_erase_on_unresolved_time_or_incomplete_write(tmp_path):
    adapter, driver = sheet(dict(F3="我", G3="学院", H3="TEST01", I3="13800000000"))
    schedule = Schedule(tmp_path / "seen.json")
    adapter.sync_schedule(schedule, rebuild(adapter), PROFILE)
    driver.cells.update(A3="待定", H3="OTHER")
    adapter.sync_schedule(schedule, rebuild(adapter), PROFILE)
    assert schedule.entries[0]["status"] == "已报名"
    driver.cells.update(A3="9:30-10:15", H3="TEST01", I3="")
    adapter.sync_schedule(schedule, rebuild(adapter), PROFILE)
    assert schedule.entries[0]["status"].startswith("待确认")


def test_sync_exact_student_id_and_conflicts(tmp_path):
    schedule = Schedule(tmp_path / "seen.json")
    adapter, driver = sheet(dict(F3=PROFILE["name"], G3="学院", H3="SOMEONE", I3="13800000000"))
    activity = rebuild(adapter)
    adapter.sync_schedule(schedule, activity, PROFILE)
    assert not schedule.entries
    driver.cells["H3"] = PROFILE["student_id"]
    adapter.sync_schedule(schedule, rebuild(adapter), PROFILE)
    other = Activity("https://kdocs.cn/l/OTHER", "另一个", ())
    schedule.begin(other, activity.slots[0], 3, "", True)
    schedule.confirm(other, activity.slots[0])
    assert all(schedule.conflicts().values())
    driver.cells["H3"] = "SOMEONE"
    adapter.sync_schedule(schedule, rebuild(adapter), PROFILE)
    assert not any(schedule.conflicts().values())


@pytest.mark.parametrize("dry_run", [True, False])
def test_link_only_handle_rebuilds_table_before_filtering(tmp_path, dry_run):
    adapter, driver = sheet()
    adapter.open = lambda url: None
    adapter.close = lambda: None
    config = dict(runtime=dict(dry_run=dry_run), tencent_docs=adapter.config, profile=PROFILE)
    schedule = Schedule(tmp_path / "seen.json")
    assert handle_message(extract_message(URL), config, SeenState(str(tmp_path / "seen.json")), adapter,
                          SimpleNamespace(join=lambda url: None), logging.getLogger(__name__), schedule=schedule)
    assert bool(driver.cells.get("H3")) is (not dry_run)
    assert bool(schedule.entries) is (not dry_run)


def test_thread_is_restarted_but_completed_job_is_not(tmp_path):
    monitor = make_monitor(tmp_path, {"thread_restart_seconds": 0})
    calls = []
    def handler(*args, outcome, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("测试线程意外退出")
        outcome.update(status="填写完成", finished=True)
    monitor.handler = handler
    monitor.add(extract_message(URL))
    monitor.start_pending()
    monitor.threads[0].join(3)
    assert len(monitor.jobs) == 1
    monitor.start_pending()
    monitor.threads[0].join(3)
    monitor.start_pending()
    assert len(calls) == 2 and not monitor.jobs


def test_stop_does_not_resurrect_job(tmp_path):
    monitor = make_monitor(tmp_path)
    monitor.add(extract_message(URL))
    monitor.stop_event.set()
    monitor.start_pending()
    assert not monitor.threads


def test_restart_respects_configured_delay(tmp_path, monkeypatch):
    monitor = make_monitor(tmp_path, {"thread_restart_seconds": 10})
    monitor.handler = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("crash"))
    monitor.add(extract_message(URL))
    monkeypatch.setattr("src.monitor.time.monotonic", lambda: 100)
    monitor.start_pending()
    monitor.threads[0].join(3)
    job = monitor.jobs[document_id(URL)]
    previous = job["thread"]
    assert job["restart_at"] == 110
    monitor.start_pending()
    assert job["thread"] is previous
    monitor.stop_event.set()


def test_closed_document_sync_only_reads_and_keeps_original_message(tmp_path):
    monitor = make_monitor(tmp_path)
    monitor.add(extract_message("2026第三周 " + URL))
    monitor.jobs.clear()
    actions = []
    class ReadOnly:
        def __init__(self, config): assert not config["reuse_document_tabs"]
        def open(self, url): actions.append("open")
        def rebuild_activity(self, activity, message, semester):
            assert "第三周" in message.text
            return activity
        def sync_schedule(self, *args): actions.append("sync")
        def close(self): actions.append("close")
        def submit(self, *args): raise AssertionError("sync must never submit")
    monitor.adapter_factory = ReadOnly
    monitor.sync_once()
    assert actions == ["open", "sync", "close"]
    monitor.jobs[document_id(URL)] = {}
    monitor.sync_once()
    assert len(actions) == 3


def test_sync_skips_blocked_or_open_retry_exhausted_document(tmp_path):
    monitor = make_monitor(tmp_path)
    monitor.add(extract_message(URL))
    monitor.jobs.clear()
    monitor.adapter_factory = lambda config: pytest.fail("must not reopen skipped document")
    monitor.records.add("live", URL, "打开失败，已跳过")
    monitor.sync_once()
    monitor.records.add("live", URL, "填写完成")
    monitor.state.add(f"live:manual-block:{document_id(URL)}")
    monitor.sync_once()


def test_read_failure_preserves_previous_schedule_and_closes_own_tab(tmp_path):
    monitor = make_monitor(tmp_path)
    monitor.add(extract_message(URL))
    monitor.jobs.clear()
    slot = TimeSlot(datetime(2099, 9, 20, 9), datetime(2099, 9, 20, 10))
    activity = Activity(URL, "测试", (slot,))
    monitor.schedule.begin(activity, slot, 3, "")
    monitor.schedule.confirm(activity, slot)
    closed = []
    class Broken:
        def __init__(self, config): pass
        def open(self, url): pass
        def rebuild_activity(self, *args): raise RuntimeError("read failed")
        def close(self): closed.append(True)
    monitor.adapter_factory = Broken
    monitor.sync_once()
    assert closed == [True] and monitor.schedule.entries[0]["status"] == "已报名"


def test_cutoff_uses_rebuilt_table_instead_of_qq_hours(tmp_path, monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls): return cls(2099, 9, 19, 10)
    monkeypatch.setattr("run.datetime", Clock)
    adapter, driver = sheet({"A3": "12:00-13:00"})
    adapter.open = lambda url: None
    adapter.close = lambda: None
    config = dict(runtime=dict(dry_run=False, document_deadline_days=1), tencent_docs=adapter.config, profile=PROFILE)
    outcome = {}
    assert handle_message(extract_message("9月20日 09:00-10:00 " + URL), config,
                          SeenState(str(tmp_path / "seen.json")), adapter, SimpleNamespace(join=lambda url: None),
                          logging.getLogger(__name__), schedule=Schedule(tmp_path / "seen.json"), continuous=True, outcome=outcome)
    assert outcome["deadline"] == datetime(2099, 9, 19, 12)
    assert driver.cells["H3"] == PROFILE["student_id"]


class Control:
    def __init__(self, text="", kind="Group", children=(), identifier=""):
        self.text, self.items = text, list(children)
        self.element_info = SimpleNamespace(control_type=kind, automation_id=identifier)
    def children(self): return self.items
    def descendants(self): return [c for child in self.items for c in [child, *child.descendants()]]
    def window_text(self): return self.text


def test_qq_bubbles_do_not_mix_sidebar_and_adjacent_announcements():
    root = Control(identifier="ml-root", children=[
        Control(children=[Control("9月19日 8:00-9:00", "Text"), Control("https://kdocs.cn/l/A", "Hyperlink")]),
        Control(children=[Control("9月20日篮球志愿", "Text"), Control(URL, "Text")]),
    ])
    sidebar = Control("别的群 9月21日 https://kdocs.cn/l/WRONG", "Text")
    blocks = QQWindowReader.message_blocks([sidebar, root, *root.descendants()])
    assert len(blocks) == 2 and "19日" not in blocks[1] and "21日" not in "".join(blocks)
    assert QQWindowReader.message_blocks([sidebar]) == []
