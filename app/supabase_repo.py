from __future__ import annotations

import re
import secrets
import time
import hashlib
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

    if raw.startswith("tl_"):
        return raw

    if raw.endswith("@g.us"):
        return raw

    if re.fullmatch(r"\d+-\d+", raw):
        return raw

    digits = _normalize_phone(raw)
    return digits or raw


def _parse_list_id(value: str | int | None) -> int | None:
    if value is None:
        return None

    raw = str(value).strip()
    if not raw:
        return None

    if raw.startswith("#"):
        raw = raw[1:]

    if not raw.isdigit():
        return None

    list_id = int(raw)
    if list_id <= 0:
        return None
    return list_id


def _normalize_list_key(value: str | None) -> str:
    raw = (value or "").strip().lower()
    if not raw:
        return ""

    key = re.sub(r"[^a-z0-9_-]+", "-", raw)
    key = re.sub(r"-{2,}", "-", key).strip("-_")
    return key[:64]


def _suffix_list_key(base: str, suffix: str) -> str:
    clean_base = _normalize_list_key(base) or "list"
    clean_suffix = _normalize_list_key(suffix) or "x"
    max_base_len = 64 - len(clean_suffix) - 1
    if max_base_len <= 0:
        return clean_suffix[:64]
    return f"{clean_base[:max_base_len]}-{clean_suffix}"


