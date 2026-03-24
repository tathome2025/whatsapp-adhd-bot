from app.parser import parse_task_text
from calendar import monthrange
from datetime import datetime
from zoneinfo import ZoneInfo


def test_parse_with_due_time_and_title() -> None:
    parsed = parse_task_text("下星期二 3pm 同客開會", "Asia/Hong_Kong")
    assert parsed.title
    assert parsed.due_at_utc is not None
    assert parsed.title == "同客開會"


def test_parse_without_date() -> None:
    parsed = parse_task_text("整理報價單", "Asia/Hong_Kong")
    assert parsed.title == "整理報價單"
    assert parsed.due_at_utc is None


def test_priority_keywords() -> None:
    parsed = parse_task_text("緊急 明天 9am 交 proposal", "Asia/Hong_Kong")
    assert parsed.priority == 3


def test_parse_relative_weekday_without_time() -> None:
    parsed = parse_task_text("下禮拜二 同客開會", "Asia/Hong_Kong")
    assert parsed.due_at_utc is not None
    assert parsed.title == "同客開會"


def test_parse_named_relative_day() -> None:
    parsed = parse_task_text("後天 4pm 跟進客戶", "Asia/Hong_Kong")
    assert parsed.due_at_utc is not None
    assert parsed.title == "跟進客戶"


def test_parse_month_anchor() -> None:
    parsed = parse_task_text("月尾 結算", "Asia/Hong_Kong")
    assert parsed.due_at_utc is not None

    local_due = datetime.fromisoformat(parsed.due_at_utc).astimezone(ZoneInfo("Asia/Hong_Kong"))
    assert local_due.day == monthrange(local_due.year, local_due.month)[1]


def test_parse_next_month_start_with_time() -> None:
    parsed = parse_task_text("下個月頭 10:00 交租", "Asia/Hong_Kong")
    assert parsed.due_at_utc is not None
    assert parsed.title == "交租"

    local_due = datetime.fromisoformat(parsed.due_at_utc).astimezone(ZoneInfo("Asia/Hong_Kong"))
    assert local_due.day == 1
    assert local_due.hour == 10
