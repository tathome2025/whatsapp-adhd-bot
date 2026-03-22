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

    async def handle_message(self, chat_id: str, text: str, source_message_id: str | None = None) -> str:
        command_reply = await self._dispatch_command(chat_id, text)
        if command_reply is not None:
            return command_reply

        profile = await self.repo.get_user_profile(chat_id)
        timezone_name = profile.get("timezone") or self.settings.timezone

        parsed = parse_task_text(text, timezone_name)
        if not parsed.title:
            return "我未能解析任務內容，請用例如：下星期二 3pm 同客開會。"

        created = await self.repo.create_task(
            {
                "chat_id": chat_id,
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
        return (
            f"已加入任務 #{created['id']}\n"
            f"內容：{created['title']}\n"
            f"時間：{due_label}\n"
            f"優先度：{priority_label}"
        )

    async def push_daily_plans(self) -> dict[str, int]:
        pushed = 0
        skipped = 0

        for chat_id in await self.repo.list_active_chat_ids():
            profile = await self.repo.get_user_profile(chat_id)
            timezone_name = profile.get("timezone") or self.settings.timezone
            today_tasks = await self._get_today_tasks(chat_id, timezone_name)
            local_tz = ZoneInfo(timezone_name)
            local_today = datetime.now(local_tz).date().isoformat()

            if not today_tasks:
                skipped += 1
                continue

            plan = await self.planner.rank_tasks_for_adhd(chat_id, today_tasks, profile)
            limited_tasks = plan["ordered_tasks"][: int(profile.get("max_daily_tasks") or self.settings.max_daily_tasks)]

            message = self._format_today_message(limited_tasks, timezone_name, plan.get("reasons", []), push_mode=True)
            await self.whatsapp_client.send_text_message(chat_id, message)

            await self.repo.save_daily_plan(
                chat_id=chat_id,
                plan_date_iso=local_today,
                ordered_task_ids=[int(t["id"]) for t in limited_tasks],
                rationale={
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

        if normalized in {"list", "/list"}:
            return await self._cmd_list(chat_id)

        if normalized in {"today", "/today"}:
            return await self._cmd_today(chat_id)

        if normalized in {"help", "/help"}:
            return self._cmd_help()

        done_match = re.match(r"^/?done\b(.*)$", raw, flags=re.IGNORECASE)
        if done_match:
            task_ids = _parse_task_ids(done_match.group(1))
            if not task_ids:
                return "請提供任務 ID，例如：done 3 或 done 3 5 8。"
            if len(task_ids) > 30:
                return "一次最多可完成 30 項任務，請分批執行。"
            return await self._cmd_done_many(chat_id, task_ids)

        return None

    async def _cmd_list(self, chat_id: str) -> str:
        profile = await self.repo.get_user_profile(chat_id)
        timezone_name = profile.get("timezone") or self.settings.timezone

        tasks = await self.repo.list_open_tasks(chat_id)
        if not tasks:
            return "目前沒有待辦任務。"

        lines = ["待辦清單："]
        for task in tasks[:30]:
            due_label = _format_due(task.get("due_at"), timezone_name)
            priority = PRIORITY_LABELS.get(int(task.get("priority", 2)), "中")
            lines.append(f"#{task['id']} [{priority}] {task['title']}｜{due_label}")

        if len(tasks) > 30:
            lines.append(f"... 另有 {len(tasks) - 30} 項")

        return "\n".join(lines)

    async def _cmd_today(self, chat_id: str) -> str:
        profile = await self.repo.get_user_profile(chat_id)
        timezone_name = profile.get("timezone") or self.settings.timezone

        today_tasks = await self._get_today_tasks(chat_id, timezone_name)
        if not today_tasks:
            return "今天沒有排程任務。"

        plan = await self.planner.rank_tasks_for_adhd(chat_id, today_tasks, profile)
        max_daily = int(profile.get("max_daily_tasks") or self.settings.max_daily_tasks)
        limited_tasks = plan["ordered_tasks"][:max_daily]

        return self._format_today_message(limited_tasks, timezone_name, plan.get("reasons", []), push_mode=False)

    async def _cmd_done_many(self, chat_id: str, task_ids: list[int]) -> str:
        completed: list[tuple[int, str]] = []
        not_found: list[int] = []

        for task_id in task_ids:
            updated = await self.repo.mark_done(chat_id, task_id)
            if not updated:
                not_found.append(task_id)
                continue
            completed.append((task_id, str(updated.get("title") or "")))

        if not completed and len(task_ids) == 1:
            return f"找不到可完成的任務 #{task_ids[0]}。"

        lines: list[str] = []

        if completed:
            lines.append(f"已完成 {len(completed)} 項任務：")
            for task_id, title in completed[:20]:
                lines.append(f"- #{task_id} {title}")
            if len(completed) > 20:
                lines.append(f"... 另有 {len(completed) - 20} 項已完成")

        if not_found:
            missed = ", ".join(f"#{task_id}" for task_id in not_found)
            lines.append(f"未找到或已完成：{missed}")

        return "\n".join(lines) if lines else "找不到可完成的任務。"

    def _cmd_help(self) -> str:
        return (
            "可用指令：\n"
            "list - 查看全部待辦\n"
            "today - 查看今日任務\n"
            "done <id ...> - 完成一個或多個任務（例：done 3 5 8）\n"
            "\n自然語言例子：下星期二 3pm 同客開會"
        )

    async def _get_today_tasks(self, chat_id: str, timezone_name: str) -> list[dict[str, Any]]:
        local_tz = ZoneInfo(timezone_name)
        now_local = datetime.now(local_tz)
        start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        end_local = start_local + timedelta(days=1)

        start_utc = start_local.astimezone(timezone.utc).isoformat()
        end_utc = end_local.astimezone(timezone.utc).isoformat()

        return await self.repo.list_tasks_for_date(chat_id, start_utc, end_utc)

    def _format_today_message(
        self,
        tasks: list[dict[str, Any]],
        timezone_name: str,
        reasons: list[str],
        *,
        push_mode: bool,
    ) -> str:
        title = "今日工作清單" if push_mode else "今天建議執行順序"
        lines = [f"{title}："]

        for index, task in enumerate(tasks, start=1):
            due_label = _format_due(task.get("due_at"), timezone_name)
            priority = PRIORITY_LABELS.get(int(task.get("priority", 2)), "中")
            lines.append(f"{index}. #{task['id']} [{priority}] {task['title']}｜{due_label}")

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


def _format_due(due_at_iso: str | None, timezone_name: str) -> str:
    if not due_at_iso:
        return "未排程"

    dt = datetime.fromisoformat(due_at_iso)
    local_dt = dt.astimezone(ZoneInfo(timezone_name))
    return local_dt.strftime("%m/%d %H:%M")
