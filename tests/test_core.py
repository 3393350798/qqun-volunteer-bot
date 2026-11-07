from datetime import datetime
from pathlib import Path

from src.models import Activity, Message, TimeSlot
from src.parser import classify_link, extract_group_links, extract_message
from src.qr import decode_qr
from src.state import SeenState
from src.time_rules import available_slots, from_ics, parse_text_rule
from src.docs_adapter import TencentDocsAdapter, TencentDocsError
from src.table_mapping import header_indices
from src.group_join import GroupJoiner
from src.qq_adapter import QQWindowReader
from run import handle_message


class FakeCell:
    def __init__(self):
        self.value = ""
        self.clicked = False
    def click(self):
        self.clicked = True
    def clear(self):
        self.value = ""
    def send_keys(self, *values):
        printable = [value for value in values if isinstance(value, str) and value not in {"\ue009", "\ue003", "\ue007"}]
        if printable:
            self.value = printable[-1]
    def get_attribute(self, name):
        return self.value if name == "value" else None


class FakeRow:
    def __init__(self):
        self.cells = {name: FakeCell() for name in (".date", ".start", ".end", ".name", ".student", ".phone")}
    def find_elements(self, by, selector):
        return [self.cells[selector]] if selector in self.cells else []


class FakeDriver:
    def __init__(self):
        self.rows = [FakeRow(), FakeRow()]
        self.button = FakeCell()
    def find_elements(self, by, selector):
        if selector == ".header":
            return [type("Header", (), {"text": value})() for value in ("手机号", "姓名", "学号")]
        return self.rows if selector == ".row" else []
    def find_element(self, by, selector):
        if selector == ".submit":
            return self.button
        if selector in {".name", ".editor"}:
            return FakeCell()
        raise AssertionError(selector)


class FakeCanvasDriver:
    def __init__(self, cells):
        self.cells = dict(cells)
        self.selected = ""
        self.name_box = FakeCell()
        self.editor = FakeCell()

        def select(*values):
            printable = [value for value in values if isinstance(value, str) and not (len(value) == 1 and ord(value) >= 0xE000)]
            targets = [value for value in printable if value.upper() == value and any(char.isdigit() for char in value)]
            if targets:
                self.selected = targets[-1]
                self.name_box.value = self.selected

        def edit(*values):
            printable = [value for value in values if isinstance(value, str) and not (len(value) == 1 and ord(value) >= 0xE000)]
            if printable and self.selected:
                self.cells[self.selected] = printable[-1]

        self.name_box.send_keys = select
        self.editor.send_keys = edit
        self.editor.get_attribute = lambda name: self.cells.get(self.selected, "") if name == "value" else None

    def find_element(self, by, selector):
        if selector == ".name":
            return self.name_box
        if selector == ".editor":
            return self.editor
        if selector == "body":
            return type("Body", (), {"text": ""})()
        raise AssertionError(selector)

    def find_elements(self, by, selector):
        return []


def _dynamic_adapter(driver):
    adapter = TencentDocsAdapter({
        "submit_mode": "autosave",
        "selectors": {
            "name_box": ".name",
            "editor": ".editor",
            "header_row": 2,
            "data_start_row": 3,
            "scan_max_rows": 5,
            "header_scan_columns": ["A", "B", "C", "F", "G", "H", "I"],
        },
    })
    adapter.driver = driver
    return adapter


def test_extract_and_deduplicate_id():
    message = extract_message("志愿活动 https://docs.qq.com/sheet/abc")
    assert message and message.message_id
    assert extract_message("没有链接") is None
    assert classify_link("https://www.kdocs.cn/l/abc") == "wps_docs"
    assert classify_link("https://qm.qq.com/q/abc") == "qq_group"
    assert extract_group_links("加群 https://qm.qq.com/q/abc https://weixin.qq.com/x") == ["https://qm.qq.com/q/abc", "https://weixin.qq.com/x"]


def test_group_message_is_not_parsed_as_activity():
    message = extract_message("二维码群邀请：https://qm.qq.com/q/abc")
    try:
        __import__("src.parser", fromlist=["parse_activity"]).parse_activity(message)
    except ValueError:
        pass
    else:
        raise AssertionError("group-only message should not become an activity")


def test_qr_missing_file_is_explicit():
    try:
        decode_qr("data/no-such-qr.png")
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("missing QR file should be explicit")


def test_group_joiner_is_disabled_by_default():
    assert not GroupJoiner().join("https://qm.qq.com/q/abc")


