from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from app.config import Settings
from app.openai_planner import OpenAIPlanner
from app.parser import parse_task_text
from app.supabase_repo import SupabaseRepo
from app.whatsapp import WhatsAppClient

PRIORITY_LABELS = {
    3: "高",
    2: "中",
    1: "低",
}


class TaskService:
    def __init__(
        self,
        settings: Settings,
        repo: SupabaseRepo,
        whatsapp_client: WhatsAppClient,
        planner: OpenAIPlanner,
    ):
        self.settings = settings
        self.repo = repo
        self.whatsapp_client = whatsapp_client
        self.planner = planner

    async def handle_message(
        self,
        chat_id: str,
        text: str,
        source_message_id: str | None = None,
        sender_id: str | None = None,
        is_group: bool = False,
    ) -> str | None:
        _ = is_group
        sender = sender_id or chat_id
        if not await self.repo.is_whitelisted_sender(sender):
            return None

        command_reply = await self._dispatch_command(chat_id, text)
        if command_reply is not None:
            return command_reply

        scope = await self.repo.resolve_task_scope_info(chat_id)
        profile = await self.repo.get_user_profile(chat_id)
        timezone_name = profile.get("timezone") or self.settings.timezone

        parsed = parse_task_text(text, timezone_name)
        if not parsed.title:
            return "我未能解析任務內容，請用例如：下星期二 3pm 同客開會。"

        created = await self.repo.create_task(
            {
                "chat_id": scope["scope_chat_id"],
                "list_id": int(scope["list_id"]),
                "title": parsed.title,
                "due_at": parsed.due_at_utc,
                "priority": parsed.priority,
                "status": "open",
                "effort_min": parsed.effort_min,
                "energy_need": parsed.energy_need,
                "source_text": text,
                "source_message_id": source_message_id,
            }
        )

        if not created:
            return "這則訊息已處理過，無需重複加入。"

        due_label = _format_due(created.get("due_at"), timezone_name)
        priority_label = PRIORITY_LABELS.get(int(created.get("priority", 2)), "中")
        display_no = _task_no(created)

        lines = [
            f"已加入任務 #{display_no}",
            f"清單：{_format_list_ref(scope)}",
            f"內容：{created['title']}",
            f"時間：{due_label}",
            f"優先度：{priority_label}",
        ]

        order_hint = await self._build_new_task_order_hint(
            scope_chat_id=str(scope["scope_chat_id"]),
            planner_context=f"list:{scope['list_id']}",
            created_task=created,
            profile=profile,
        )
        if order_hint:
            lines.append(order_hint)

        return "\n".join(lines)

    async def push_daily_plans(self) -> dict[str, int]:
        pushed = 0
        skipped = 0

        for recipient_chat_id in await self.repo.list_active_chat_ids():
            try:
                scope = await self.repo.resolve_task_scope_info(recipient_chat_id)
            except Exception:  # noqa: BLE001
                skipped += 1
                continue

            profile = await self.repo.get_user_profile(recipient_chat_id)
            timezone_name = profile.get("timezone") or self.settings.timezone
            today_tasks = await self._get_today_tasks(
                scope_chat_id=str(scope["scope_chat_id"]),
                timezone_name=timezone_name,
                list_id=int(scope["list_id"]),
            )
            local_tz = ZoneInfo(timezone_name)
            local_today = datetime.now(local_tz).date().isoformat()

            if not today_tasks:
                skipped += 1
                continue

            plan = await self.planner.rank_tasks_for_adhd(
                f"list:{scope['list_id']}:chat:{recipient_chat_id}",
                today_tasks,
                profile,
            )
            limited_tasks = plan["ordered_tasks"][: int(profile.get("max_daily_tasks") or self.settings.max_daily_tasks)]

            message = self._format_today_message(
                tasks=limited_tasks,
                timezone_name=timezone_name,
                reasons=plan.get("reasons", []),
                push_mode=True,
                ai_sorted=not bool(plan.get("fallback", False)),
                list_name=str(scope.get("list_name") or ""),
                list_id=int(scope["list_id"]),
                list_key=str(scope.get("list_key") or ""),
            )
            await self.whatsapp_client.send_text_message(recipient_chat_id, message)

            await self.repo.save_daily_plan(
                chat_id=recipient_chat_id,
                plan_date_iso=local_today,
                ordered_task_ids=[int(t["id"]) for t in limited_tasks],
                rationale={
                    "list_id": int(scope["list_id"]),
                    "list_name": str(scope.get("list_name") or ""),
                    "reasons": plan.get("reasons", []),
                    "suggested_time_blocks": plan.get("suggested_time_blocks", []),
                    "fallback": plan.get("fallback", False),
                },
            )
            pushed += 1

        return {"pushed": pushed, "skipped": skipped}

    async def _dispatch_command(self, chat_id: str, text: str) -> str | None:
        raw = text.strip()
        normalized = raw.lower()

        if normalized in {"help", "/help"}:
            return self._cmd_help()

        if normalized in {"lists", "/lists"}:
            return await self._cmd_lists(chat_id)

        newlist_match = re.match(r"^/?(?:newlist|createlist|mklist)\s+(.+)$", raw, flags=re.IGNORECASE | re.DOTALL)
        if newlist_match:
            return await self._cmd_newlist(chat_id, newlist_match.group(1).strip())

        use_match = re.match(r"^/?use\s+(.+)$", raw, flags=re.IGNORECASE | re.DOTALL)
        if use_match:
            return await self._cmd_use(chat_id, use_match.group(1).strip())

        share_match = re.match(r"^/?share\s+(\S+)\s+(\S+)$", raw, flags=re.IGNORECASE)
        if share_match:
            return await self._cmd_share(chat_id, share_match.group(1), share_match.group(2))

        unshare_match = re.match(r"^/?unshare\s+(\S+)\s+(\S+)$", raw, flags=re.IGNORECASE)
        if unshare_match:
            return await self._cmd_unshare(chat_id, unshare_match.group(1), unshare_match.group(2))

        scope = await self.repo.resolve_task_scope_info(chat_id)

        if normalized in {"list", "/list"}:
            return await self._cmd_list(chat_id, scope)

        if normalized in {"today", "/today"}:
            return await self._cmd_today(chat_id, scope)

        edit_match = re.match(r"^/?edit\s+(\d+)\s+(.+)$", raw, flags=re.IGNORECASE | re.DOTALL)
        if edit_match:
            return await self._cmd_edit(chat_id, scope, int(edit_match.group(1)), edit_match.group(2).strip())

        if re.match(r"^/?edit\b", raw, flags=re.IGNORECASE):
            return "請用：edit <id> <新內容>，例如：edit 3 明天 4pm 跟客開會。"

        delete_match = re.match(r"^/?(?:delete|del|remove)\b(.*)$", raw, flags=re.IGNORECASE)
        if delete_match:
            task_ids = _parse_task_ids(delete_match.group(1))
            if not task_ids:
                return "請提供任務 ID，例如：delete 3 或 delete 3 5 8。"
            if len(task_ids) > 30:
                return "一次最多可刪除 30 項任務，請分批執行。"
            return await self._cmd_delete_many(scope, task_ids)

        done_match = re.match(r"^/?done\b(.*)$", raw, flags=re.IGNORECASE)
        if done_match:
            task_ids = _parse_task_ids(done_match.group(1))
            if not task_ids:
                return "請提供任務 ID，例如：done 3 或 done 3 5 8。"
            if len(task_ids) > 30:
                return "一次最多可完成 30 項任務，請分批執行。"
            return await self._cmd_done_many(scope, task_ids)

        return None

    async def _cmd_lists(self, chat_id: str) -> str:
        lists = await self.repo.list_task_lists_for_chat(chat_id)
        if not lists:
            return "你目前沒有可用清單。"

        lines = ["你的 Task Lists："]
        for item in lists[:30]:
            marker = "*" if bool(item.get("is_default")) else " "
            role = str(item.get("role") or "member")
            key = str(item.get("list_key") or "")
            key_label = f" key:{key}" if key else ""
            lines.append(f"{marker} #{item['list_id']}{key_label} [{role}] {item.get('list_name','')} ")

        lines.append("\n用法：use <list_id或list_key> 切換預設清單")
        return "\n".join(lines)

    async def _cmd_newlist(self, chat_id: str, name: str) -> str:
        created = await self.repo.create_task_list(chat_id, name, make_default_for_owner=True)
        key = str(created.get("list_key") or "")
        key_line = f"識別：{key}\n" if key else ""
        return (
            f"已建立清單 #{created['list_id']}\n"
            f"{key_line}"
            f"名稱：{created.get('list_name','')}\n"
            "已設為目前預設清單。"
        )

    async def _cmd_use(self, chat_id: str, target: str) -> str:
        try:
            chosen = await self.repo.resolve_task_list_for_chat(chat_id, target)
        except ValueError:
            return "找不到該清單，請先用 lists 查看可用清單。"

        applied = await self.repo.set_default_task_list(chat_id, int(chosen["list_id"]))
        return f"已切換至清單 {_format_list_ref(applied)}"

    async def _cmd_share(self, chat_id: str, list_token: str, member_chat_id: str) -> str:
        try:
            chosen = await self.repo.resolve_task_list_for_chat(chat_id, list_token)
        except ValueError:
            return "請提供有效清單，例如：share 12 85291234567 或 share personal 85291234567"

        list_id = int(chosen["list_id"])
        await self.repo.add_task_list_member(member_chat_id, list_id, role="member", make_default=False)
        return f"已分享清單 {_format_list_ref(chosen)} 給 {member_chat_id}。"

    async def _cmd_unshare(self, chat_id: str, list_token: str, member_chat_id: str) -> str:
        try:
            chosen = await self.repo.resolve_task_list_for_chat(chat_id, list_token)
        except ValueError:
            return "請提供有效清單，例如：unshare 12 85291234567 或 unshare personal 85291234567"

        list_id = int(chosen["list_id"])
        removed = await self.repo.remove_task_list_member(member_chat_id, list_id)
        if not removed:
            return f"未找到 {member_chat_id} 在清單 {_format_list_ref(chosen)} 的分享紀錄。"
        return f"已取消清單 {_format_list_ref(chosen)} 對 {member_chat_id} 的分享。"

    async def _cmd_list(self, chat_id: str, scope: dict[str, Any]) -> str:
        profile = await self.repo.get_user_profile(chat_id)
        timezone_name = profile.get("timezone") or self.settings.timezone

        tasks = await self.repo.list_open_tasks(
            str(scope["scope_chat_id"]),
            list_id=int(scope["list_id"]),
        )
        if not tasks:
            return f"清單 {_format_list_ref(scope)} 目前沒有待辦任務。"

        lines = [f"待辦清單 {_format_list_ref(scope)}："]
        for task in tasks[:30]:
            due_label = _format_due(task.get("due_at"), timezone_name)
            priority = PRIORITY_LABELS.get(int(task.get("priority", 2)), "中")
            lines.append(f"#{_task_no(task)} [{priority}] {task['title']}｜{due_label}")

        if len(tasks) > 30:
            lines.append(f"... 另有 {len(tasks) - 30} 項")

        return "\n".join(lines)

    async def _cmd_today(self, chat_id: str, scope: dict[str, Any]) -> str:
        profile = await self.repo.get_user_profile(chat_id)
        timezone_name = profile.get("timezone") or self.settings.timezone

        today_tasks = await self._get_today_tasks(
            scope_chat_id=str(scope["scope_chat_id"]),
            timezone_name=timezone_name,
            list_id=int(scope["list_id"]),
        )
        if not today_tasks:
            return f"清單 {_format_list_ref(scope)} 今天沒有排程任務。"

        plan = await self.planner.rank_tasks_for_adhd(
            f"list:{scope['list_id']}:chat:{chat_id}",
            today_tasks,
            profile,
        )
        max_daily = int(profile.get("max_daily_tasks") or self.settings.max_daily_tasks)
        limited_tasks = plan["ordered_tasks"][:max_daily]

        return self._format_today_message(
            tasks=limited_tasks,
            timezone_name=timezone_name,
            reasons=plan.get("reasons", []),
            push_mode=False,
            ai_sorted=not bool(plan.get("fallback", False)),
            list_name=str(scope.get("list_name") or ""),
            list_id=int(scope["list_id"]),
            list_key=str(scope.get("list_key") or ""),
        )

    async def _cmd_done_many(self, scope: dict[str, Any], task_ids: list[int]) -> str:
        completed: list[tuple[int, str]] = []
        not_found: list[int] = []

        for task_no in task_ids:
            updated = await self.repo.mark_done_by_task_no(
                str(scope["scope_chat_id"]),
                task_no,
                list_id=int(scope["list_id"]),
            )
            if not updated:
                not_found.append(task_no)
                continue
            completed.append((task_no, str(updated.get("title") or "")))

        if not completed and len(task_ids) == 1:
            return f"找不到可完成的任務 #{task_ids[0]}。"

        lines: list[str] = []

        if completed:
            lines.append(f"已完成 {len(completed)} 項任務：")
            for task_no, title in completed[:20]:
                lines.append(f"- #{task_no} {title}")
            if len(completed) > 20:
                lines.append(f"... 另有 {len(completed) - 20} 項已完成")

        if not_found:
            missed = ", ".join(f"#{task_no}" for task_no in not_found)
            lines.append(f"未找到或已完成：{missed}")

        return "\n".join(lines) if lines else "找不到可完成的任務。"

    async def _cmd_delete_many(self, scope: dict[str, Any], task_ids: list[int]) -> str:
        deleted: list[tuple[int, str]] = []
        not_found: list[int] = []

        for task_no in task_ids:
            removed = await self.repo.delete_task_by_task_no(
                str(scope["scope_chat_id"]),
                task_no,
                list_id=int(scope["list_id"]),
            )
            if not removed:
                not_found.append(task_no)
                continue
            deleted.append((task_no, str(removed.get("title") or "")))

        if not deleted and len(task_ids) == 1:
            return f"找不到可刪除的任務 #{task_ids[0]}。"

        lines: list[str] = []
        if deleted:
            lines.append(f"已刪除 {len(deleted)} 項任務：")
            for task_no, title in deleted[:20]:
                lines.append(f"- #{task_no} {title}")
            if len(deleted) > 20:
                lines.append(f"... 另有 {len(deleted) - 20} 項已刪除")

        if not_found:
            missed = ", ".join(f"#{task_no}" for task_no in not_found)
            lines.append(f"未找到任務：{missed}")

        return "\n".join(lines) if lines else "找不到可刪除的任務。"

    async def _cmd_edit(self, chat_id: str, scope: dict[str, Any], task_no: int, new_text: str) -> str:
        profile = await self.repo.get_user_profile(chat_id)
        timezone_name = profile.get("timezone") or self.settings.timezone

        existing = await self.repo.get_open_task_by_task_no(
            str(scope["scope_chat_id"]),
            task_no,
            list_id=int(scope["list_id"]),
        )
        if not existing:
            return f"找不到可編輯的任務 #{task_no}。"

        parsed = parse_task_text(new_text, timezone_name)
        if not parsed.title:
            return "我未能解析新內容，請用例如：edit 3 明天 4pm 跟客開會。"

        patch = {
            "title": parsed.title,
            "due_at": parsed.due_at_utc if parsed.due_at_utc is not None else existing.get("due_at"),
            "priority": parsed.priority,
            "effort_min": parsed.effort_min,
            "energy_need": parsed.energy_need,
            "source_text": new_text,
        }

        updated = await self.repo.update_task_by_task_no(
            str(scope["scope_chat_id"]),
            task_no,
            patch,
            list_id=int(scope["list_id"]),
        )
        if not updated:
            return f"找不到可編輯的任務 #{task_no}。"

        due_label = _format_due(updated.get("due_at"), timezone_name)
        priority_label = PRIORITY_LABELS.get(int(updated.get("priority", 2)), "中")
        return (
            f"已更新任務 #{_task_no(updated)}\n"
            f"清單：{_format_list_ref(scope)}\n"
            f"內容：{updated['title']}\n"
            f"時間：{due_label}\n"
            f"優先度：{priority_label}"
        )

    def _cmd_help(self) -> str:
        return (
            "可用指令：\n"
            "list - 查看目前清單待辦\n"
            "today - 查看目前清單今日任務\n"
            "lists - 查看你可用的所有清單\n"
            "newlist <名稱> - 建立新清單並切換\n"
            "use <list_id/list_key/名稱> - 切換目前清單\n"
            "share <list_id或list_key> <電話> - 分享清單給其他號碼\n"
            "unshare <list_id或list_key> <電話> - 取消分享\n"
            "done <id ...> - 完成一個或多個任務（例：done 3 5 8）\n"
            "delete <id ...> - 刪除任務（例：delete 3 5）\n"
            "edit <id> <新內容> - 更新任務（例：edit 3 明天 4pm 跟客開會）\n"
            "\n自然語言例子：下星期二 3pm 同客開會"
        )

    async def _build_new_task_order_hint(
        self,
        *,
        scope_chat_id: str,
        planner_context: str,
        created_task: dict[str, Any],
        profile: dict[str, Any],
    ) -> str:
        try:
            open_tasks = await self.repo.list_open_tasks(scope_chat_id, limit=100)
            if not open_tasks:
                return ""

            plan = await self.planner.rank_tasks_for_adhd(planner_context, open_tasks, profile)
            ordered_tasks = plan.get("ordered_tasks") or open_tasks
            ai_sorted = not bool(plan.get("fallback", False))
            method_label = "由AI排序" if ai_sorted else "由規則排序"

            created_ref = _task_no(created_task)
            created_position: int | None = None
            for index, task in enumerate(ordered_tasks, start=1):
                if _task_no(task) == created_ref:
                    created_position = index
                    break

            lines: list[str] = []
            if created_position is not None:
                lines.append(f"建議次序：第 {created_position} 位（{method_label}）")
            else:
                lines.append(f"已納入建議清單（{method_label}）")

            preview = self._format_top_tasks_preview(ordered_tasks)
            if preview:
                lines.append(preview)

            return "\n".join(lines)
        except Exception:  # noqa: BLE001
            return ""

    @staticmethod
    def _format_top_tasks_preview(tasks: list[dict[str, Any]]) -> str:
        if not tasks:
            return ""

        lines = ["目前建議前 3 項："]
        for index, task in enumerate(tasks[:3], start=1):
            lines.append(f"{index}. #{_task_no(task)} {task['title']}")
        return "\n".join(lines)

    async def _get_today_tasks(self, scope_chat_id: str, timezone_name: str, list_id: int) -> list[dict[str, Any]]:
        local_tz = ZoneInfo(timezone_name)
        now_local = datetime.now(local_tz)
        start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        end_local = start_local + timedelta(days=1)

        start_utc = start_local.astimezone(timezone.utc).isoformat()
        end_utc = end_local.astimezone(timezone.utc).isoformat()

        return await self.repo.list_tasks_for_date(scope_chat_id, start_utc, end_utc, list_id=list_id)

    def _format_today_message(
        self,
        tasks: list[dict[str, Any]],
        timezone_name: str,
        reasons: list[str],
        *,
        push_mode: bool,
        ai_sorted: bool,
        list_name: str,
        list_id: int,
        list_key: str = "",
    ) -> str:
        title = "今日工作清單" if push_mode else "今天建議執行順序"
        key_part = f", key:{list_key}" if list_key else ""
        lines = [f"{title}：", f"清單：{list_name} (#{list_id}{key_part})"]
        if ai_sorted:
            lines.append("（由AI排序）")

        for index, task in enumerate(tasks, start=1):
            due_label = _format_due(task.get("due_at"), timezone_name)
            priority = PRIORITY_LABELS.get(int(task.get("priority", 2)), "中")
            lines.append(f"{index}. #{_task_no(task)} [{priority}] {task['title']}｜{due_label}")

        if reasons:
            lines.append("\n排序理由：")
            for reason in reasons[:3]:
                lines.append(f"- {reason}")

        return "\n".join(lines)


def _parse_task_ids(raw_text: str) -> list[int]:
    ids: list[int] = []
    for token in re.findall(r"\d+", raw_text):
        task_id = int(token)
        if task_id <= 0:
            continue
        if task_id not in ids:
            ids.append(task_id)
    return ids


def _format_list_ref(scope: dict[str, Any]) -> str:
    list_name = str(scope.get("list_name") or "")
    list_id = int(scope.get("list_id") or 0)
    list_key = str(scope.get("list_key") or "")
    key_part = f", key:{list_key}" if list_key else ""
    return f"{list_name} (#{list_id}{key_part})"


def _task_no(task: dict[str, Any]) -> int:
    raw = task.get("task_no")
    if raw not in (None, ""):
        return int(raw)
    return int(task.get("id") or 0)


def _format_due(due_at_iso: str | None, timezone_name: str) -> str:
    if not due_at_iso:
        return "未排程"

    dt = datetime.fromisoformat(due_at_iso)
    local_dt = dt.astimezone(ZoneInfo(timezone_name))
    return local_dt.strftime("%m/%d %H:%M")
