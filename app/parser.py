import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import dateparser
from dateparser.search import search_dates

from app.models import ParsedTask

PRIORITY_KEYWORDS = {
    3: ["緊急", "urgent", "asap", "盡快", "立刻", "最遲", "deadline"],
    2: ["重要", "normal", "一般", "稍後", "today", "today"],
    1: ["低", "later", "有空", "唔急", "不急"],
}

ENERGY_KEYWORDS = {
    "high": ["深度", "寫方案", "設計", "開會", "決策", "focus"],
    "medium": ["整理", "回覆", "review", "檢查", "follow up"],
    "low": ["簡單", "quick", "小事", "行政", "抄錄"],
}

TIME_PATTERN = re.compile(
    r"(?ix)(\\b\\d{1,2}(:\\d{2})?\\s?(am|pm)\\b|\\b\\d{1,2}:\\d{2}\\b|\\b\\d{1,2}\\s?(點|点|時|时)\\b)"
)
EFFORT_PATTERN = re.compile(
    r"(?ix)(\\d{1,3})\\s*(分鐘|分|mins?|minutes?|hr|hrs|hours?|小時|小时)"
)


def _has_explicit_time(text: str) -> bool:
    return bool(TIME_PATTERN.search(text))


def _infer_priority(text: str) -> int:
    lowered = text.lower()
    for value, keywords in PRIORITY_KEYWORDS.items():
        if any(k.lower() in lowered for k in keywords):
            return value
    return 2


def _infer_energy_need(text: str) -> str:
    lowered = text.lower()
    for energy, keywords in ENERGY_KEYWORDS.items():
        if any(k.lower() in lowered for k in keywords):
            return energy
    return "medium"


def _extract_effort_minutes(text: str) -> int | None:
    match = EFFORT_PATTERN.search(text)
    if not match:
        return None

    value = int(match.group(1))
    unit = match.group(2).lower()

    if unit in {"hr", "hrs", "hour", "hours", "小時", "小时"}:
        return value * 60
    return value


def _normalize_title(raw_text: str, date_fragment: str | None) -> str:
    title = raw_text
    if date_fragment:
        title = title.replace(date_fragment, " ", 1)

    title = re.sub(r"\\s+", " ", title).strip(" ,，。.!！？")
    return title


def parse_task_text(text: str, timezone_name: str) -> ParsedTask:
    cleaned = text.strip()
    if not cleaned:
        return ParsedTask(
            title="",
            priority=2,
            due_at_utc=None,
            due_at_local=None,
            effort_min=None,
            energy_need="medium",
        )

    local_tz = ZoneInfo(timezone_name)
    now_local = datetime.now(local_tz)

    parser_settings = {
        "PREFER_DATES_FROM": "future",
        "TIMEZONE": timezone_name,
        "RETURN_AS_TIMEZONE_AWARE": True,
        "RELATIVE_BASE": now_local,
    }

    due_local = None
    matched_fragment = None

    matches = search_dates(cleaned, languages=["zh", "en"], settings=parser_settings)
    if matches:
        matched_fragment, due_local = matches[0]
    else:
        parsed = dateparser.parse(cleaned, languages=["zh", "en"], settings=parser_settings)
        if parsed:
            due_local = parsed
            matched_fragment = cleaned

    if due_local and due_local.tzinfo is None:
        due_local = due_local.replace(tzinfo=local_tz)

    if due_local and not _has_explicit_time(matched_fragment or cleaned):
        due_local = due_local.astimezone(local_tz).replace(hour=17, minute=0, second=0, microsecond=0)

    title = _normalize_title(cleaned, matched_fragment)
    if not title and due_local:
        title = "未命名任務"

    due_at_utc = due_local.astimezone(timezone.utc).isoformat() if due_local else None

    return ParsedTask(
        title=title,
        priority=_infer_priority(cleaned),
        due_at_utc=due_at_utc,
        due_at_local=due_local,
        effort_min=_extract_effort_minutes(cleaned),
        energy_need=_infer_energy_need(cleaned),
    )
