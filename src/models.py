from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class TimeSlot:
    start: datetime
    end: datetime

    def overlaps(self, other: "TimeSlot") -> bool:
        return self.start < other.end and other.start < self.end


@dataclass(frozen=True)
class Message:
    message_id: str
    text: str
    source: str = "qq"
    image_paths: tuple[str, ...] = ()
    group_id: str = ""


@dataclass(frozen=True)
class Activity:
    url: str
    title: str
    slots: tuple[TimeSlot, ...]
    locations: tuple[str, ...] = ()
