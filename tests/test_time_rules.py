from datetime import datetime
import logging
from unittest.mock import Mock

import pytest

from src.models import TimeSlot
from src.time_rules import available_slots, from_ics, unavailable_slots
from src.parser import extract_message
from src.state import SeenState
from run import handle_message


def calendar_file(tmp_path, *events):
    path = tmp_path / "busy.ics"
    body = "\n".join(f"BEGIN:VEVENT\n{event}\nEND:VEVENT" for event in events)
    path.write_text(f"BEGIN:VCALENDAR\nVERSION:2.0\n{body}\nEND:VCALENDAR\n", encoding="utf-8")
    return str(path)


@pytest.mark.parametrize("rules", [
    {"weekly": [{"weekday": 0, "start": "23:00", "end": "01:00"}]},
    {"text_rules": ["每周一 23：00-01：00"]},
])
def test_previous_day_overnight_rule_blocks_early_morning_only(rules):
    slots = [TimeSlot(datetime(2026, 9, 15, 0, 30), datetime(2026, 9, 15, 1)),
             TimeSlot(datetime(2026, 9, 15, 1), datetime(2026, 9, 15, 2))]
    busy = unavailable_slots({"unavailable": rules}, slots[0].start, slots[-1].end)
    assert available_slots(slots, busy) == [slots[1]]


def test_weekday_text_rule_repeats_through_the_activity_window():
    busy = unavailable_slots({"unavailable": {"text_rules": ["周一 09:00-10:00"]}},
                             datetime(2026, 9, 14), datetime(2026, 10, 1))
    assert [slot.start for slot in busy] == [datetime(2026, 9, day, 9) for day in (14, 21, 28)]


def test_dated_text_rule_does_not_repeat():
    busy = unavailable_slots({"unavailable": {"text_rules": ["2026-09-14 09:00-10:00"]}},
                             datetime(2026, 9, 14), datetime(2026, 10, 1))
    assert busy == [TimeSlot(datetime(2026, 9, 14, 9), datetime(2026, 9, 14, 10))]


def test_calendar_recurs_with_duration_folded_lines_and_exception_dates(tmp_path):
    path = calendar_file(tmp_path, "UID:class\nDTSTART;TZID=Asia/Shanghai:20260914T090000\n"
                         "DURATION:PT90M\nRRULE:FREQ=WEEKLY;\n COUNT=3\n"
                         "EXDATE;TZID=Asia/Shanghai:20260921T090000\n"
                         "RDATE;TZID=Asia/Shanghai:20260923T090000")
    busy = from_ics([path], datetime(2026, 9, 14), datetime(2026, 10, 1))
    assert busy == [TimeSlot(datetime(2026, 9, day, 9), datetime(2026, 9, day, 10, 30))
                    for day in (14, 23, 28)]


def test_calendar_recurrence_override_replaces_original_time(tmp_path):
    path = calendar_file(tmp_path,
                         "UID:class\nDTSTART:20260914T090000\nDTEND:20260914T100000\nRRULE:FREQ=WEEKLY;COUNT=2",
                         "UID:class\nRECURRENCE-ID:20260921T090000\nDTSTART:20260921T140000\nDTEND:20260921T150000")
    busy = from_ics([path], datetime(2026, 9, 21), datetime(2026, 9, 22))
    assert busy == [TimeSlot(datetime(2026, 9, 21, 14), datetime(2026, 9, 21, 15))]


def test_calendar_all_day_without_end_blocks_one_day(tmp_path):
    path = calendar_file(tmp_path, "UID:away\nDTSTART;VALUE=DATE:20260914")
    busy = from_ics([path], datetime(2026, 9, 14), datetime(2026, 9, 16))
    assert busy == [TimeSlot(datetime(2026, 9, 14), datetime(2026, 9, 15))]


def test_calendar_free_cancelled_and_zero_duration_events_do_not_block(tmp_path):
    path = calendar_file(tmp_path,
                         "UID:free\nDTSTART:20260914T090000\nDTEND:20260914T100000\nTRANSP:TRANSPARENT",
                         "UID:cancelled\nDTSTART:20260914T100000\nDTEND:20260914T110000\nSTATUS:CANCELLED",
                         "UID:instant\nDTSTART:20260914T110000")
    assert from_ics([path], datetime(2026, 9, 14), datetime(2026, 9, 15)) == []


def test_calendar_utc_event_overlapping_query_from_previous_day(tmp_path):
    path = calendar_file(tmp_path, "UID:overnight\nDTSTART:20260914T150000Z\nDTEND:20260914T170000Z")
    busy = from_ics([path], datetime(2026, 9, 15, 0, 30), datetime(2026, 9, 15, 2))
    assert busy == [TimeSlot(datetime(2026, 9, 14, 23), datetime(2026, 9, 15, 1))]


def test_calendar_recurrence_keeps_source_zone_across_dst_change(tmp_path):
    path = calendar_file(tmp_path, "UID:class\nDTSTART;TZID=America/New_York:20261025T090000\n"
                         "DTEND;TZID=America/New_York:20261025T100000\nRRULE:FREQ=WEEKLY;COUNT=2")
    busy = from_ics([path], datetime(2026, 10, 25), datetime(2026, 11, 2))
    assert busy == [TimeSlot(datetime(2026, 10, 25, 21), datetime(2026, 10, 25, 22)),
                    TimeSlot(datetime(2026, 11, 1, 22), datetime(2026, 11, 1, 23))]


def test_missing_calendar_logs_failure_without_submitting(tmp_path, caplog):
    config = {"runtime": {"dry_run": False}, "unavailable": {"ics_files": [str(tmp_path / "missing.ics")]}}
    docs, joiner = Mock(), Mock()
    message = extract_message("周一 09:00-10:00 https://www.kdocs.cn/l/test")
    state = SeenState(str(tmp_path / "seen.json"))
    assert not handle_message(message, config, state, docs, joiner, logging.getLogger(__name__))
    docs.open.assert_not_called()
    docs.submit.assert_not_called()
    assert not state.values
    assert "读取不可用时间失败" in caplog.text
