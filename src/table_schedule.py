"""Resolve row dates and hours without requiring hours in the QQ message."""
import re
from datetime import datetime, time, timedelta

from .models import TimeSlot
from .parser import WEEK_NUMBER_RE, _chinese_number, parse_semester_start


def dates_in(text, year):
    text = re.sub(r"https?://\S+", "", str(text)).replace("：", ":")
    text = re.sub(r"\d{1,2}:\d{2}", "", text)
    pattern = r"(?<![\d:/.-])(?:(\d{4})\s*[年/.-]\s*)?(\d{1,2})\s*[月/.-]\s*(\d{1,2})(?:日|号)?(?!\d)"
    result = []
    for match in re.finditer(pattern, str(text)):
        y, month, day = match.groups()
        result.append(datetime(int(y or year), int(month), int(day)).date())
    return list(dict.fromkeys(result))


def row_slots(values, message, activity, semester_start, table_context=""):
    text = " ".join(values.get(key, "") for key in ("date", "time", "start", "end")).replace("：", ":")
    year_match = re.search(r"(?<!\d)(20\d{2})(?:年|第|[-/.])", table_context + "\n" + message.text)
    year = int(year_match.group(1)) if year_match else (activity.slots[0].start.year if activity.slots else datetime.now().year)
    days = dates_in(values.get("date", ""), year) or dates_in(values.get("time", ""), year)
    serial = values.get("date", "").strip()
    if not days and re.fullmatch(r"\d{5}(?:\.0+)?", serial):
        days = [(datetime(1899, 12, 30) + timedelta(days=float(serial))).date()]
    weekday = re.search(r"(?:周|星期|礼拜)([一二三四五六日天])", values.get("weekday", "") + values.get("date", ""))
    index = min("一二三四五六日天".index(weekday.group(1)), 6) if weekday else None
    numeric = values.get("weekday", "").strip()
    if index is None and numeric in {str(i) for i in range(1, 8)}:
        index = int(numeric) - 1
    if not days:
        anchors = dates_in(table_context, year) or dates_in(message.text, year)
        if index is not None:
            days = [day for day in anchors if day.weekday() == index]
            week = WEEK_NUMBER_RE.search(table_context) or WEEK_NUMBER_RE.search(message.text)
            if not days and week:
                days = [parse_semester_start(semester_start) + timedelta(weeks=_chinese_number(week.group(2)) - 1, days=index)]
        elif len(anchors) == 1:
            days = anchors
    ranges = re.findall(r"(\d{1,2}):(\d{2})\s*(?:-|~|～|—|–|至|到)\s*(\d{1,2}):(\d{2})", text)
    if not ranges and values.get("start") and values.get("end"):
        def clock(value):
            match = re.search(r"(\d{1,2}):(\d{2})", value.replace("：", ":"))
            if match:
                return tuple(map(int, match.groups()))
            fraction = float(value)
            if not 0 <= fraction < 1:
                raise ValueError("表格时间不是时分或有效时间小数")
            minutes = round(fraction * 1440)
            return minutes // 60, minutes % 60
        ranges = [(*clock(values["start"]), *clock(values["end"]))]
    if not days or not ranges:
        return []
    result = []
    for day in days:
        for sh, sm, eh, em in ranges:
            start = datetime.combine(day, time(int(sh), int(sm)))
            end = datetime.combine(day, time(int(eh), int(em)))
            if end <= start:
                end += timedelta(days=1)
            result.append(TimeSlot(start, end))
    return result