def test_qq_reader_filters_configured_group_numbers():
    class Control:
        def __init__(self, text):
            self.text = text
        def window_text(self):
            return self.text
    reader = QQWindowReader(groups=["123456789"], scan_all_groups=False)
    allowed, group_id = reader._group_context(Control("志愿通知群 (123456789)"), [Control("志愿通知群 (123456789)")])
    assert allowed and group_id == "123456789"
    blocked, _ = reader._group_context(Control("其他群 (987654321)"), [Control("其他群 (987654321)")])
    assert not blocked


def test_qq_reader_filters_active_group_name_only():
    class Control:
        def __init__(self, text, class_name=""):
            self.text = text
            self.element_info = type("Info", (), {"class_name": class_name})()
        def window_text(self):
            return self.text
    reader = QQWindowReader(group_names=["示例志愿者群"], scan_all_groups=False)
    allowed, group = reader._group_context(Control("QQ"), [Control("示例志愿者群", "chat-header__contact-name"), Control("其他群")])
    assert allowed and group == "示例志愿者群"
    blocked, _ = reader._group_context(Control("QQ"), [Control("其他群", "chat-header__contact-name"), Control("示例志愿者群")])
    assert not blocked


def test_group_invitation_is_processed_even_with_document():
    class Joiner:
        def __init__(self):
            self.urls = []
        def join(self, url):
            self.urls.append(url)
            return True
    class State:
        def __init__(self):
            self.ids = set()
        def contains(self, value):
            return value in self.ids
        def add(self, value):
            self.ids.add(value)
    class Docs:
        def open(self, *args):
            pass
        def submit(self, *args):
            return None
        def close(self):
            return None
    message = extract_message("活动 周一 9:00-10:00 https://www.kdocs.cn/l/test 加群 https://qm.qq.com/q/abc")
    joiner, state = Joiner(), State()
    config = {"activity": {"default_weekdays": [0]}, "runtime": {"dry_run": True}, "profile": {}}
    assert handle_message(message, config, state, Docs(), joiner, __import__("logging").getLogger("test"))
    assert joiner.urls == ["https://qm.qq.com/q/abc"]


def test_duplicate_message_does_not_submit_twice():
    class State:
        def __init__(self):
            self.ids = set()
        def contains(self, value):
            return value in self.ids
        def add(self, value):
            self.ids.add(value)
    class Docs:
        def __init__(self):
            self.calls = 0
        def submit(self, *args):
            self.calls += 1
        def close(self):
            pass
    message = extract_message("周一 9:00-10:00 https://www.kdocs.cn/l/test")
    state, docs = State(), Docs()
    config = {"activity": {"default_weekdays": [0]}, "runtime": {"dry_run": True}, "profile": {}}
    log = __import__("logging").getLogger("test")
    assert handle_message(message, config, state, docs, GroupJoiner(), log)
    assert not handle_message(message, config, state, docs, GroupJoiner(), log)
    assert docs.calls == 1


def test_repeated_no_slot_failures_eventually_suppress_document():
    class Docs:
        def open(self, *args):
            pass
        def submit(self, *args):
            raise TencentDocsError("找不到匹配且为空的报名行：2026-09-14")
        def close(self):
            pass
    class State:
        def __init__(self): self.ids = set()
        def contains(self, value): return value in self.ids
        def add(self, value): self.ids.add(value)
        def count_prefix(self, prefix): return sum(value.startswith(prefix) for value in self.ids)
    message = extract_message("周一 9:00-10:00 https://www.kdocs.cn/l/test")
    state, docs = State(), Docs()
    config = {"activity": {"default_weekdays": [0]}, "runtime": {"dry_run": False, "no_slot_retry_limit": 2}, "profile": {"name": "测试用户", "college": "学院", "student_id": "A1", "phone": "13800000000"}}
    log = __import__("logging").getLogger("test")
    assert not handle_message(message, config, state, docs, GroupJoiner(), log)
    assert not handle_message(extract_message("补充 " + message.text), config, state, docs, GroupJoiner(), log)
    assert state.count_prefix("live:document:") == 1


def test_link_context_preserves_activity_description():
    text = "【时间】：周一 9:30-10:15\n【地点】：测试点位甲\n报名链接：https://www.kdocs.cn/l/test"
    message = extract_message(text)
    activity = __import__("src.parser", fromlist=["parse_activity"]).parse_activity(message, 2026)
    assert activity.slots and activity.locations == ("测试点位甲",)


