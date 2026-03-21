from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass
class ParsedTask:
    title: str
    priority: int
    due_at_utc: str | None
    due_at_local: datetime | None
    effort_min: int | None
    energy_need: str


@dataclass
class InboundMessage:
    message_id: str
    chat_id: str
    text: str
    raw_payload: dict[str, Any]
