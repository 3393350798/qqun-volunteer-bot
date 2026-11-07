import hashlib
import re
from datetime import datetime, time, timedelta
from urllib.parse import urlsplit

from dateutil import parser as date_parser

from .models import Activity, Message, TimeSlot

DOC_RE = re.compile(r"https?://(?:docs\.qq\.com|(?:www\.)?kdocs\.cn)/[\w./?=&%-]+", re.I)
GROUP_URL_RE = re.compile(r"https?://(?:qm\.qq\.com|qun\.qq\.com|weixin\.qq\.com|weixin\.com)/[\w./?=&%-]+", re.I)
RANGE_RE = re.compile(r"(\d{1,2})月(\d{1,2})日[^\d]*(\d{1,2}):(\d{2})\s*[-~至]\s*(\d{1,2}):(\d{2})")
TIME_ONLY_RE = re.compile(r"(\d{1,2}):(\d{2})\s*[-~至]\s*(\d{1,2}):(\d{2})")
WEEKDAY_RE = re.compile(r"(?:周|星期)([一二三四五六日天])")
WEEK_NUMBER_RE = re.compile(r"(?:(\d{4})\s*年?\s*)?第\s*(\d+|[一二三四五六七八九十]+)\s*周")


def _chinese_number(value: str) -> int:
    if value.isdigit():
        return int(value)
    digits = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
    if value in digits:
        return digits[value]
    if value.startswith("十"):
        return 10 + (digits.get(value[1:], 0) if len(value) > 1 else 0)
    if value.endswith("十"):
        return digits[value[0]] * 10
    left, right = value.split("十", 1)
    return digits[left] * 10 + digits.get(right, 0)


def message_id(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:24]


def document_id(url: str) -> str:
    """Identify a shared document independently of referral parameters."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host == "www.kdocs.cn":
        host = "kdocs.cn"
    path = parts.path.rstrip("/")
    # These providers put the document ID in the path; queries select views
    # or carry referral information, and must not create another signup.
    if not ((host == "kdocs.cn" and path.startswith("/l/")) or
            (host == "docs.qq.com" and path.startswith("/sheet/"))):
        raise ValueError("不支持的报名表链接格式")
    return message_id(host + path)


def extract_message(text: str) -> Message | None:
    if not (DOC_RE.search(text) or GROUP_URL_RE.search(text)):
        return None
    return Message(message_id(text), text)


def activity_title(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("@")]
    headline = next((line for line in lines if line.startswith("【") and not re.search(r"[：:]", line)), None)
    return (headline or (lines[0] if lines else "志愿活动"))[:120]


def parse_semester_start(value):
    if not value:
        raise ValueError("请设置 activity.semester_start_date（本学期第一周周一，YYYY-MM-DD）")
    start = datetime.strptime(str(value), "%Y-%m-%d").date()
    if start.weekday() != 0:
        raise ValueError("学期起始日期必须是第一周的周一")
    return start


def parse_activity(message: Message, year: int | None = None, default_weekdays: tuple[int, ...] = (0, 1, 2, 3, 4),
                   semester_start_date: str | None = None) -> Activity:
    year = year or datetime.now().year
    text = message.text.replace("：", ":").replace("～", "~")
    document = DOC_RE.search(text)
    if not document:
        raise ValueError("消息不包含报名文档链接")
    slots = []
    locations = ()
    location_line = re.search(r"(?:【\s*)?地点(?:\s*】)?\s*[：:]\s*([^\n]+)", text)
    if location_line:
        locations = tuple(item.strip(" 、,，") for item in re.split(r"[、,，]", location_line.group(1)) if item.strip(" 、,，"))
    for match in RANGE_RE.finditer(text):
        month, day, sh, sm, eh, em = map(int, match.groups())
        start = datetime(year, month, day, sh, sm)
        end = datetime(year, month, day, eh, em)
        if end <= start:
            end += timedelta(days=1)
        slots.append(TimeSlot(start, end))
    if not slots:
        reference = datetime.now()
        weekdays = list(WEEKDAY_RE.finditer(text))
        week = WEEK_NUMBER_RE.search(text)
        monday = None
        if week and (weekdays or default_weekdays):
            number = _chinese_number(week.group(2))
            if number < 1:
                raise ValueError("学期周次必须从第 1 周开始")
            monday = parse_semester_start(semester_start_date) + timedelta(weeks=number - 1)
        if not weekdays:
            if monday is not None:
                for match in TIME_ONLY_RE.finditer(text):
                    sh, sm, eh, em = map(int, match.groups())
                    for weekday in default_weekdays:
                        day = monday + timedelta(days=weekday)
                        start = datetime.combine(day, time(sh, sm))
                        end = datetime.combine(day, time(eh, em))
                        if end <= start:
                            end += timedelta(days=1)
                        slots.append(TimeSlot(start, end))
            if not slots:
                return Activity(document.group(0), activity_title(message.text), (), locations)
            return Activity(document.group(0), activity_title(message.text), tuple(slots), locations)
        for match in TIME_ONLY_RE.finditer(text):
            previous = [item for item in weekdays if item.start() < match.start()]
            weekday_match = previous[-1] if previous else None
            weekday = min("一二三四五六日天".index(weekday_match.group(1)), 6) if weekday_match else reference.weekday()
            day = monday + timedelta(days=weekday) if monday is not None else reference.date() + timedelta(days=(weekday - reference.weekday()) % 7)
            sh, sm, eh, em = map(int, match.groups())
            start = datetime.combine(day, time(sh, sm))
            end = datetime.combine(day, time(eh, em))
            if end <= start:
                end += timedelta(days=1)
            slots.append(TimeSlot(start, end))
    title = activity_title(message.text)
    return Activity(document.group(0), title, tuple(slots), locations)


def extract_group_links(text: str) -> list[str]:
    return list(dict.fromkeys(match.group(0) for match in GROUP_URL_RE.finditer(text)))


def classify_link(url: str) -> str:
    lower = url.lower()
    if "qm.qq.com" in lower or "qun.qq.com" in lower:
        return "qq_group"
    if "weixin" in lower:
        return "wechat_group"
    if "kdocs.cn" in lower:
        return "wps_docs"
    if "docs.qq.com" in lower:
        return "qq_docs"
    return "unknown"