def test_weekday_only_activity_times_are_parsed():
    message = extract_message("【2026第三周校园交通志愿者】周三上午9:30-10:15，中午11:30-12:15 https://www.kdocs.cn/l/test")
    activity = __import__("src.parser", fromlist=["parse_activity"]).parse_activity(message, 2026, semester_start_date="2026-08-31")
    assert len(activity.slots) == 2


def test_activity_extracts_locations():
    message = extract_message("\u6d3b\u52a8\n\u3010\u5730\u70b9\u3011\uff1a\u6d4b\u8bd5\u70b9\u4f4d\u7532\u3001\u6d4b\u8bd5\u70b9\u4f4d\u4e59\n\u5468\u4e00 9:00-10:00 https://www.kdocs.cn/l/test")
    activity = __import__("src.parser", fromlist=["parse_activity"]).parse_activity(message, 2026)
    assert activity.locations == ("\u6d4b\u8bd5\u70b9\u4f4d\u7532", "\u6d4b\u8bd5\u70b9\u4f4d\u4e59")


def test_week_number_without_weekday_does_not_guess_date():
    message = extract_message("2026第三周交通志愿者 上午9:30-10:15 https://www.kdocs.cn/l/test")
    activity = __import__("src.parser", fromlist=["parse_activity"]).parse_activity(message, 2026, default_weekdays=())
    assert activity.slots == ()


def test_week_number_generates_configured_weekdays():
    message = extract_message("2026第三周交通志愿者 上午9:30-10:15 https://www.kdocs.cn/l/test")
    activity = __import__("src.parser", fromlist=["parse_activity"]).parse_activity(message, 2026, semester_start_date="2026-08-31")
    assert len(activity.slots) == 5
    assert {slot.start.weekday() for slot in activity.slots} == {0, 1, 2, 3, 4}


def test_each_time_uses_nearest_preceding_weekday():
    message = extract_message("周一 9:00-10:00，周二 14:00-15:00 https://www.kdocs.cn/l/test")
    activity = __import__("src.parser", fromlist=["parse_activity"]).parse_activity(message, 2026)
    assert [slot.start.weekday() for slot in activity.slots] == [0, 1]


def test_fullwidth_colon_is_parsed():
    message = extract_message("周三 上午9：30-10：15 https://www.kdocs.cn/l/test")
    activity = __import__("src.parser", fromlist=["parse_activity"]).parse_activity(message, 2026)
    assert activity.slots[0].start.hour == 9 and activity.slots[0].end.minute == 15


def test_conflicting_slot_is_removed():
    day = datetime(2026, 9, 14)
    slots = [TimeSlot(day.replace(hour=18), day.replace(hour=20)), TimeSlot(day.replace(hour=9), day.replace(hour=11))]
    blocked = [TimeSlot(day.replace(hour=19), day.replace(hour=21))]
    assert available_slots(slots, blocked) == [slots[1]]


def test_text_rule():
    slots = parse_text_rule("周一 18:00-21:00", datetime(2026, 9, 14, 9))
    assert slots[0].start.hour == 18 and slots[0].end.hour == 21


def test_text_rule_supports_sunday_and_cross_midnight():
    slots = parse_text_rule("星期天 23:00-01:00", datetime(2026, 9, 14, 9))
    assert slots[0].start.weekday() == 6
    assert slots[0].end.day == slots[0].start.day + 1


def test_all_non_conflicting_slots_are_kept():
    day = datetime(2026, 9, 14)
    slots = [TimeSlot(day.replace(hour=9), day.replace(hour=10)), TimeSlot(day.replace(hour=14), day.replace(hour=15))]
    assert available_slots(slots, []) == slots


def test_ics_busy_time_is_loaded():
    calendar = """BEGIN:VCALENDAR\nBEGIN:VEVENT\nDTSTART:20260914T180000\nDTEND:20260914T200000\nEND:VEVENT\nEND:VCALENDAR\n"""
    path = Path("data/test_busy.ics")
    path.parent.mkdir(exist_ok=True)
    path.write_text(calendar, encoding="utf-8")
    start = datetime(2026, 9, 14, 9)
    end = datetime(2026, 9, 15)
    slots = from_ics([str(path)], start, end)
    assert slots[0].start.hour == 18 and slots[0].end.hour == 20
    path.unlink()


