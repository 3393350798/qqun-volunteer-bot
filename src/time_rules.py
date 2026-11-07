from datetime import datetime, time, timedelta
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from dateutil import parser as date_parser
from icalendar import Calendar
import recurring_ical_events

from .models import TimeSlot


def _clock(value: str) -> time:
    return datetime.strptime(value.strip(), "%H:%M").time()


def _slot(day, start_text: str, end_text: str) -> TimeSlot:
    start = datetime.combine(day, _clock(start_text))
    end = datetime.combine(day, _clock(end_text))
    if end <= start:
        end += timedelta(days=1)
    return TimeSlot(start, end)


def parse_text_rule(rule: str, reference: datetime, until: datetime | None = None) -> list[TimeSlot]:
    """Parse simple Chinese rules such as 周一 18:00-21:00 or 2026-09-12 09:00-12:00."""
    rule = rule.replace("：", ":").replace("～", "~")
    match = re.search(r"(周[一二三四五六日天]|星期[一二三四五六日天])?\s*(\d{1,2}:\d{2})\s*[-~至]\s*(\d{1,2}:\d{2})", rule)
    if not match:
        return []
    day_text, start_text, end_text = match.groups()
    date_match = re.search(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", rule)
    index = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}[day_text[-1]] if day_text else None
    if date_match:
        return [_slot(date_parser.parse(date_match.group()).date(), start_text, end_text)]
    if until is not None:
        # Include the previous day for a rule that extends past midnight.
        first = reference.date() - timedelta(days=1)
        return [_slot(day, start_text, end_text)
                for offset in range((until.date() - first).days + 1)
                for day in [first + timedelta(days=offset)]
                if index is None or day.weekday() == index]
    day = reference.date()
    if index is not None:
        delta = (index - reference.weekday()) % 7
        day += timedelta(days=delta)
    return [_slot(day, start_text, end_text)]


def from_weekly(items: list[dict], start: datetime, days: int = 14) -> list[TimeSlot]:
    result = []
    for offset in range(days):
        day = start.date() + timedelta(days=offset)
        for item in items or []:
            if int(item["weekday"]) == day.weekday():
                result.append(_slot(day, item["start"], item["end"]))
    return result


def from_ics(paths: list[str], start: datetime, end: datetime, local_timezone: str = "Asia/Shanghai") -> list[TimeSlot]:
    return sorted(set(TimeSlot(entry["start"], entry["end"]) for entry in ics_entries(paths, start, end, local_timezone)),
                  key=lambda slot: (slot.start, slot.end))


def ics_entries(paths: list[str], start: datetime, end: datetime, local_timezone: str = "Asia/Shanghai") -> list[dict]:
    if not paths:
        return []
    zone = ZoneInfo(local_timezone)
    query_start, query_end = start.replace(tzinfo=zone), end.replace(tzinfo=zone)
    result = []
    for filename in paths or []:
        calendar = Calendar.from_ical(Path(filename).read_text(encoding="utf-8-sig"))
        for event in recurring_ical_events.of(calendar).between(query_start, query_end):
            if str(event.get("TRANSP", "")).upper() == "TRANSPARENT" or str(event.get("STATUS", "")).upper() == "CANCELLED":
                continue
            slot = TimeSlot(_local_datetime(event.decoded("DTSTART"), zone),
                            _local_datetime(event.decoded("DTEND"), zone))
            if slot.start < slot.end and slot.end > start and slot.start < end:
                result.append(dict(start=slot.start, end=slot.end, title=str(event.get("SUMMARY", "日历事项")),
                                   url=str(event.get("URL", "")), status="ICS 忙碌", location=str(event.get("LOCATION", ""))))
    return sorted(result, key=lambda entry: (entry["start"], entry["end"]))


def _local_datetime(value, zone: ZoneInfo) -> datetime:
    if not isinstance(value, datetime):
        return datetime.combine(value, time.min)
    if value.tzinfo is not None:
        return value.astimezone(zone).replace(tzinfo=None)
    return value


def unavailable_slots(config: dict, start: datetime, end: datetime) -> list[TimeSlot]:
    items = from_weekly(config.get("unavailable", {}).get("weekly", []), start - timedelta(days=1), (end.date() - start.date()).days + 2)
    for rule in config.get("unavailable", {}).get("text_rules", []) or []:
        items.extend(parse_text_rule(rule, start, end))
    timezone_name = config.get("runtime", {}).get("timezone", "Asia/Shanghai")
    items.extend(from_ics(config.get("unavailable", {}).get("ics_files", []), start, end, timezone_name))
    return [slot for slot in items if slot.end > start and slot.start < end]


def available_slots(slots: list[TimeSlot], unavailable: list[TimeSlot]) -> list[TimeSlot]:
    return [slot for slot in slots if not any(slot.overlaps(blocked) for blocked in unavailable)]
