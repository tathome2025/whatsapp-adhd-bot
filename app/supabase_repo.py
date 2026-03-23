from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

import httpx

from app.config import Settings


def _normalize_phone(value: str) -> str:
    return re.sub(r"\D+", "", value or "")


def _normalize_chat_id(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""

    if raw.endswith("@g.us"):
        return raw

    if re.fullmatch(r"\d+-\d+", raw):
        return raw

    digits = _normalize_phone(raw)
    return digits or raw


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
        task = {**task, "chat_id": _normalize_chat_id(str(task.get("chat_id") or ""))}

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
        chat_id = _normalize_chat_id(chat_id)
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

    async def list_tasks(self, chat_id: str, status: str = "open", limit: int = 500) -> list[dict[str, Any]]:
        chat_id = _normalize_chat_id(chat_id)
        params: dict[str, str] = {
            "select": "*",
            "chat_id": f"eq.{chat_id}",
            "order": "status.asc,due_at.asc.nullslast,priority.desc,created_at.asc",
            "limit": str(limit),
        }
        if status in {"open", "done"}:
            params["status"] = f"eq.{status}"
        data = await self._request("GET", "tasks", params=params)
        return data or []

    async def list_tasks_for_date(
        self,
        chat_id: str,
        start_utc_iso: str,
        end_utc_iso: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        chat_id = _normalize_chat_id(chat_id)
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

    async def get_open_task_by_task_no(self, chat_id: str, task_no: int) -> dict[str, Any] | None:
        chat_id = _normalize_chat_id(chat_id)
        tasks = await self._request(
            "GET",
            "tasks",
            params={
                "select": "*",
                "chat_id": f"eq.{chat_id}",
                "task_no": f"eq.{task_no}",
                "status": "eq.open",
                "limit": "1",
            },
        )
        return tasks[0] if tasks else None

    async def mark_done_by_task_no(self, chat_id: str, task_no: int) -> dict[str, Any] | None:
        chat_id = _normalize_chat_id(chat_id)
        payload = {
            "status": "done",
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        updated = await self._request(
            "PATCH",
            "tasks",
            params={
                "chat_id": f"eq.{chat_id}",
                "task_no": f"eq.{task_no}",
                "status": "eq.open",
            },
            json_data=payload,
            prefer="return=representation",
        )
        return updated[0] if updated else None

    async def update_task_by_task_no(
        self,
        chat_id: str,
        task_no: int,
        patch: dict[str, Any],
    ) -> dict[str, Any] | None:
        chat_id = _normalize_chat_id(chat_id)
        updated = await self._request(
            "PATCH",
            "tasks",
            params={
                "chat_id": f"eq.{chat_id}",
                "task_no": f"eq.{task_no}",
                "status": "eq.open",
            },
            json_data=patch,
            prefer="return=representation",
        )
        return updated[0] if updated else None

    async def delete_task_by_task_no(self, chat_id: str, task_no: int) -> dict[str, Any] | None:
        chat_id = _normalize_chat_id(chat_id)
        deleted = await self._request(
            "DELETE",
            "tasks",
            params={
                "chat_id": f"eq.{chat_id}",
                "task_no": f"eq.{task_no}",
            },
            prefer="return=representation",
        )
        return deleted[0] if deleted else None

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
        bindings = await self._request(
            "GET",
            "task_list_bindings",
            params={
                "select": "chat_id,list_chat_id",
                "limit": "1000",
            },
        )

        chat_ids = {row["chat_id"] for row in (tasks or [])}
        chat_ids.update({row["chat_id"] for row in (profiles or [])})
        for row in bindings or []:
            chat_ids.add(str(row.get("chat_id") or ""))
            chat_ids.add(str(row.get("list_chat_id") or ""))

        chat_ids.discard("")
        return sorted(chat_ids)

    async def get_user_profile(self, chat_id: str) -> dict[str, Any]:
        chat_id = _normalize_chat_id(chat_id)
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
        chat_id = _normalize_chat_id(chat_id)
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

    async def is_whitelisted_sender(self, sender_id: str) -> bool:
        normalized = _normalize_phone(sender_id)
        if not normalized:
            return False

        rows = await self._request(
            "GET",
            "whitelist_contacts",
            params={
                "select": "sender_id",
                "sender_id": f"eq.{normalized}",
                "limit": "1",
            },
        )
        return bool(rows)

    async def list_whitelist_contacts(self) -> list[dict[str, Any]]:
        rows = await self._request(
            "GET",
            "whitelist_contacts",
            params={
                "select": "sender_id,label,created_at",
                "order": "created_at.desc",
                "limit": "1000",
            },
        )
        return rows or []

    async def upsert_whitelist_contact(self, sender_id: str, label: str = "") -> dict[str, Any]:
        normalized = _normalize_phone(sender_id)
        if not normalized:
            raise ValueError("Invalid sender id")

        rows = await self._request(
            "POST",
            "whitelist_contacts",
            params={"on_conflict": "sender_id"},
            json_data={
                "sender_id": normalized,
                "label": label.strip() if label else None,
            },
            prefer="return=representation,resolution=merge-duplicates",
        )
        return rows[0]

    async def remove_whitelist_contact(self, sender_id: str) -> bool:
        normalized = _normalize_phone(sender_id)
        if not normalized:
            return False

        deleted = await self._request(
            "DELETE",
            "whitelist_contacts",
            params={
                "sender_id": f"eq.{normalized}",
            },
            prefer="return=representation",
        )
        return bool(deleted)

    async def resolve_task_scope(self, chat_id: str) -> str:
        normalized = _normalize_chat_id(chat_id)
        if not normalized:
            return normalized

        rows = await self._request(
            "GET",
            "task_list_bindings",
            params={
                "select": "list_chat_id",
                "chat_id": f"eq.{normalized}",
                "limit": "1",
            },
        )
        if rows:
            return _normalize_chat_id(str(rows[0].get("list_chat_id") or normalized))
        return normalized

    async def list_task_bindings(self) -> list[dict[str, Any]]:
        rows = await self._request(
            "GET",
            "task_list_bindings",
            params={
                "select": "chat_id,list_chat_id,created_at,updated_at",
                "order": "updated_at.desc",
                "limit": "1000",
            },
        )
        return rows or []

    async def upsert_task_binding(self, chat_id: str, list_chat_id: str) -> dict[str, Any]:
        chat_norm = _normalize_chat_id(chat_id)
        scope_norm = _normalize_chat_id(list_chat_id)
        if not chat_norm or not scope_norm:
            raise ValueError("Invalid chat id or list id")

        rows = await self._request(
            "POST",
            "task_list_bindings",
            params={"on_conflict": "chat_id"},
            json_data={
                "chat_id": chat_norm,
                "list_chat_id": scope_norm,
            },
            prefer="return=representation,resolution=merge-duplicates",
        )
        return rows[0]

    async def remove_task_binding(self, chat_id: str) -> bool:
        chat_norm = _normalize_chat_id(chat_id)
        if not chat_norm:
            return False

        deleted = await self._request(
            "DELETE",
            "task_list_bindings",
            params={
                "chat_id": f"eq.{chat_norm}",
            },
            prefer="return=representation",
        )
        return bool(deleted)

    async def health_check(self) -> dict[str, Any]:
        if not self.settings.supabase_url or not self.settings.supabase_service_role_key:
            return {
                "ok": False,
                "error": "SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY is missing",
            }

        try:
            await self._request(
                "GET",
                "tasks",
                params={
                    "select": "id",
                    "limit": "1",
                },
            )
            return {"ok": True}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