def _default_list_key_for_chat(chat_id: str) -> str:
    digest = hashlib.sha1(chat_id.encode("utf-8")).hexdigest()[:10]
    return f"default-{digest}"


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

    # ----------------------------
    # Task list internals
    # ----------------------------
    def _make_list_scope_chat_id(self, owner_chat_id: str) -> str:
        owner = _normalize_chat_id(owner_chat_id) or "owner"
        ts = f"{int(time.time()):x}"
        rand = secrets.token_hex(4)
        return f"tl_{owner}_{ts}_{rand}"

    async def _get_task_list_by_id(self, list_id: int) -> dict[str, Any] | None:
        rows = await self._request(
            "GET",
            "task_lists",
            params={
                "select": "id,name,list_key,owner_chat_id,scope_chat_id,is_archived,created_at,updated_at",
                "id": f"eq.{int(list_id)}",
                "is_archived": "eq.false",
                "limit": "1",
            },
        )
        return rows[0] if rows else None

    async def _get_task_list_by_scope(self, scope_chat_id: str) -> dict[str, Any] | None:
        scope = _normalize_chat_id(scope_chat_id)
        rows = await self._request(
            "GET",
            "task_lists",
            params={
                "select": "id,name,list_key,owner_chat_id,scope_chat_id,is_archived,created_at,updated_at",
                "scope_chat_id": f"eq.{scope}",
                "is_archived": "eq.false",
                "limit": "1",
            },
        )
        return rows[0] if rows else None

    async def _get_task_list_by_key(self, list_key: str) -> dict[str, Any] | None:
        key = _normalize_list_key(list_key)
        if not key:
            return None

        rows = await self._request(
            "GET",
            "task_lists",
            params={
                "select": "id,name,list_key,owner_chat_id,scope_chat_id,is_archived,created_at,updated_at",
                "list_key": f"eq.{key}",
                "is_archived": "eq.false",
                "limit": "1",
            },
        )
        return rows[0] if rows else None

    async def _make_unique_list_key(self, preferred: str) -> str:
        base = _normalize_list_key(preferred) or "list"
        candidate = base
        for idx in range(1, 101):
            existing = await self._get_task_list_by_key(candidate)
            if not existing:
                return candidate
            candidate = _suffix_list_key(base, str(idx + 1))

        # Fallback should be practically unreachable, but keeps creation robust.
        return _suffix_list_key(base, f"{int(time.time()):x}")

    async def _upsert_membership(
        self,
        chat_id: str,
        list_id: int,
        role: str = "member",
        is_default: bool | None = None,
    ) -> dict[str, Any]:
        chat_norm = _normalize_chat_id(chat_id)
        payload: dict[str, Any] = {
            "chat_id": chat_norm,
            "list_id": int(list_id),
            "role": role,
        }
        if is_default is not None:
            payload["is_default"] = bool(is_default)

        rows = await self._request(
            "POST",
            "task_list_members",
            params={"on_conflict": "chat_id,list_id"},
            json_data=payload,
            prefer="return=representation,resolution=merge-duplicates",
        )
        return rows[0]

    async def _list_memberships_for_chat(self, chat_id: str, limit: int = 200) -> list[dict[str, Any]]:
        chat_norm = _normalize_chat_id(chat_id)
        rows = await self._request(
            "GET",
            "task_list_members",
            params={
                "select": "id,chat_id,list_id,role,is_default,created_at",
                "chat_id": f"eq.{chat_norm}",
                "order": "is_default.desc,created_at.asc",
                "limit": str(limit),
            },
        )
        return rows or []

    async def _set_default_membership(self, chat_id: str, list_id: int) -> None:
        chat_norm = _normalize_chat_id(chat_id)
        await self._request(
            "PATCH",
            "task_list_members",
            params={
                "chat_id": f"eq.{chat_norm}",
            },
            json_data={"is_default": False},
            prefer="return=minimal",
        )

        updated = await self._request(
            "PATCH",
            "task_list_members",
            params={
                "chat_id": f"eq.{chat_norm}",
                "list_id": f"eq.{int(list_id)}",
            },
            json_data={"is_default": True},
            prefer="return=representation",
        )
        if not updated:
            await self._upsert_membership(chat_norm, int(list_id), role="member", is_default=True)

    async def _ensure_default_task_list(self, chat_id: str) -> dict[str, Any]:
        chat_norm = _normalize_chat_id(chat_id)
        if not chat_norm:
            raise ValueError("Invalid chat id")

        memberships = await self._list_memberships_for_chat(chat_norm)
        if memberships:
            default_row = next((row for row in memberships if bool(row.get("is_default"))), memberships[0])
            default_list = await self._get_task_list_by_id(int(default_row["list_id"]))
            if default_list:
                if not bool(default_row.get("is_default")):
                    await self._set_default_membership(chat_norm, int(default_list["id"]))
                return default_list

        default_list = await self._get_task_list_by_scope(chat_norm)
        if not default_list:
            created = await self._request(
                "POST",
                "task_lists",
                params={"on_conflict": "scope_chat_id"},
                json_data={
                    "name": "Default",
                    "list_key": _default_list_key_for_chat(chat_norm),
                    "owner_chat_id": chat_norm,
                    "scope_chat_id": chat_norm,
                },
                prefer="return=representation,resolution=merge-duplicates",
            )
            default_list = created[0]

        role = "owner" if str(default_list.get("owner_chat_id") or "") == chat_norm else "member"
        await self._upsert_membership(chat_norm, int(default_list["id"]), role=role, is_default=True)
        await self._set_default_membership(chat_norm, int(default_list["id"]))
        return default_list

    # ----------------------------
    # Task list public APIs
    # ----------------------------
    async def list_task_lists_for_chat(self, chat_id: str) -> list[dict[str, Any]]:
        chat_norm = _normalize_chat_id(chat_id)
        if not chat_norm:
            return []

        memberships = await self._list_memberships_for_chat(chat_norm)
        if not memberships:
            await self._ensure_default_task_list(chat_norm)
            memberships = await self._list_memberships_for_chat(chat_norm)

        list_ids = sorted({int(row["list_id"]) for row in memberships})
        if not list_ids:
            return []

        id_filter = ",".join(str(x) for x in list_ids)
        lists = await self._request(
            "GET",
            "task_lists",
            params={
                "select": "id,name,list_key,owner_chat_id,scope_chat_id,is_archived,created_at,updated_at",
                "id": f"in.({id_filter})",
                "is_archived": "eq.false",
                "limit": str(max(200, len(list_ids))),
            },
        )
        by_id = {int(row["id"]): row for row in (lists or [])}

        out: list[dict[str, Any]] = []
        for member in memberships:
            list_id = int(member["list_id"])
            list_row = by_id.get(list_id)
            if not list_row:
                continue
            out.append(
                {
                    "list_id": list_id,
                    "list_name": str(list_row.get("name") or ""),
                    "list_key": str(list_row.get("list_key") or ""),
                    "scope_chat_id": str(list_row.get("scope_chat_id") or ""),
                    "owner_chat_id": str(list_row.get("owner_chat_id") or ""),
                    "is_default": bool(member.get("is_default")),
                    "role": str(member.get("role") or "member"),
                    "member_chat_id": chat_norm,
                    "created_at": str(member.get("created_at") or ""),
                }
            )

        out.sort(key=lambda row: (not bool(row["is_default"]), row["list_id"]))
        return out

    async def resolve_task_scope_info(
        self,
        chat_id: str,
        list_id: int | None = None,
        *,
        list_key: str | None = None,
    ) -> dict[str, Any]:
        chat_norm = _normalize_chat_id(chat_id)
        if not chat_norm:
            raise ValueError("Invalid chat id")

        if list_id is None and not list_key:
            memberships = await self.list_task_lists_for_chat(chat_norm)
            if not memberships:
                default_list = await self._ensure_default_task_list(chat_norm)
                return {
                    "chat_id": chat_norm,
                    "list_id": int(default_list["id"]),
                    "list_name": str(default_list.get("name") or "Default"),
                    "list_key": str(default_list.get("list_key") or ""),
                    "scope_chat_id": str(default_list.get("scope_chat_id") or chat_norm),
                    "owner_chat_id": str(default_list.get("owner_chat_id") or chat_norm),
                    "is_default": True,
                    "role": "owner",
                }
            return {
                "chat_id": chat_norm,
                **memberships[0],
            }

        memberships = await self.list_task_lists_for_chat(chat_norm)
        if list_id is not None:
            list_id_int = int(list_id)
            found = next((row for row in memberships if int(row["list_id"]) == list_id_int), None)
        else:
            target_key = _normalize_list_key(list_key)
            found = next((row for row in memberships if _normalize_list_key(str(row.get("list_key") or "")) == target_key), None)
        if not found:
            raise ValueError("List not found for this chat")

        return {
            "chat_id": chat_norm,
            **found,
        }

    async def resolve_task_list_for_chat(self, chat_id: str, selector: str | int) -> dict[str, Any]:
        memberships = await self.list_task_lists_for_chat(chat_id)
        if not memberships:
            raise ValueError("List not found for this chat")

        token = str(selector).strip()
        if token.startswith("#"):
            token = token[1:]

        by_id = _parse_list_id(token)
        if by_id is not None:
            matched = next((row for row in memberships if int(row["list_id"]) == by_id), None)
            if matched:
                return matched

        token_key = _normalize_list_key(token)
        if token_key:
            matched = next(
                (
                    row
                    for row in memberships
                    if _normalize_list_key(str(row.get("list_key") or "")) == token_key
                ),
                None,
            )
            if matched:
                return matched

        token_lower = token.lower()
        if token_lower:
            matched = next((row for row in memberships if str(row.get("list_name", "")).lower() == token_lower), None)
            if matched:
                return matched
            matched = next((row for row in memberships if token_lower in str(row.get("list_name", "")).lower()), None)
            if matched:
                return matched

        raise ValueError("List not found for this chat")

    async def resolve_list_id_any(self, selector: str | int) -> int:
        token = str(selector).strip()
        if token.startswith("#"):
            token = token[1:]

        list_id = _parse_list_id(token)
        if list_id is not None:
            row = await self._get_task_list_by_id(list_id)
            if not row:
                raise ValueError("List not found")
            return int(row["id"])

        row = await self._get_task_list_by_key(token)
        if row:
            return int(row["id"])

        raise ValueError("List not found")

    async def resolve_task_scope(self, chat_id: str) -> str:
        info = await self.resolve_task_scope_info(chat_id)
        return str(info["scope_chat_id"])

    async def set_default_task_list(self, chat_id: str, list_id: int) -> dict[str, Any]:
        list_row = await self._get_task_list_by_id(int(list_id))
        if not list_row:
            raise ValueError("List not found")

        chat_norm = _normalize_chat_id(chat_id)
        memberships = await self.list_task_lists_for_chat(chat_norm)
        if not any(int(row["list_id"]) == int(list_id) for row in memberships):
            await self._upsert_membership(chat_norm, int(list_id), role="member", is_default=True)

        await self._set_default_membership(chat_norm, int(list_id))
        return await self.resolve_task_scope_info(chat_norm, int(list_id))

    async def create_task_list(
        self,
        owner_chat_id: str,
        name: str,
        *,
        list_key: str | None = None,
        make_default_for_owner: bool = False,
    ) -> dict[str, Any]:
        owner = _normalize_chat_id(owner_chat_id)
        if not owner:
            raise ValueError("Invalid owner chat id")

        clean_name = (name or "").strip() or "New List"
        scope_chat_id = self._make_list_scope_chat_id(owner)
        preferred_key = _normalize_list_key(list_key)
        if list_key is not None:
            if not preferred_key:
                raise ValueError("Invalid list key")
            if await self._get_task_list_by_key(preferred_key):
                raise ValueError("list_key already exists")
            assigned_key = preferred_key
        else:
            assigned_key = await self._make_unique_list_key(clean_name)

        rows = await self._request(
            "POST",
            "task_lists",
            json_data={
                "name": clean_name,
                "list_key": assigned_key,
                "owner_chat_id": owner,
                "scope_chat_id": scope_chat_id,
            },
            prefer="return=representation",
        )
        created = rows[0]
        list_id = int(created["id"])

        memberships = await self._list_memberships_for_chat(owner, limit=1000)
        first_list = not memberships
        await self._upsert_membership(owner, list_id, role="owner", is_default=first_list or make_default_for_owner)

        if first_list or make_default_for_owner:
            await self._set_default_membership(owner, list_id)

        return {
            "list_id": list_id,
            "list_name": str(created.get("name") or clean_name),
            "list_key": str(created.get("list_key") or assigned_key),
            "scope_chat_id": str(created.get("scope_chat_id") or scope_chat_id),
            "owner_chat_id": str(created.get("owner_chat_id") or owner),
            "is_default": bool(first_list or make_default_for_owner),
            "role": "owner",
            "member_chat_id": owner,
        }

    async def add_task_list_member(
        self,
        chat_id: str,
        list_id: int,
        *,
        role: str = "member",
        make_default: bool = False,
    ) -> dict[str, Any]:
        chat_norm = _normalize_chat_id(chat_id)
        list_row = await self._get_task_list_by_id(int(list_id))
        if not chat_norm or not list_row:
            raise ValueError("Invalid chat or list")

        clean_role = role if role in {"owner", "member"} else "member"
        await self._upsert_membership(chat_norm, int(list_id), role=clean_role, is_default=make_default)
        if make_default:
            await self._set_default_membership(chat_norm, int(list_id))

        info = await self.resolve_task_scope_info(chat_norm, int(list_id))
        return info

    async def remove_task_list_member(self, chat_id: str, list_id: int) -> bool:
        chat_norm = _normalize_chat_id(chat_id)
        list_id_int = int(list_id)

        memberships = await self._list_memberships_for_chat(chat_norm, limit=1000)
        target = next((row for row in memberships if int(row["list_id"]) == list_id_int), None)
        if not target:
            return False

        if str(target.get("role") or "") == "owner":
            return False

        deleted = await self._request(
            "DELETE",
            "task_list_members",
            params={
                "chat_id": f"eq.{chat_norm}",
                "list_id": f"eq.{list_id_int}",
            },
            prefer="return=representation",
        )

        if not deleted:
            return False

        updated_memberships = await self._list_memberships_for_chat(chat_norm, limit=1000)
        if not any(bool(row.get("is_default")) for row in updated_memberships):
            if updated_memberships:
                await self._set_default_membership(chat_norm, int(updated_memberships[0]["list_id"]))
            else:
                await self._ensure_default_task_list(chat_norm)

        return True

    # ----------------------------
    # Backward compatible binding APIs
    # ----------------------------
    async def list_task_bindings(self) -> list[dict[str, Any]]:
        memberships = await self._request(
            "GET",
            "task_list_members",
            params={
                "select": "chat_id,list_id,role,is_default,created_at",
                "order": "chat_id.asc,is_default.desc,created_at.asc",
                "limit": "2000",
            },
        )
        if not memberships:
            return []

        list_ids = sorted({int(row["list_id"]) for row in memberships})
        id_filter = ",".join(str(x) for x in list_ids)
        lists = await self._request(
            "GET",
            "task_lists",
            params={
                "select": "id,name,list_key,owner_chat_id,scope_chat_id,updated_at",
                "id": f"in.({id_filter})",
                "limit": str(max(200, len(list_ids))),
            },
        )
        by_id = {int(row["id"]): row for row in (lists or [])}

        out: list[dict[str, Any]] = []
        for member in memberships:
            list_id = int(member["list_id"])
            list_row = by_id.get(list_id)
            if not list_row:
                continue
            out.append(
                {
                    "chat_id": str(member.get("chat_id") or ""),
                    "list_id": list_id,
                    "list_name": str(list_row.get("name") or ""),
                    "list_key": str(list_row.get("list_key") or ""),
                    "list_chat_id": str(list_row.get("scope_chat_id") or ""),
                    "owner_chat_id": str(list_row.get("owner_chat_id") or ""),
                    "role": str(member.get("role") or "member"),
                    "is_default": bool(member.get("is_default")),
                    "created_at": str(member.get("created_at") or ""),
                    "updated_at": str(list_row.get("updated_at") or ""),
                }
            )

        return out

    async def upsert_task_binding(self, chat_id: str, list_chat_id: str) -> dict[str, Any]:
        chat_norm = _normalize_chat_id(chat_id)
        if not chat_norm:
            raise ValueError("Invalid chat id")

        target_list_id = _parse_list_id(list_chat_id)
        if target_list_id is None:
            target_list = await self._get_task_list_by_key(list_chat_id)
            if target_list is not None:
                target_list_id = int(target_list["id"])
            else:
                owner_scope = await self.resolve_task_scope_info(list_chat_id)
                target_list_id = int(owner_scope["list_id"])

        info = await self.add_task_list_member(chat_norm, target_list_id, role="member", make_default=True)
        return {
            "chat_id": chat_norm,
            "list_chat_id": str(info["scope_chat_id"]),
            "list_id": int(info["list_id"]),
            "list_name": str(info.get("list_name") or ""),
            "list_key": str(info.get("list_key") or ""),
            "is_default": bool(info.get("is_default")),
        }

    async def remove_task_binding(self, chat_id: str) -> bool:
        chat_norm = _normalize_chat_id(chat_id)
        if not chat_norm:
            return False

        own_default = await self._get_task_list_by_scope(chat_norm)
        if not own_default:
            own_default = await self._ensure_default_task_list(chat_norm)

        await self._set_default_membership(chat_norm, int(own_default["id"]))
        return True

    # ----------------------------
    # Task APIs
    # ----------------------------
    async def create_task(self, task: dict[str, Any]) -> dict[str, Any] | None:
        scope_chat_id = _normalize_chat_id(str(task.get("chat_id") or ""))
        payload = {**task, "chat_id": scope_chat_id}

        list_id = task.get("list_id")
        if list_id in (None, ""):
            list_row = await self._get_task_list_by_scope(scope_chat_id)
            if list_row:
                payload["list_id"] = int(list_row["id"])
        else:
            payload["list_id"] = int(list_id)

        data = await self._request(
            "POST",
            "tasks",
            params={"on_conflict": "source_message_id"},
            json_data=payload,
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

    async def list_open_tasks(
        self,
        chat_id: str,
        limit: int = 100,
        *,
        list_id: int | None = None,
    ) -> list[dict[str, Any]]:
        scope_chat_id = _normalize_chat_id(chat_id)
        params: dict[str, str] = {
            "select": "*",
            "status": "eq.open",
            "order": "due_at.asc.nullslast,priority.desc,created_at.asc",
            "limit": str(limit),
        }
        if list_id is not None:
            params["list_id"] = f"eq.{int(list_id)}"
        else:
            params["chat_id"] = f"eq.{scope_chat_id}"

        data = await self._request("GET", "tasks", params=params)
        return data or []

    async def list_tasks(
        self,
        chat_id: str,
        status: str = "open",
        limit: int = 500,
        *,
        list_id: int | None = None,
    ) -> list[dict[str, Any]]:
        scope_chat_id = _normalize_chat_id(chat_id)
        params: dict[str, str] = {
            "select": "*",
            "order": "status.asc,due_at.asc.nullslast,priority.desc,created_at.asc",
            "limit": str(limit),
        }
        if list_id is not None:
            params["list_id"] = f"eq.{int(list_id)}"
        else:
            params["chat_id"] = f"eq.{scope_chat_id}"

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
        *,
        list_id: int | None = None,
    ) -> list[dict[str, Any]]:
        scope_chat_id = _normalize_chat_id(chat_id)
        params: list[tuple[str, str]] = [
            ("select", "*"),
            ("status", "eq.open"),
            ("due_at", f"gte.{start_utc_iso}"),
            ("due_at", f"lt.{end_utc_iso}"),
            ("order", "priority.desc,due_at.asc,created_at.asc"),
            ("limit", str(limit)),
        ]
        if list_id is not None:
            params.append(("list_id", f"eq.{int(list_id)}"))
        else:
            params.append(("chat_id", f"eq.{scope_chat_id}"))

        data = await self._request("GET", "tasks", params=params)
        return data or []

    async def get_open_task_by_task_no(
        self,
        chat_id: str,
        task_no: int,
        *,
        list_id: int | None = None,
    ) -> dict[str, Any] | None:
        scope_chat_id = _normalize_chat_id(chat_id)
        params: dict[str, str] = {
            "select": "*",
            "task_no": f"eq.{task_no}",
            "status": "eq.open",
            "limit": "1",
        }
        if list_id is not None:
            params["list_id"] = f"eq.{int(list_id)}"
        else:
            params["chat_id"] = f"eq.{scope_chat_id}"

        tasks = await self._request("GET", "tasks", params=params)
        return tasks[0] if tasks else None

    async def mark_done_by_task_no(
        self,
        chat_id: str,
        task_no: int,
        *,
        list_id: int | None = None,
    ) -> dict[str, Any] | None:
        scope_chat_id = _normalize_chat_id(chat_id)
        payload = {
            "status": "done",
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        params: dict[str, str] = {
            "task_no": f"eq.{task_no}",
            "status": "eq.open",
        }
        if list_id is not None:
            params["list_id"] = f"eq.{int(list_id)}"
        else:
            params["chat_id"] = f"eq.{scope_chat_id}"

        updated = await self._request(
            "PATCH",
            "tasks",
            params=params,
            json_data=payload,
            prefer="return=representation",
        )
        return updated[0] if updated else None

    async def update_task_by_task_no(
        self,
        chat_id: str,
        task_no: int,
        patch: dict[str, Any],
        *,
        list_id: int | None = None,
    ) -> dict[str, Any] | None:
        scope_chat_id = _normalize_chat_id(chat_id)
        params: dict[str, str] = {
            "task_no": f"eq.{task_no}",
            "status": "eq.open",
        }
        if list_id is not None:
            params["list_id"] = f"eq.{int(list_id)}"
        else:
            params["chat_id"] = f"eq.{scope_chat_id}"

        updated = await self._request(
            "PATCH",
            "tasks",
            params=params,
            json_data=patch,
            prefer="return=representation",
        )
        return updated[0] if updated else None

    async def delete_task_by_task_no(
        self,
        chat_id: str,
        task_no: int,
        *,
        list_id: int | None = None,
    ) -> dict[str, Any] | None:
        scope_chat_id = _normalize_chat_id(chat_id)
        params: dict[str, str] = {
            "task_no": f"eq.{task_no}",
        }
        if list_id is not None:
            params["list_id"] = f"eq.{int(list_id)}"
        else:
            params["chat_id"] = f"eq.{scope_chat_id}"

        deleted = await self._request(
            "DELETE",
            "tasks",
            params=params,
            prefer="return=representation",
        )
        return deleted[0] if deleted else None

    async def list_active_chat_ids(self) -> list[str]:
        memberships = await self._request(
            "GET",
            "task_list_members",
            params={
                "select": "chat_id",
                "is_default": "eq.true",
                "limit": "2000",
            },
        )
        profiles = await self._request(
            "GET",
            "user_profiles",
            params={
                "select": "chat_id",
                "limit": "2000",
            },
        )

        chat_ids = {str(row.get("chat_id") or "") for row in (memberships or [])}
        chat_ids.update({str(row.get("chat_id") or "") for row in (profiles or [])})
        chat_ids.discard("")
        return sorted(chat_ids)

    async def get_user_profile(self, chat_id: str) -> dict[str, Any]:
        chat_norm = _normalize_chat_id(chat_id)
        profile = await self._request(
            "GET",
            "user_profiles",
            params={
                "select": "*",
                "chat_id": f"eq.{chat_norm}",
                "limit": "1",
            },
        )

        if profile:
            return profile[0]

        return {
            "chat_id": chat_norm,
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
        chat_norm = _normalize_chat_id(chat_id)
        await self._request(
            "POST",
            "daily_plans",
            json_data={
                "chat_id": chat_norm,
                "plan_date": plan_date_iso,
                "ordered_task_ids": ordered_task_ids,
                "rationale": rationale,
            },
            prefer="return=minimal",
        )

    # ----------------------------
    # Whitelist APIs
    # ----------------------------
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

    # ----------------------------
    # Admin login store APIs
    # ----------------------------
    async def get_admin_user_by_email(self, email: str) -> dict[str, Any] | None:
        normalized = (email or "").strip().lower()
        if not normalized:
            return None

        rows = await self._request(
            "GET",
            "admin_users",
            params={
                "select": "id,email,display_name,password_hash,status",
                "email": f"eq.{normalized}",
                "status": "eq.active",
                "limit": "1",
            },
        )
        return rows[0] if rows else None

    async def get_admin_user_by_id(self, user_id: int) -> dict[str, Any] | None:
        rows = await self._request(
            "GET",
            "admin_users",
            params={
                "select": "id,email,display_name,status",
                "id": f"eq.{int(user_id)}",
                "status": "eq.active",
                "limit": "1",
            },
        )
        return rows[0] if rows else None

    async def touch_admin_login(self, user_id: int) -> None:
        await self._request(
            "PATCH",
            "admin_users",
            params={
                "id": f"eq.{int(user_id)}",
            },
            json_data={
                "last_login_at": datetime.now(timezone.utc).isoformat(),
            },
            prefer="return=minimal",
        )

    async def admin_user_store_health_check(self) -> dict[str, Any]:
        try:
            await self._request(
                "GET",
                "admin_users",
                params={
                    "select": "id",
                    "limit": "1",
                },
            )
            return {"ok": True}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    # ----------------------------
    # Health check
    # ----------------------------
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
