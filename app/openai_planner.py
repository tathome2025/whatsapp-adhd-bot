from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)


class OpenAIPlanner:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def rank_tasks_for_adhd(
        self,
        chat_id: str,
        tasks: list[dict[str, Any]],
        profile: dict[str, Any],
    ) -> dict[str, Any]:
        if not tasks:
            return {
                "ordered_tasks": [],
                "top_3_now": [],
                "reasons": [],
                "suggested_time_blocks": [],
                "fallback": False,
            }

        fallback_order = self._fallback_order(tasks)

        if not self.settings.openai_api_key:
            return {
                "ordered_tasks": fallback_order,
                "top_3_now": fallback_order[:3],
                "reasons": ["OpenAI API 未設定，使用規則排序。"],
                "suggested_time_blocks": [],
                "fallback": True,
            }

        try:
            return await self._rank_with_openai(chat_id, tasks, profile, fallback_order)
        except Exception as exc:  # noqa: BLE001
            logger.exception("OpenAI ranking failed: %s", exc)
            return {
                "ordered_tasks": fallback_order,
                "top_3_now": fallback_order[:3],
                "reasons": ["AI 排序暫時不可用，已改用規則排序。"],
                "suggested_time_blocks": [],
                "fallback": True,
            }

    async def _rank_with_openai(
        self,
        chat_id: str,
        tasks: list[dict[str, Any]],
        profile: dict[str, Any],
        fallback_order: list[dict[str, Any]],
    ) -> dict[str, Any]:
        task_input = [
            {
                "id": t["id"],
                "title": t["title"],
                "source_text": t.get("source_text"),
                "due_at": t.get("due_at"),
                "priority": t.get("priority", 2),
                "effort_min": t.get("effort_min"),
                "energy_need": t.get("energy_need", "medium"),
            }
            for t in tasks
        ]

        prompt = {
            "chat_id": chat_id,
            "timezone": profile.get("timezone", self.settings.timezone),
            "max_daily_tasks": profile.get("max_daily_tasks", self.settings.max_daily_tasks),
            "focus_window": profile.get("focus_window", "09:00-12:00"),
            "break_pref": profile.get("break_pref", "25-5"),
            "tasks": task_input,
            "goal": (
                "根據任務自然語言內容（source_text/title）、截止時間、優先度、預估投入時間，"
                "輸出 ADHD 友善的執行順序（先易後難啟動，兼顧 deadline）。"
            ),
        }

        payload = {
            "model": self.settings.openai_model,
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是任務排序助理。只輸出 JSON，不要 markdown。"
                        "JSON 格式: {ordered_task_ids:number[], top_3_now:number[],"
                        "reasons:string[], suggested_time_blocks:object[]}"
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(prompt, ensure_ascii=False),
                },
            ],
        }

        headers = {
            "Authorization": f"Bearer {self.settings.openai_api_key}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers=headers,
                json=payload,
            )
        resp.raise_for_status()

        content = resp.json()["choices"][0]["message"]["content"]
        parsed = json.loads(self._strip_code_fence(content))

        by_id = {int(t["id"]): t for t in tasks}
        ordered_ids = self._safe_task_ids(parsed.get("ordered_task_ids", []), by_id)

        ordered_tasks: list[dict[str, Any]] = [by_id[task_id] for task_id in ordered_ids]
        for task in fallback_order:
            if task["id"] not in {t["id"] for t in ordered_tasks}:
                ordered_tasks.append(task)

        top_3_ids = self._safe_task_ids(parsed.get("top_3_now", []), by_id)
        top_3_now = [by_id[task_id] for task_id in top_3_ids] or ordered_tasks[:3]

        return {
            "ordered_tasks": ordered_tasks,
            "top_3_now": top_3_now,
            "reasons": parsed.get("reasons", []),
            "suggested_time_blocks": parsed.get("suggested_time_blocks", []),
            "fallback": False,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    @staticmethod
    def _strip_code_fence(text: str) -> str:
        stripped = text.strip()
        if stripped.startswith("```"):
            stripped = stripped.strip("`")
            if stripped.startswith("json"):
                stripped = stripped[4:]
        return stripped.strip()

    @staticmethod
    def _fallback_order(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        def sort_key(task: dict[str, Any]) -> tuple:
            due = task.get("due_at") or "9999-12-31T23:59:59+00:00"
            priority = int(task.get("priority", 2))
            effort = int(task.get("effort_min") or 30)
            return (due, -priority, effort)

        return sorted(tasks, key=sort_key)

    @staticmethod
    def _safe_task_ids(values: list[Any], by_id: dict[int, dict[str, Any]]) -> list[int]:
        result: list[int] = []
        for value in values:
            try:
                task_id = int(value)
            except (TypeError, ValueError):
                continue
            if task_id in by_id and task_id not in result:
                result.append(task_id)
        return result
