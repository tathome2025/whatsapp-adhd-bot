from app.parser import parse_task_text


def test_parse_with_due_time_and_title() -> None:
    parsed = parse_task_text("下星期二 3pm 同客開會", "Asia/Hong_Kong")
    assert parsed.title
    assert parsed.due_at_utc is not None


def test_parse_without_date() -> None:
    parsed = parse_task_text("整理報價單", "Asia/Hong_Kong")
    assert parsed.title == "整理報價單"
    assert parsed.due_at_utc is None


def test_priority_keywords() -> None:
    parsed = parse_task_text("緊急 明天 9am 交 proposal", "Asia/Hong_Kong")
    assert parsed.priority == 3
