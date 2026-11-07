from datetime import datetime
import logging
from unittest.mock import Mock

import pytest

from run import handle_message
from src.parser import extract_message, parse_activity
from src.state import SeenState


@pytest.mark.parametrize("title", ["2026第三周", "第三周", "第3周", "2026年 第三周"])
def test_semester_week_uses_configured_start(title):
    message = extract_message(f"{title} 上午9:30-10:15 https://www.kdocs.cn/l/test")
    activity = parse_activity(message, semester_start_date="2026-08-31")
    assert [slot.start for slot in activity.slots] == [datetime(2026, 9, day, 9, 30) for day in range(14, 19)]


def test_explicit_weekdays_stay_in_requested_semester_week():
    message = extract_message("第3周 周三 09:00-10:00 星期天 14:00-15:00 https://www.kdocs.cn/l/test")
    activity = parse_activity(message, semester_start_date="2026-08-31")
    assert [slot.start for slot in activity.slots] == [datetime(2026, 9, 16, 9), datetime(2026, 9, 20, 14)]


def test_explicit_month_day_takes_priority_over_week_number():
    message = extract_message("第三周 9月22日 09:00-10:00 https://www.kdocs.cn/l/test")
    activity = parse_activity(message, year=2026, semester_start_date="2026-08-31")
    assert activity.slots[0].start == datetime(2026, 9, 22, 9)


def test_semester_week_can_cross_into_next_year():
    message = extract_message("第19周 周一 09:00-10:00 https://www.kdocs.cn/l/test")
    activity = parse_activity(message, semester_start_date="2026-08-31")
    assert activity.slots[0].start == datetime(2027, 1, 4, 9)


@pytest.mark.parametrize("start", [None, "2026-09-01"])
def test_missing_or_non_monday_start_is_not_interpreted_as_iso_week(start):
    message = extract_message("第三周 09:00-10:00 https://www.kdocs.cn/l/test")
    with pytest.raises(ValueError):
        parse_activity(message, semester_start_date=start)


def test_listener_uses_semester_date_for_conflict_filter(tmp_path):
    message = extract_message("第三周 09:00-10:00 https://www.kdocs.cn/l/test")
    config = {"activity": {"semester_start_date": "2026-08-31", "default_weekdays": [0, 1]},
              "runtime": {"dry_run": True},
              "unavailable": {"text_rules": ["2026-09-14 09:00-10:00"]}}
    docs = Mock()
    assert handle_message(message, config, SeenState(str(tmp_path / "state.json")), docs, Mock(), logging.getLogger(__name__))
    assert [slot.start for slot in docs.submit.call_args.args[1]] == [datetime(2026, 9, 15, 9)]
