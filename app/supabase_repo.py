from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

from app.config import Settings


class SupabaseRepo:
    def __init__(self, settings: Settings):
        self.settings = settings

    def _headers(self, prefer: str | None = None) -> dict[str, str]:
        headers = {
            "apikey": self.settings.supabase_service_role_key,
            "Authorization": f"Bearer {self.settings.supabase_service_role_key}",
            "Content-Type": "application/json",
        }
        if prefer:
            headers["Prefer"] = prefer
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | list[tuple[str, Any]] | None = None,
        json_data: dict[str, Any] | list[dict[str, Any]] | None = None,
        prefer: str | None = None,
    ) -> Any:
        if not self.settings.supabase_url or not self.settings.supabase_service_role_key:
            raise RuntimeError("Supabase settings are not configured")

        url = f"{self.settings.supabase_rest_url}/{path.lstrip('/')}"
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.request(
                method,
                url,
                headers=self._headers(prefer),
                params=params,
                json=json_data,
            )
        response.raise_for_status()
        if not response.text:
            return None
        return response.json()

    async def create_task(self, task: dict[str, Any]) -> dict[str, Any] | None:
        data = await self._request(
            "POST",
            "tasks",
            params={"on_conflict": "source_message_id"},
            json_data=task,
            prefer="return=representation,resolution=ignore-duplicates",
        )

        if data:
            return data[0]

        message_id = task.get("source_message_id")
        if not message_id:
            return None

        existing = await self._request(
            "GET",
            "tasks",
            params={
                "select": "*",
                "source_message_id": f"eq.{message_id}",
                "limit": "1",
            },
        )
        return existing[0] if existing else None

    async def list_open_tasks(self, chat_id: str, limit: int = 100) -> list[dict[str, Any]]:
        data = await self._request(
            "GET",
            "tasks",
            params={
                "select": "*",
                "chat_id": f"eq.{chat_id}",
                "status": "eq.open",
                "order": "due_at.asc.nullslast,priority.desc,created_at.asc",
                "limit": str(limit),
            },
        )
        return data or []

    async def list_tasks_for_date(
        self,
        chat_id: str,
        start_utc_iso: str,
        end_utc_iso: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        data = await self._request(
            "GET",
            "tasks",
            params=[
                ("select", "*"),
                ("chat_id", f"eq.{chat_id}"),
                ("status", "eq.open"),
                ("due_at", f"gte.{start_utc_iso}"),
                ("due_at", f"lt.{end_utc_iso}"),
                ("order", "priority.desc,due_at.asc,created_at.asc"),
                ("limit", str(limit)),
            ],
        )
        return data or []

    async def mark_done(self, chat_id: str, task_id: int) -> dict[str, Any] | None:
        payload = {
            "status": "done",
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        updated = await self._request(
            "PATCH",
            "tasks",
            params={
                "chat_id": f"eq.{chat_id}",
                "id": f"eq.{task_id}",
                "status": "eq.open",
            },
            json_data=payload,
            prefer="return=representation",
        )
        return updated[0] if updated else None

    async def list_active_chat_ids(self) -> list[str]:
        tasks = await self._request(
            "GET",
            "tasks",
            params={
                "select": "chat_id",
                "status": "eq.open",
                "limit": "1000",
            },
        )
        profiles = await self._request(
            "GET",
            "user_profiles",
            params={
                "select": "chat_id",
                "limit": "1000",
            },
        )

        chat_ids = {row["chat_id"] for row in (tasks or [])}
        chat_ids.update({row["chat_id"] for row in (profiles or [])})
        return sorted(chat_ids)

    async def get_user_profile(self, chat_id: str) -> dict[str, Any]:
        profile = await self._request(
            "GET",
            "user_profiles",
            params={
                "select": "*",
                "chat_id": f"eq.{chat_id}",
                "limit": "1",
            },
        )

        if profile:
            return profile[0]

        return {
            "chat_id": chat_id,
            "timezone": self.settings.timezone,
            "max_daily_tasks": self.settings.max_daily_tasks,
            "focus_window": "09:00-12:00",
            "break_pref": "25-5",
        }

    async def save_daily_plan(
        self,
        chat_id: str,
        plan_date_iso: str,
        ordered_task_ids: list[int],
        rationale: dict[str, Any],
    ) -> None:
        await self._request(
            "POST",
            "daily_plans",
            json_data={
                "chat_id": chat_id,
                "plan_date": plan_date_iso,
                "ordered_task_ids": ordered_task_ids,
                "rationale": rationale,
            },
            prefer="return=minimal",
        )