def test_ics_utc_time_is_converted_to_configured_local_timezone():
    calendar = """BEGIN:VCALENDAR\nBEGIN:VEVENT\nDTSTART:20260914T100000Z\nDTEND:20260914T110000Z\nEND:VEVENT\nEND:VCALENDAR\n"""
    path = Path("data/test_utc_busy.ics")
    path.parent.mkdir(exist_ok=True)
    path.write_text(calendar, encoding="utf-8")
    start = datetime(2026, 9, 14, 0)
    end = datetime(2026, 9, 15)
    slots = from_ics([str(path)], start, end, "Asia/Shanghai")
    assert slots[0].start.hour == 18 and slots[0].end.hour == 19
    path.unlink()


def test_ics_tzid_time_is_preserved_in_local_timezone():
    calendar = """BEGIN:VCALENDAR\nBEGIN:VEVENT\nDTSTART;TZID=Asia/Shanghai:20260914T100000\nDTEND;TZID=Asia/Shanghai:20260914T110000\nEND:VEVENT\nEND:VCALENDAR\n"""
    path = Path("data/test_tzid_busy.ics")
    path.parent.mkdir(exist_ok=True)
    path.write_text(calendar, encoding="utf-8")
    slots = from_ics([str(path)], datetime(2026, 9, 14), datetime(2026, 9, 15), "Asia/Shanghai")
    assert slots[0].start.hour == 10 and slots[0].end.hour == 11
    path.unlink()


def test_seen_state_persists():
    path = Path("data/test_seen.json")
    state = SeenState(str(path))
    state.add("abc")
    assert SeenState(str(path)).contains("abc")
    path.unlink()


def test_docs_dry_run_requires_no_browser():
    activity = Activity("https://docs.qq.com/sheet/test", "test", ())
    adapter = TencentDocsAdapter({})
    adapter.submit(activity, [TimeSlot(datetime(2026, 9, 14, 9), datetime(2026, 9, 14, 10))], {}, True)


def test_docs_dry_run_does_not_log_personal_values(caplog):
    activity = Activity("https://docs.qq.com/sheet/test", "test", ())
    adapter = TencentDocsAdapter({})
    adapter.submit(activity, [TimeSlot(datetime(2026, 9, 14, 9), datetime(2026, 9, 14, 10))], {"student_id": "secret-id", "phone": "secret-phone"}, True)
    assert "secret-id" not in caplog.text and "secret-phone" not in caplog.text


def test_docs_rejects_empty_slots():
    adapter = TencentDocsAdapter({})
    try:
        adapter.submit(None, [], {}, True)
    except TencentDocsError as exc:
        assert "没有" in str(exc)
    else:
        raise AssertionError("empty slots should be rejected")


def test_canvas_dynamic_rows_match_headers_skip_used_rows_and_fill_first_empty():
    cells = {
        "A2": "时间", "B2": "地点", "C2": "星期", "F2": "姓名", "G2": "学院", "H2": "学号", "I2": "联系方式",
        "A3": "09:00-10:00", "B3": "测试点位甲", "C3": "周一", "F3": "已报名", "G3": "学院", "H3": "OLD", "I3": "13800000000",
        "A4": "09:00-10:00", "B4": "测试点位乙", "C4": "1",
        "A5": "11:00-12:00", "B5": "测试点位甲", "C5": "星期一",
        "A6": "14:00-15:00", "B6": "其他地点", "C6": "周一",
    }
    driver = FakeCanvasDriver(cells)
    adapter = _dynamic_adapter(driver)
    activity = Activity(
        "https://www.kdocs.cn/l/test", "test",
        (TimeSlot(datetime(2026, 9, 14, 9), datetime(2026, 9, 14, 10)),
         TimeSlot(datetime(2026, 9, 14, 11), datetime(2026, 9, 14, 12))),
        ("测试点位甲", "测试点位乙"),
    )
    adapter.submit(activity, list(activity.slots), {"name": "测试用户", "college": "学院", "student_id": "A1", "phone": "13800000000"}, False)
    for row in (4, 5):
        assert driver.cells[f"F{row}"] == "测试用户"
        assert driver.cells[f"G{row}"] == "学院"
        assert driver.cells[f"H{row}"] == "A1"
        assert driver.cells[f"I{row}"] == "13800000000"
    assert driver.cells["F3"] == "已报名"
    assert driver.cells.get("F6", "") == ""


