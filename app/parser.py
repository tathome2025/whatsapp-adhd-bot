from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
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
RELATIVE_WEEKDAY_PATTERN = re.compile(
    r"(?P<full>(?P<prefix>下下|下|今|這|这|呢|本)?\\s*(?:個|个)?\\s*(?:星期|週|周|禮拜|礼拜)\\s*(?P<weekday>[一二三四五六日天]))"
)
WEEKDAY_MAP = {
    "一": 0,
    "二": 1,
    "三": 2,
    "四": 3,
    "五": 4,
    "六": 5,
    "日": 6,
    "天": 6,
}
WEEK_OFFSET_MAP = {
    "下下": 2,
    "下": 1,
    "今": 0,
    "這": 0,
    "这": 0,
    "呢": 0,
    "本": 0,
    "": 0,
}


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


def _normalize_title(raw_text: str, fragments: list[str] | None) -> str:
    title = raw_text
    for fragment in fragments or []:
        clean = (fragment or "").strip()
        if clean:
            title = title.replace(clean, " ", 1)

    title = re.sub(r"\\s+", " ", title).strip(" ,，。.!！？")
    return title


def _compute_relative_weekday_due(
    match: re.Match[str],
    now_local: datetime,
    timezone_name: str,
    source_text: str,
) -> tuple[datetime, list[str]] | None:
    weekday_raw = (match.group("weekday") or "").strip()
    target_weekday = WEEKDAY_MAP.get(weekday_raw)
    if target_weekday is None:
        return None

    prefix_raw = (match.group("prefix") or "").strip()
    week_offset = WEEK_OFFSET_MAP.get(prefix_raw, 0)

    today = now_local.date()
    this_week_start = today - timedelta(days=today.weekday())
    target_date = this_week_start + timedelta(days=target_weekday + (7 * week_offset))

    # Keep future preference for phrases without an explicit "next week" offset.
    if week_offset == 0 and target_date <= today:
        target_date = target_date + timedelta(days=7)

    local_tz = ZoneInfo(timezone_name)
    due_local = datetime(
        year=target_date.year,
        month=target_date.month,
        day=target_date.day,
        hour=12,
        minute=0,
        second=0,
        microsecond=0,
        tzinfo=local_tz,
    )

    date_fragment = (match.group("full") or "").strip()
    fragments = [date_fragment] if date_fragment else []

    remainder = source_text.replace(date_fragment, " ", 1)
    parser_settings = {
        "PREFER_DATES_FROM": "future",
        "TIMEZONE": timezone_name,
        "RETURN_AS_TIMEZONE_AWARE": True,
        "RELATIVE_BASE": due_local,
    }
    time_matches = search_dates(remainder, languages=["zh", "en"], settings=parser_settings) or []
    time_fragment = None
    time_value = None
    for fragment, parsed in time_matches:
        if _has_explicit_time(fragment):
            time_fragment = fragment
            time_value = parsed
            break

    if time_value:
        if time_value.tzinfo is None:
            time_value = time_value.replace(tzinfo=local_tz)
        time_local = time_value.astimezone(local_tz)
        due_local = due_local.replace(
            hour=time_local.hour,
            minute=time_local.minute,
            second=0,
            microsecond=0,
        )
        if time_fragment:
            fragments.append(time_fragment)

    return due_local, fragments


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
    matched_fragments: list[str] = []

    relative_match = RELATIVE_WEEKDAY_PATTERN.search(cleaned)
    if relative_match:
        relative_due = _compute_relative_weekday_due(relative_match, now_local, timezone_name, cleaned)
        if relative_due:
            due_local, matched_fragments = relative_due

    if due_local is None:
        matches = search_dates(cleaned, languages=["zh", "en"], settings=parser_settings)
        if matches:
            matched_fragment, due_local = matches[0]
            if matched_fragment:
                matched_fragments = [matched_fragment]
        else:
            parsed = dateparser.parse(cleaned, languages=["zh", "en"], settings=parser_settings)
            if parsed:
                due_local = parsed
                matched_fragments = [cleaned]

    if due_local and due_local.tzinfo is None:
        due_local = due_local.replace(tzinfo=local_tz)

    merged_fragment = " ".join(matched_fragments).strip()
    if due_local and not _has_explicit_time(merged_fragment or cleaned):
        due_local = due_local.astimezone(local_tz).replace(hour=12, minute=0, second=0, microsecond=0)

    title = _normalize_title(cleaned, matched_fragments)
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