def test_canvas_dynamic_rows_keeps_available_slots_when_one_is_full(caplog, tmp_path):
    cells = {
        "A2": "时间", "B2": "地点", "C2": "星期", "F2": "姓名", "G2": "学院", "H2": "学号", "I2": "联系方式",
        "A3": "17:30-18:15", "B3": "测试点位甲", "C3": "周二", "F3": "已报名", "G3": "学院", "H3": "OLD", "I3": "13800000000",
        "A4": "09:30-10:15", "B4": "测试点位甲", "C4": "周三",
        "A5": "11:30-12:15", "B5": "测试点位甲", "C5": "周四",
    }
    driver = FakeCanvasDriver(cells)
    adapter = _dynamic_adapter(driver)
    slots = [TimeSlot(datetime(2026, 9, 15, 17, 30), datetime(2026, 9, 15, 18, 15)),
             TimeSlot(datetime(2026, 9, 16, 9, 30), datetime(2026, 9, 16, 10, 15)),
             TimeSlot(datetime(2026, 9, 17, 11, 30), datetime(2026, 9, 17, 12, 15))]
    activity = Activity("https://www.kdocs.cn/l/test", "test", tuple(slots), ("测试点位甲",))
    from src.schedule import Schedule
    schedule = Schedule(tmp_path / "seen.json")
    adapter.on_slot_start = lambda slot, row, location: schedule.begin(activity, slot, row, location)
    adapter.on_slot_done = lambda slot: schedule.confirm(activity, slot)
    filled = adapter.submit(activity, slots, {"name": "测试用户", "college": "学院", "student_id": "A1", "phone": "13800000000"}, False)
    assert filled == 2
    assert {entry["start"] for entry in schedule.entries} == {s.start.isoformat() for s in slots[1:]}
    assert all(entry["status"] == "已报名" for entry in schedule.entries)
    assert driver.cells["F3"] == "已报名"
    assert driver.cells["F4"] == driver.cells["F5"] == "测试用户"
    assert "2026-09-15 17:30-18:15" in caplog.text


def test_canvas_dynamic_rows_stops_without_matching_empty_row():
    cells = {
        "A2": "时间", "B2": "地点", "C2": "星期", "F2": "姓名", "G2": "学院", "H2": "学号", "I2": "联系方式",
        "A3": "09:00-10:00", "B3": "其他地点", "C3": "周一",
    }
    driver = FakeCanvasDriver(cells)
    adapter = _dynamic_adapter(driver)
    activity = Activity(
        "https://www.kdocs.cn/l/test", "test",
        (TimeSlot(datetime(2026, 9, 14, 9), datetime(2026, 9, 14, 10)),),
        ("测试点位甲",),
    )
    try:
        adapter.submit(activity, list(activity.slots), {"name": "测试用户", "college": "学院", "student_id": "A1", "phone": "13800000000"}, False)
    except TencentDocsError as exc:
        assert "匹配且为空" in str(exc)
    else:
        raise AssertionError("dynamic rows must stop without a matching empty row")
    assert all(not key.startswith(("F", "G", "H", "I")) or not value for key, value in driver.cells.items() if key[1:].isdigit() and int(key[1:]) >= 3)


def test_open_without_ready_selector_keeps_browser_setup(monkeypatch):
    class Driver:
        def get(self, url):
            self.url = url
    def edge(options=None):
        return Driver()
    import selenium.webdriver
    monkeypatch.setattr(selenium.webdriver, "Edge", edge)
    adapter = TencentDocsAdapter({"ready_selector": ""})
    adapter.open("https://www.kdocs.cn/l/test")
    assert adapter.driver.url.endswith("/test")


def test_open_can_attach_to_existing_edge_debugger(monkeypatch):
    class Options:
        def __init__(self):
            self.experimental = {}
            self.arguments = []
        def add_experimental_option(self, name, value):
            self.experimental[name] = value
        def add_argument(self, value):
            self.arguments.append(value)

    class Driver:
        switch_to = type("Switch", (), {"new_window": lambda self, kind: None})()
        def get(self, url):
            self.url = url

    seen = {}
    def edge(options=None):
        seen["options"] = options
        return Driver()

    import selenium.webdriver
    import selenium.webdriver.edge.options
    monkeypatch.setattr(selenium.webdriver.edge.options, "Options", Options)
    monkeypatch.setattr(selenium.webdriver, "Edge", edge)
    adapter = TencentDocsAdapter({"debugger_address": "127.0.0.1:9222", "user_data_dir": "ignored"})
    adapter.open("https://www.kdocs.cn/l/test")
    assert seen["options"].experimental["debuggerAddress"] == "127.0.0.1:9222"
    assert seen["options"].arguments == []


def test_open_edge_failure_is_explicit(monkeypatch):
    import selenium.webdriver
    monkeypatch.setattr(selenium.webdriver, "Edge", lambda options=None: (_ for _ in ()).throw(RuntimeError("测试：Edge 不可用")))
    adapter = TencentDocsAdapter({})
    try:
        adapter.open("https://www.kdocs.cn/l/test")
    except TencentDocsError as exc:
        assert "Edge" in str(exc)
    else:
        raise AssertionError("Edge startup failure must be explicit")


def test_attached_edge_uses_and_closes_only_a_new_tab(monkeypatch):
    class Options:
        def __init__(self):
            self.experimental = {}
        def add_experimental_option(self, name, value):
            self.experimental[name] = value

    class Switch:
        def __init__(self, driver):
            self.driver = driver
        def new_window(self, kind):
            self.driver.new_window_kind = kind

    class Driver:
        def __init__(self):
            self.switch_to = Switch(self)
            self.closed = False
            self.quit_called = False
        def get(self, url):
            self.url = url
        def close(self):
            self.closed = True
        def quit(self):
            self.quit_called = True

    holder = {}
    def edge(options=None):
        holder["driver"] = Driver()
        return holder["driver"]

    import selenium.webdriver
    import selenium.webdriver.edge.options
    monkeypatch.setattr(selenium.webdriver.edge.options, "Options", Options)
    monkeypatch.setattr(selenium.webdriver, "Edge", edge)
    adapter = TencentDocsAdapter({"debugger_address": "127.0.0.1:9222"})
    adapter.open("https://www.kdocs.cn/l/test")
    assert holder["driver"].new_window_kind == "tab"
    adapter.close()
    assert holder["driver"].closed and not holder["driver"].quit_called


def test_open_ready_selector_failure_is_explicit(monkeypatch):
    class Driver:
        def get(self, url):
            self.url = url
        def find_elements(self, by, selector):
            return []

    def edge(options=None):
        return Driver()

    class Wait:
        def __init__(self, driver, timeout):
            pass
        def until(self, predicate):
            raise RuntimeError("测试：登录页面未就绪")

    import selenium.webdriver
    import selenium.webdriver.support.ui
    monkeypatch.setattr(selenium.webdriver, "Edge", edge)
    monkeypatch.setattr(selenium.webdriver.support.ui, "WebDriverWait", Wait)
    adapter = TencentDocsAdapter({"ready_selector": ".sheet-ready"})
    try:
        adapter.open("https://www.kdocs.cn/l/test")
    except TencentDocsError as exc:
        assert "未就绪" in str(exc)
    else:
        raise AssertionError("ready selector failure must stop the task")


def test_real_submit_requires_complete_profile():
    adapter = TencentDocsAdapter({"selectors": {"row": ".row", "submit": ".submit"}})
    adapter.driver = FakeDriver()
    activity = Activity("https://www.kdocs.cn/l/test", "test", ())
    try:
        adapter.submit(activity, [TimeSlot(datetime(2026, 9, 14, 9), datetime(2026, 9, 14, 10))], {"name": "测试用户"}, False)
    except TencentDocsError as exc:
        assert "个人资料" in str(exc)
    else:
        raise AssertionError("real submit must require complete profile")


def test_header_order_is_irrelevant():
    assert header_indices(["手机号", "姓名", "学号"], ["姓名", "学号", "手机号"]) == {"姓名": 1, "学号": 2, "手机号": 0}


def test_docs_fills_multiple_rows_and_submits():
    activity = Activity("https://docs.qq.com/sheet/test", "test", ())
    adapter = TencentDocsAdapter({
        "required_headers": ["姓名", "学号", "手机号"],
        "selectors": {"header": ".header", "row": ".row", "date": ".date", "start": ".start", "end": ".end", "name": ".name", "student_id": ".student", "phone": ".phone", "submit": ".submit"},
    })
    adapter.driver = FakeDriver()
    slots = [TimeSlot(datetime(2026, 9, 14, 9), datetime(2026, 9, 14, 10)), TimeSlot(datetime(2026, 9, 15, 14), datetime(2026, 9, 15, 15))]
    adapter.submit(activity, slots, {"name": "测试用户", "college": "学院", "student_id": "A1", "phone": "13800000000"}, False)
    assert adapter.driver.rows[0].cells[".name"].value == "测试用户"
    assert adapter.driver.rows[1].cells[".start"].value == "14:00"
    assert adapter.driver.button.clicked
