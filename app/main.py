from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from datetime import datetime, timezone
from html import escape
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel, Field

from app.config import get_settings
from app.openai_planner import OpenAIPlanner
from app.parser import parse_task_text
from app.services import TaskService
from app.supabase_repo import SupabaseRepo
from app.whatsapp import WhatsAppClient, extract_inbound_messages

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

settings = get_settings()
repo = SupabaseRepo(settings)
whatsapp_client = WhatsAppClient(settings)
planner = OpenAIPlanner(settings)
service = TaskService(settings, repo, whatsapp_client, planner)

app = FastAPI(title="WhatsApp ADHD Task Bot", version="0.1.0")

WEBHOOK_RUNTIME: dict[str, object] = {
    "last_attempt_utc": "",
    "last_status": "none",
    "last_error": "",
    "last_messages_count": 0,
    "last_processed": 0,
    "last_ignored": 0,
}


class AdminWhitelistUpsert(BaseModel):
    sender_id: str
    label: str = ""


class AdminBindingUpsert(BaseModel):
    chat_id: str
    list_chat_id: str


class AdminBatchAddRequest(BaseModel):
    chat_id: str
    lines: list[str] = Field(default_factory=list)


class AdminBatchEditItem(BaseModel):
    task_no: int
    text: str


class AdminBatchEditRequest(BaseModel):
    chat_id: str
    items: list[AdminBatchEditItem] = Field(default_factory=list)


class AdminBatchDeleteRequest(BaseModel):
    chat_id: str
    task_nos: list[int] = Field(default_factory=list)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/privacy", response_class=HTMLResponse)
async def privacy_policy() -> str:
    contact_email = escape(os.getenv("PRIVACY_CONTACT_EMAIL", "support@example.com"))
    content = f"""
    <h2>1. What We Collect</h2>
    <ul>
      <li>WhatsApp phone number and message metadata</li>
      <li>Task content you send to the bot (title, due date/time, priority)</li>
      <li>Operational logs required for reliability and debugging</li>
    </ul>

    <h2>2. Why We Collect It</h2>
    <ul>
      <li>Create and manage your to-do tasks</li>
      <li>Send reminders and daily schedules</li>
      <li>Improve prioritization quality and service reliability</li>
    </ul>

    <h2>3. Third-Party Processors</h2>
    <ul>
      <li>Meta WhatsApp Cloud API (message delivery)</li>
      <li>Supabase (data storage)</li>
      <li>OpenAI API (task ordering assistance)</li>
    </ul>

    <h2>4. Data Retention</h2>
    <p>Data is retained only as long as needed to provide the service, unless a longer period is required by law.</p>

    <h2>5. Data Deletion</h2>
    <p>You can request deletion at any time. See <a href="/data-deletion">Data Deletion Instructions</a>.</p>

    <h2>6. Contact</h2>
    <p>For privacy requests, contact: <a href="mailto:{contact_email}">{contact_email}</a></p>

    <h2>7. Effective Date</h2>
    <p>2026-03-23</p>
    """
    return _render_legal_page("Privacy Policy", content)


@app.get("/data-deletion", response_class=HTMLResponse)
async def data_deletion_instructions() -> str:
    contact_email = escape(os.getenv("PRIVACY_CONTACT_EMAIL", "support@example.com"))
    content = f"""
    <h2>How to Request Data Deletion</h2>
    <ol>
      <li>Send a WhatsApp message to this bot with: <code>delete my data</code>, or</li>
      <li>Email <a href="mailto:{contact_email}">{contact_email}</a> from your registered contact and include your WhatsApp number.</li>
    </ol>

    <h2>What Will Be Deleted</h2>
    <ul>
      <li>Task records linked to your WhatsApp number</li>
      <li>User profile settings used for scheduling</li>
      <li>Stored daily plan outputs</li>
    </ul>

    <h2>Processing Time</h2>
    <p>Requests are typically processed within 7 business days.</p>

    <h2>Effective Date</h2>
    <p>2026-03-23</p>
    """
    return _render_legal_page("Data Deletion Instructions", content)


@app.get("/status.json")
async def status_json() -> dict[str, object]:
    return await _build_status_snapshot()


@app.get("/status", response_class=HTMLResponse)
async def status_page() -> str:
    snapshot = await _build_status_snapshot()
    return _render_status_html(snapshot)


@app.get("/admin", response_class=HTMLResponse)
async def admin_page() -> str:
    return _render_admin_html()


@app.get("/admin/api/whitelist")
async def admin_list_whitelist(request: Request) -> dict[str, list[dict[str, Any]]]:
    _assert_admin_auth(request)
    items = await repo.list_whitelist_contacts()
    return {"items": items}


@app.post("/admin/api/whitelist")
async def admin_upsert_whitelist(request: Request, body: AdminWhitelistUpsert) -> dict[str, Any]:
    _assert_admin_auth(request)
    try:
        item = await repo.upsert_whitelist_contact(body.sender_id, body.label)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"item": item}


@app.delete("/admin/api/whitelist/{sender_id}")
async def admin_delete_whitelist(request: Request, sender_id: str) -> dict[str, bool]:
    _assert_admin_auth(request)
    deleted = await repo.remove_whitelist_contact(sender_id)
    return {"deleted": deleted}


@app.get("/admin/api/bindings")
async def admin_list_bindings(request: Request) -> dict[str, list[dict[str, Any]]]:
    _assert_admin_auth(request)
    items = await repo.list_task_bindings()
    return {"items": items}


@app.post("/admin/api/bindings")
async def admin_upsert_binding(request: Request, body: AdminBindingUpsert) -> dict[str, Any]:
    _assert_admin_auth(request)
    try:
        item = await repo.upsert_task_binding(body.chat_id, body.list_chat_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"item": item}


@app.delete("/admin/api/bindings/{chat_id}")
async def admin_delete_binding(request: Request, chat_id: str) -> dict[str, bool]:
    _assert_admin_auth(request)
    deleted = await repo.remove_task_binding(chat_id)
    return {"deleted": deleted}


@app.get("/admin/api/tasks")
async def admin_list_tasks(
    request: Request,
    chat_id: str,
    status: str = "open",
) -> dict[str, Any]:
    _assert_admin_auth(request)
    if status not in {"open", "done", "all"}:
        raise HTTPException(status_code=400, detail="status must be open, done, or all")

    scope_chat_id = await repo.resolve_task_scope(chat_id)
    profile = await repo.get_user_profile(scope_chat_id)
    timezone_name = profile.get("timezone") or settings.timezone
    tasks = await repo.list_tasks(scope_chat_id, status=status)

    return {
        "chat_id": chat_id,
        "scope_chat_id": scope_chat_id,
        "timezone": timezone_name,
        "status": status,
        "tasks": [_serialize_task_for_admin(task, timezone_name) for task in tasks],
    }


@app.post("/admin/api/tasks/batch-add")
async def admin_batch_add_tasks(request: Request, body: AdminBatchAddRequest) -> dict[str, Any]:
    _assert_admin_auth(request)
    lines = [line.strip() for line in body.lines if line and line.strip()]
    if not lines:
        raise HTTPException(status_code=400, detail="No lines to add")
    if len(lines) > 100:
        raise HTTPException(status_code=400, detail="At most 100 lines per request")

    scope_chat_id = await repo.resolve_task_scope(body.chat_id)
    profile = await repo.get_user_profile(scope_chat_id)
    timezone_name = profile.get("timezone") or settings.timezone

    created_items: list[dict[str, Any]] = []
    failed_items: list[dict[str, str]] = []

    for line in lines:
        parsed = parse_task_text(line, timezone_name)
        if not parsed.title:
            failed_items.append({"line": line, "error": "Cannot parse title"})
            continue

        created = await repo.create_task(
            {
                "chat_id": scope_chat_id,
                "title": parsed.title,
                "due_at": parsed.due_at_utc,
                "priority": parsed.priority,
                "status": "open",
                "effort_min": parsed.effort_min,
                "energy_need": parsed.energy_need,
                "source_text": line,
                "source_message_id": None,
            }
        )
        if not created:
            failed_items.append({"line": line, "error": "Cannot create task"})
            continue
        created_items.append(_serialize_task_for_admin(created, timezone_name))

    return {
        "chat_id": body.chat_id,
        "scope_chat_id": scope_chat_id,
        "created_count": len(created_items),
        "failed_count": len(failed_items),
        "created_items": created_items,
        "failed_items": failed_items,
    }


@app.post("/admin/api/tasks/batch-edit")
async def admin_batch_edit_tasks(request: Request, body: AdminBatchEditRequest) -> dict[str, Any]:
    _assert_admin_auth(request)
    if not body.items:
        raise HTTPException(status_code=400, detail="No items to edit")
    if len(body.items) > 100:
        raise HTTPException(status_code=400, detail="At most 100 edits per request")

    scope_chat_id = await repo.resolve_task_scope(body.chat_id)
    profile = await repo.get_user_profile(scope_chat_id)
    timezone_name = profile.get("timezone") or settings.timezone

    updated_items: list[dict[str, Any]] = []
    failed_items: list[dict[str, str]] = []

    for item in body.items:
        task_no = int(item.task_no)
        if task_no <= 0:
            failed_items.append({"task_no": str(task_no), "error": "Invalid task id"})
            continue

        existing = await repo.get_open_task_by_task_no(scope_chat_id, task_no)
        if not existing:
            failed_items.append({"task_no": str(task_no), "error": "Task not found or already done"})
            continue

        parsed = parse_task_text(item.text.strip(), timezone_name)
        if not parsed.title:
            failed_items.append({"task_no": str(task_no), "error": "Cannot parse new content"})
            continue

        patch = {
            "title": parsed.title,
            "due_at": parsed.due_at_utc if parsed.due_at_utc is not None else existing.get("due_at"),
            "priority": parsed.priority,
            "effort_min": parsed.effort_min,
            "energy_need": parsed.energy_need,
            "source_text": item.text.strip(),
        }
        updated = await repo.update_task_by_task_no(scope_chat_id, task_no, patch)
        if not updated:
            failed_items.append({"task_no": str(task_no), "error": "Update failed"})
            continue
        updated_items.append(_serialize_task_for_admin(updated, timezone_name))

    return {
        "chat_id": body.chat_id,
        "scope_chat_id": scope_chat_id,
        "updated_count": len(updated_items),
        "failed_count": len(failed_items),
        "updated_items": updated_items,
        "failed_items": failed_items,
    }


@app.post("/admin/api/tasks/batch-delete")
async def admin_batch_delete_tasks(request: Request, body: AdminBatchDeleteRequest) -> dict[str, Any]:
    _assert_admin_auth(request)
    raw_ids = [int(task_no) for task_no in body.task_nos]
    task_nos = sorted({task_no for task_no in raw_ids if task_no > 0})
    if not task_nos:
        raise HTTPException(status_code=400, detail="No valid task ids")
    if len(task_nos) > 200:
        raise HTTPException(status_code=400, detail="At most 200 ids per request")

    scope_chat_id = await repo.resolve_task_scope(body.chat_id)
    deleted: list[int] = []
    not_found: list[int] = []

    for task_no in task_nos:
        removed = await repo.delete_task_by_task_no(scope_chat_id, task_no)
        if removed:
            deleted.append(task_no)
        else:
            not_found.append(task_no)

    return {
        "chat_id": body.chat_id,
        "scope_chat_id": scope_chat_id,
        "deleted_count": len(deleted),
        "not_found_count": len(not_found),
        "deleted": deleted,
        "not_found": not_found,
    }


@app.get("/webhook", response_class=PlainTextResponse)
async def verify_webhook(request: Request) -> str:
    mode = request.query_params.get("hub.mode")
    verify_token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")

    if mode == "subscribe" and verify_token == settings.whatsapp_verify_token:
        return challenge or ""

    raise HTTPException(status_code=403, detail="Webhook verification failed")


@app.post("/webhook")
async def receive_webhook(request: Request) -> dict[str, int | str]:
    _update_webhook_runtime(
        last_status="received",
        last_error="",
        last_messages_count=0,
        last_processed=0,
        last_ignored=0,
    )

    raw_body = await request.body()
    signature = request.headers.get("x-hub-signature-256")

    if not _verify_signature(raw_body, signature):
        _update_webhook_runtime(last_status="invalid_signature", last_error="x-hub-signature-256 mismatch")
        raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        logger.warning("Invalid JSON payload: %s", exc)
        _update_webhook_runtime(last_status="invalid_json", last_error=str(exc))
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc

    messages = extract_inbound_messages(payload)
    processed = 0
    ignored = 0
    failed = 0
    _update_webhook_runtime(
        last_status="processing",
        last_messages_count=len(messages),
        last_processed=0,
        last_ignored=0,
        last_error="",
    )

    for message in messages:
        try:
            reply = await service.handle_message(
                chat_id=message.chat_id,
                text=message.text,
                source_message_id=message.message_id,
                sender_id=message.sender_id,
                is_group=message.is_group,
            )
            if reply is None:
                ignored += 1
                continue
            await whatsapp_client.send_text_message(message.chat_id, reply)
            processed += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            logger.exception("Failed to process message %s: %s", message.message_id, exc)

    runtime_status = "ok" if failed == 0 else "partial_error"
    runtime_error = "" if failed == 0 else f"{failed} message(s) failed"
    _update_webhook_runtime(
        last_status=runtime_status,
        last_messages_count=len(messages),
        last_processed=processed,
        last_ignored=ignored,
        last_error=runtime_error,
    )

    return {"status": "ok", "processed": processed, "ignored": ignored, "failed": failed}


@app.get("/internal/daily-push")
@app.post("/internal/daily-push")
async def daily_push(request: Request) -> dict[str, int | str]:
    _assert_cron_auth(request)
    result = await service.push_daily_plans()
    return {
        "status": "ok",
        "pushed": result["pushed"],
        "skipped": result["skipped"],
    }


def _verify_signature(raw_body: bytes, signature_header: str | None) -> bool:
    if not settings.whatsapp_app_secret:
        return True

    if not signature_header or not signature_header.startswith("sha256="):
        return False

    expected = hmac.new(
        settings.whatsapp_app_secret.encode("utf-8"),
        msg=raw_body,
        digestmod=hashlib.sha256,
    ).hexdigest()
    provided = signature_header.split("=", 1)[1]

    return hmac.compare_digest(expected, provided)


def _assert_cron_auth(request: Request) -> None:
    if not settings.cron_secret:
        return

    auth_header = request.headers.get("authorization", "")
    query_secret = request.query_params.get("cron_secret", "")

    if auth_header == f"Bearer {settings.cron_secret}":
        return

    if query_secret == settings.cron_secret:
        return

    raise HTTPException(status_code=401, detail="Unauthorized cron call")


def _assert_admin_auth(request: Request) -> None:
    if not settings.admin_token:
        raise HTTPException(status_code=503, detail="ADMIN_TOKEN is not configured")

    header_token = request.headers.get("x-admin-token", "").strip()
    query_token = request.query_params.get("token", "").strip()
    bearer_token = _extract_bearer_token(request.headers.get("authorization", ""))

    provided = header_token or bearer_token or query_token
    if not provided or not hmac.compare_digest(provided, settings.admin_token):
        raise HTTPException(status_code=401, detail="Unauthorized admin token")


def _extract_bearer_token(auth_header: str) -> str:
    if not auth_header:
        return ""
    if not auth_header.lower().startswith("bearer "):
        return ""
    return auth_header.split(" ", 1)[1].strip()


def _is_configured(value: str) -> bool:
    return bool(value and value.strip())


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _update_webhook_runtime(
    *,
    last_status: str,
    last_error: str,
    last_messages_count: int | None = None,
    last_processed: int | None = None,
    last_ignored: int | None = None,
) -> None:
    WEBHOOK_RUNTIME["last_attempt_utc"] = _utc_now_iso()
    WEBHOOK_RUNTIME["last_status"] = last_status
    WEBHOOK_RUNTIME["last_error"] = last_error
    if last_messages_count is not None:
        WEBHOOK_RUNTIME["last_messages_count"] = int(last_messages_count)
    if last_processed is not None:
        WEBHOOK_RUNTIME["last_processed"] = int(last_processed)
    if last_ignored is not None:
        WEBHOOK_RUNTIME["last_ignored"] = int(last_ignored)


def _webhook_runtime_check() -> dict[str, object]:
    last_status = str(WEBHOOK_RUNTIME.get("last_status", "none"))
    last_attempt = str(WEBHOOK_RUNTIME.get("last_attempt_utc", ""))
    last_error = str(WEBHOOK_RUNTIME.get("last_error", ""))
    last_messages = int(WEBHOOK_RUNTIME.get("last_messages_count", 0) or 0)
    last_processed = int(WEBHOOK_RUNTIME.get("last_processed", 0) or 0)
    last_ignored = int(WEBHOOK_RUNTIME.get("last_ignored", 0) or 0)

    if last_status == "none":
        return {
            "ok": True,
            "detail": "No webhook events seen since current instance started",
        }

    ok = last_status not in {"invalid_signature", "invalid_json", "partial_error"}
    detail = f"status={last_status}, processed={last_processed}, ignored={last_ignored}, total={last_messages}, at={last_attempt}"
    if last_error:
        detail = f"{detail}, error={last_error}"

    return {
        "ok": ok,
        "detail": detail,
    }


async def _build_status_snapshot() -> dict[str, object]:
    env_status = {
        "WHATSAPP_ACCESS_TOKEN": _is_configured(settings.whatsapp_access_token),
        "WHATSAPP_PHONE_NUMBER_ID": _is_configured(settings.whatsapp_phone_number_id),
        "WHATSAPP_VERIFY_TOKEN": _is_configured(settings.whatsapp_verify_token),
        "WHATSAPP_APP_SECRET": _is_configured(settings.whatsapp_app_secret),
        "SUPABASE_URL": _is_configured(settings.supabase_url),
        "SUPABASE_SERVICE_ROLE_KEY": _is_configured(settings.supabase_service_role_key),
        "OPENAI_API_KEY": _is_configured(settings.openai_api_key),
        "CRON_SECRET": _is_configured(settings.cron_secret),
        "ADMIN_TOKEN": _is_configured(settings.admin_token),
    }

    supabase = await repo.health_check()
    whatsapp_api = await whatsapp_client.health_check()

    checks = {
        "app": {"ok": True, "detail": "FastAPI route is reachable"},
        "supabase": supabase,
        "webhook_verify_token": {
            "ok": env_status["WHATSAPP_VERIFY_TOKEN"],
            "detail": "Used by GET /webhook handshake",
        },
        "webhook_signature": {
            "ok": env_status["WHATSAPP_APP_SECRET"],
            "detail": "Used by POST /webhook signature validation",
        },
        "whatsapp_send_ready": {
            "ok": env_status["WHATSAPP_ACCESS_TOKEN"] and env_status["WHATSAPP_PHONE_NUMBER_ID"],
            "detail": "Required to send messages via Cloud API",
        },
        "whatsapp_api": whatsapp_api,
        "webhook_runtime": _webhook_runtime_check(),
        "openai_ready": {
            "ok": env_status["OPENAI_API_KEY"],
            "detail": "Used for ADHD ranking",
        },
        "cron_auth": {
            "ok": env_status["CRON_SECRET"],
            "detail": "Protects /internal/daily-push",
        },
        "admin_auth": {
            "ok": env_status["ADMIN_TOKEN"],
            "detail": "Protects /admin/api/*",
        },
    }

    missing_env = [name for name, ok in env_status.items() if not ok]
    healthy = all(bool(item.get("ok")) for item in checks.values())

    return {
        "healthy": healthy,
        "timestamp_utc": _utc_now_iso(),
        "deployment_url": os.getenv("VERCEL_URL", ""),
        "git_commit_sha": os.getenv("VERCEL_GIT_COMMIT_SHA", ""),
        "timezone": settings.timezone,
        "checks": checks,
        "env": {name: ("configured" if ok else "missing") for name, ok in env_status.items()},
        "missing_env": missing_env,
    }


def _render_status_html(snapshot: dict[str, object]) -> str:
    checks = snapshot.get("checks", {})
    rows = []
    for name, info in checks.items():
        if not isinstance(info, dict):
            continue
        ok = bool(info.get("ok"))
        status_text = "OK" if ok else "Missing / Error"
        detail = escape(str(info.get("detail", info.get("error", ""))))
        color = "#16a34a" if ok else "#ef4444"
        rows.append(
            "<tr>"
            f"<td>{escape(name)}</td>"
            f"<td style='color:{color};font-weight:600'>{status_text}</td>"
            f"<td>{detail}</td>"
            "</tr>"
        )

    missing_env = snapshot.get("missing_env", [])
    missing_html = ""
    if isinstance(missing_env, list) and missing_env:
        items = "".join(f"<li>{escape(str(item))}</li>" for item in missing_env)
        missing_html = (
            "<div class='warn'>"
            "<strong>Missing env:</strong>"
            f"<ul>{items}</ul>"
            "</div>"
        )

    badge_color = "#16a34a" if snapshot.get("healthy") else "#f97316"
    badge_text = "Healthy" if snapshot.get("healthy") else "Needs Setup"

    return f"""
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>WhatsApp Bot Status</title>
    <style>
      :root {{
        --bg: #111111;
        --panel: #1f1f1f;
        --text: #ffffff;
        --muted: #9ca3af;
        --line: #374151;
        --accent: #f97316;
      }}
      * {{ box-sizing: border-box; }}
      body {{
        margin: 0;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        background: linear-gradient(180deg, #0f0f0f 0%, #181818 100%);
        color: var(--text);
        padding: 24px;
      }}
      .container {{
        max-width: 980px;
        margin: 0 auto;
      }}
      .header {{
        display: flex;
        justify-content: space-between;
        align-items: center;
        gap: 12px;
        margin-bottom: 16px;
      }}
      .badge {{
        background: {badge_color};
        color: #fff;
        border-radius: 999px;
        padding: 6px 12px;
        font-size: 12px;
        font-weight: 700;
      }}
      .meta {{
        color: var(--muted);
        font-size: 13px;
        margin-bottom: 16px;
      }}
      .panel {{
        background: var(--panel);
        border: 1px solid var(--line);
        border-radius: 14px;
        padding: 16px;
      }}
      table {{
        width: 100%;
        border-collapse: collapse;
      }}
      th, td {{
        border-bottom: 1px solid var(--line);
        text-align: left;
        padding: 10px 8px;
        vertical-align: top;
      }}
      th {{
        color: #d1d5db;
        font-size: 12px;
        text-transform: uppercase;
        letter-spacing: 0.04em;
      }}
      td {{
        font-size: 14px;
      }}
      .warn {{
        margin-top: 16px;
        border: 1px solid #fb923c;
        background: rgba(249, 115, 22, 0.1);
        border-radius: 10px;
        padding: 12px;
      }}
      .warn ul {{
        margin: 8px 0 0 20px;
        padding: 0;
      }}
      a {{
        color: var(--accent);
      }}
    </style>
  </head>
  <body>
    <div class="container">
      <div class="header">
        <h1 style="margin:0;font-size:22px;">WhatsApp ADHD Bot Status</h1>
        <span class="badge">{badge_text}</span>
      </div>
      <div class="meta">
        UTC: {escape(str(snapshot.get("timestamp_utc", "")))}<br />
        Timezone: {escape(str(snapshot.get("timezone", "")))}<br />
        Deployment: {escape(str(snapshot.get("deployment_url", "")))}<br />
        Commit: {escape(str(snapshot.get("git_commit_sha", "")))}
      </div>
      <div class="panel">
        <table>
          <thead>
            <tr><th>Check</th><th>Status</th><th>Detail</th></tr>
          </thead>
          <tbody>
            {"".join(rows)}
          </tbody>
        </table>
        {missing_html}
      </div>
      <p class="meta" style="margin-top:12px;">
        JSON endpoint: <a href="/status.json">/status.json</a>
      </p>
    </div>
  </body>
</html>
"""


def _serialize_task_for_admin(task: dict[str, Any], timezone_name: str) -> dict[str, Any]:
    due_at = task.get("due_at")
    due_local = ""
    if due_at:
        try:
            dt = datetime.fromisoformat(str(due_at))
            due_local = dt.astimezone(ZoneInfo(timezone_name)).strftime("%Y-%m-%d %H:%M")
        except Exception:  # noqa: BLE001
            due_local = str(due_at)

    return {
        "id": int(task.get("id") or 0),
        "task_no": int(task.get("task_no") or task.get("id") or 0),
        "title": str(task.get("title") or ""),
        "priority": int(task.get("priority") or 2),
        "status": str(task.get("status") or ""),
        "due_at": due_at,
        "due_local": due_local,
        "created_at": str(task.get("created_at") or ""),
        "source_text": str(task.get("source_text") or ""),
    }


def _render_admin_html() -> str:
    return """
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Admin Console</title>
    <style>
      :root {
        --bg: #111111;
        --panel: #1f1f1f;
        --text: #ffffff;
        --muted: #9ca3af;
        --line: #374151;
        --accent: #f97316;
        --danger: #ef4444;
      }
      * { box-sizing: border-box; }
      body {
        margin: 0;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        background: linear-gradient(180deg, #0f0f0f 0%, #181818 100%);
        color: var(--text);
        padding: 20px;
      }
      .container {
        max-width: 1200px;
        margin: 0 auto;
      }
      .card {
        background: var(--panel);
        border: 1px solid var(--line);
        border-radius: 12px;
        padding: 14px;
        margin-bottom: 14px;
      }
      h1, h2, h3 { margin: 0 0 10px 0; }
      h1 { font-size: 22px; }
      h2 { font-size: 18px; }
      .muted { color: var(--muted); font-size: 13px; }
      .grid {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(340px, 1fr));
        gap: 14px;
      }
      label {
        display: block;
        font-size: 12px;
        color: #d1d5db;
        margin-bottom: 4px;
      }
      input, textarea, select, button {
        width: 100%;
        padding: 9px 10px;
        border-radius: 8px;
        border: 1px solid #4b5563;
        background: #111827;
        color: #fff;
      }
      textarea {
        min-height: 92px;
        resize: vertical;
      }
      button {
        cursor: pointer;
        background: var(--accent);
        border-color: #fb923c;
        font-weight: 600;
      }
      button.secondary {
        background: #374151;
        border-color: #4b5563;
      }
      button.danger {
        background: #7f1d1d;
        border-color: #991b1b;
      }
      .row {
        display: grid;
        grid-template-columns: 1fr 1fr auto;
        gap: 8px;
        margin-bottom: 8px;
      }
      .row.single {
        grid-template-columns: 1fr auto;
      }
      table {
        width: 100%;
        border-collapse: collapse;
      }
      th, td {
        border-bottom: 1px solid var(--line);
        text-align: left;
        padding: 8px 6px;
        font-size: 13px;
        vertical-align: top;
      }
      th {
        color: #d1d5db;
        font-size: 12px;
        text-transform: uppercase;
      }
      .pill {
        display: inline-block;
        border: 1px solid #4b5563;
        border-radius: 999px;
        padding: 2px 8px;
        font-size: 12px;
      }
      .spacer { height: 8px; }
      pre {
        margin: 0;
        padding: 10px;
        border: 1px solid var(--line);
        border-radius: 8px;
        background: #0b0f19;
        color: #d1d5db;
        overflow: auto;
        font-size: 12px;
      }
      .inline-actions {
        display: flex;
        gap: 8px;
      }
      .inline-actions button {
        width: auto;
      }
    </style>
  </head>
  <body>
    <div class="container">
      <h1>WhatsApp ADHD Bot Admin</h1>
      <p class="muted">管理白名單、共享 task list、批量任務操作。先輸入 ADMIN_TOKEN。</p>

      <div class="card">
        <div class="row single">
          <div>
            <label for="admin-token">ADMIN_TOKEN</label>
            <input id="admin-token" type="password" placeholder="paste your ADMIN_TOKEN" />
          </div>
          <button id="save-token" style="align-self:end;width:160px;">Save Token</button>
        </div>
        <p class="muted" id="token-status">Token 未設定</p>
      </div>

      <div class="grid">
        <div class="card">
          <h2>Whitelist</h2>
          <div class="row">
            <div>
              <label for="wl-phone">Phone Number</label>
              <input id="wl-phone" placeholder="85291234567" />
            </div>
            <div>
              <label for="wl-label">Label</label>
              <input id="wl-label" placeholder="Tom / Team A" />
            </div>
            <button id="wl-add" style="align-self:end;width:120px;">Add/Update</button>
          </div>
          <div class="inline-actions" style="margin-bottom:8px;">
            <button class="secondary" id="wl-refresh">Refresh</button>
          </div>
          <table>
            <thead><tr><th>sender_id</th><th>label</th><th>created_at</th><th></th></tr></thead>
            <tbody id="wl-tbody"></tbody>
          </table>
        </div>

        <div class="card">
          <h2>Shared Task Lists</h2>
          <div class="row">
            <div>
              <label for="bind-chat">Chat ID / Phone</label>
              <input id="bind-chat" placeholder="85291234567 or group id" />
            </div>
            <div>
              <label for="bind-list">List Owner Chat ID</label>
              <input id="bind-list" placeholder="85290001111" />
            </div>
            <button id="bind-save" style="align-self:end;width:120px;">Save</button>
          </div>
          <div class="inline-actions" style="margin-bottom:8px;">
            <button class="secondary" id="bind-refresh">Refresh</button>
          </div>
          <table>
            <thead><tr><th>chat_id</th><th>list_chat_id</th><th>updated_at</th><th></th></tr></thead>
            <tbody id="bind-tbody"></tbody>
          </table>
        </div>
      </div>

      <div class="card">
        <h2>Tasks (Batch Add / Edit / Delete)</h2>
        <div class="row">
          <div>
            <label for="task-chat-id">Chat ID / Phone</label>
            <input id="task-chat-id" placeholder="85291234567 or group id" />
          </div>
          <div>
            <label for="task-status">Status</label>
            <select id="task-status">
              <option value="open">open</option>
              <option value="done">done</option>
              <option value="all">all</option>
            </select>
          </div>
          <button id="task-load" style="align-self:end;width:120px;">Load</button>
        </div>

        <div class="spacer"></div>
        <table>
          <thead>
            <tr>
              <th>#</th>
              <th>priority</th>
              <th>title</th>
              <th>due (local)</th>
              <th>status</th>
            </tr>
          </thead>
          <tbody id="task-tbody"></tbody>
        </table>

        <div class="spacer"></div>
        <h3>Batch Add (自然語言，一行一項)</h3>
        <textarea id="batch-add-lines" placeholder="明天 3pm 跟客開會&#10;週四 9am 回覆電郵"></textarea>
        <div class="inline-actions" style="margin-top:8px;">
          <button id="batch-add-btn">Batch Add</button>
        </div>

        <div class="spacer"></div>
        <h3>Batch Edit (格式: task_id | 新內容)</h3>
        <textarea id="batch-edit-lines" placeholder="3 | 明天 4pm 跟客開會&#10;8 | 週五 中午 交 proposal"></textarea>
        <div class="inline-actions" style="margin-top:8px;">
          <button id="batch-edit-btn">Batch Edit</button>
        </div>

        <div class="spacer"></div>
        <h3>Batch Delete (輸入 task_id，空格/逗號分隔)</h3>
        <input id="batch-delete-ids" placeholder="3 5 8" />
        <div class="inline-actions" style="margin-top:8px;">
          <button class="danger" id="batch-delete-btn">Batch Delete</button>
        </div>

        <div class="spacer"></div>
        <pre id="ops-result">Ready.</pre>
      </div>
    </div>

    <script>
      const qs = new URLSearchParams(window.location.search);
      const tokenInput = document.getElementById("admin-token");
      const tokenStatus = document.getElementById("token-status");

      tokenInput.value = qs.get("token") || localStorage.getItem("admin_token") || "";
      updateTokenStatus();

      document.getElementById("save-token").addEventListener("click", () => {
        localStorage.setItem("admin_token", tokenInput.value.trim());
        updateTokenStatus();
      });

      function updateTokenStatus() {
        tokenStatus.textContent = tokenInput.value.trim() ? "Token 已設定" : "Token 未設定";
      }

      function authHeaders(json = true) {
        const token = tokenInput.value.trim();
        const headers = { "X-Admin-Token": token };
        if (json) headers["Content-Type"] = "application/json";
        return headers;
      }

      async function api(path, options = {}) {
        const opts = { ...options };
        opts.headers = { ...authHeaders(!opts.noJson), ...(opts.headers || {}) };
        if (opts.noJson) {
          delete opts.headers["Content-Type"];
          delete opts.noJson;
        }
        const resp = await fetch(path, opts);
        if (!resp.ok) {
          const text = await resp.text();
          throw new Error(text || `HTTP ${resp.status}`);
        }
        return resp.json();
      }

      function showResult(data) {
        document.getElementById("ops-result").textContent = JSON.stringify(data, null, 2);
      }

      function currentTaskChatId() {
        return document.getElementById("task-chat-id").value.trim();
      }

      async function loadWhitelist() {
        const data = await api("/admin/api/whitelist");
        const tbody = document.getElementById("wl-tbody");
        tbody.innerHTML = "";
        for (const item of data.items || []) {
          const tr = document.createElement("tr");
          tr.innerHTML = `
            <td>${item.sender_id || ""}</td>
            <td>${item.label || ""}</td>
            <td>${item.created_at || ""}</td>
            <td><button class="danger" data-del="${item.sender_id || ""}">Delete</button></td>
          `;
          tbody.appendChild(tr);
        }
        for (const btn of tbody.querySelectorAll("button[data-del]")) {
          btn.addEventListener("click", async () => {
            try {
              await api(`/admin/api/whitelist/${encodeURIComponent(btn.dataset.del)}`, { method: "DELETE", noJson: true });
              await loadWhitelist();
            } catch (err) {
              alert(err.message);
            }
          });
        }
        showResult({ whitelist_count: (data.items || []).length });
      }

      async function loadBindings() {
        const data = await api("/admin/api/bindings");
        const tbody = document.getElementById("bind-tbody");
        tbody.innerHTML = "";
        for (const item of data.items || []) {
          const tr = document.createElement("tr");
          tr.innerHTML = `
            <td>${item.chat_id || ""}</td>
            <td>${item.list_chat_id || ""}</td>
            <td>${item.updated_at || ""}</td>
            <td><button class="danger" data-del="${item.chat_id || ""}">Delete</button></td>
          `;
          tbody.appendChild(tr);
        }
        for (const btn of tbody.querySelectorAll("button[data-del]")) {
          btn.addEventListener("click", async () => {
            try {
              await api(`/admin/api/bindings/${encodeURIComponent(btn.dataset.del)}`, { method: "DELETE", noJson: true });
              await loadBindings();
            } catch (err) {
              alert(err.message);
            }
          });
        }
        showResult({ binding_count: (data.items || []).length });
      }

      async function loadTasks() {
        const chatId = currentTaskChatId();
        if (!chatId) {
          alert("請先輸入 Chat ID / Phone");
          return;
        }
        const status = document.getElementById("task-status").value;
        const data = await api(`/admin/api/tasks?chat_id=${encodeURIComponent(chatId)}&status=${encodeURIComponent(status)}`);
        const tbody = document.getElementById("task-tbody");
        tbody.innerHTML = "";
        for (const task of data.tasks || []) {
          const tr = document.createElement("tr");
          tr.innerHTML = `
            <td><span class="pill">#${task.task_no}</span></td>
            <td>${task.priority}</td>
            <td>${task.title || ""}</td>
            <td>${task.due_local || "未排程"}</td>
            <td>${task.status || ""}</td>
          `;
          tbody.appendChild(tr);
        }
        showResult({
          chat_id: data.chat_id,
          scope_chat_id: data.scope_chat_id,
          timezone: data.timezone,
          task_count: (data.tasks || []).length
        });
      }

      document.getElementById("wl-add").addEventListener("click", async () => {
        try {
          const sender_id = document.getElementById("wl-phone").value.trim();
          const label = document.getElementById("wl-label").value.trim();
          if (!sender_id) {
            alert("請輸入電話號碼");
            return;
          }
          const data = await api("/admin/api/whitelist", {
            method: "POST",
            body: JSON.stringify({ sender_id, label })
          });
          showResult(data);
          await loadWhitelist();
        } catch (err) {
          alert(err.message);
        }
      });

      document.getElementById("wl-refresh").addEventListener("click", () => loadWhitelist().catch(err => alert(err.message)));

      document.getElementById("bind-save").addEventListener("click", async () => {
        try {
          const chat_id = document.getElementById("bind-chat").value.trim();
          const list_chat_id = document.getElementById("bind-list").value.trim();
          if (!chat_id || !list_chat_id) {
            alert("請輸入 chat_id 與 list_chat_id");
            return;
          }
          const data = await api("/admin/api/bindings", {
            method: "POST",
            body: JSON.stringify({ chat_id, list_chat_id })
          });
          showResult(data);
          await loadBindings();
        } catch (err) {
          alert(err.message);
        }
      });

      document.getElementById("bind-refresh").addEventListener("click", () => loadBindings().catch(err => alert(err.message)));
      document.getElementById("task-load").addEventListener("click", () => loadTasks().catch(err => alert(err.message)));

      document.getElementById("batch-add-btn").addEventListener("click", async () => {
        try {
          const chat_id = currentTaskChatId();
          if (!chat_id) {
            alert("請先輸入 Chat ID / Phone");
            return;
          }
          const lines = document.getElementById("batch-add-lines").value.split("\\n").map(s => s.trim()).filter(Boolean);
          const data = await api("/admin/api/tasks/batch-add", {
            method: "POST",
            body: JSON.stringify({ chat_id, lines })
          });
          showResult(data);
          await loadTasks();
        } catch (err) {
          alert(err.message);
        }
      });

      document.getElementById("batch-edit-btn").addEventListener("click", async () => {
        try {
          const chat_id = currentTaskChatId();
          if (!chat_id) {
            alert("請先輸入 Chat ID / Phone");
            return;
          }
          const lines = document.getElementById("batch-edit-lines").value.split("\\n").map(s => s.trim()).filter(Boolean);
          const items = [];
          for (const line of lines) {
            const parts = line.split("|");
            if (parts.length < 2) continue;
            const task_no = Number(parts[0].trim());
            const text = parts.slice(1).join("|").trim();
            if (!Number.isFinite(task_no) || task_no <= 0 || !text) continue;
            items.push({ task_no, text });
          }
          if (!items.length) {
            alert("格式錯誤，請用：task_id | 新內容");
            return;
          }
          const data = await api("/admin/api/tasks/batch-edit", {
            method: "POST",
            body: JSON.stringify({ chat_id, items })
          });
          showResult(data);
          await loadTasks();
        } catch (err) {
          alert(err.message);
        }
      });

      document.getElementById("batch-delete-btn").addEventListener("click", async () => {
        try {
          const chat_id = currentTaskChatId();
          if (!chat_id) {
            alert("請先輸入 Chat ID / Phone");
            return;
          }
          const raw = document.getElementById("batch-delete-ids").value;
          const task_nos = Array.from(new Set(raw.split(/[\\s,]+/).map(s => Number(s.trim())).filter(n => Number.isFinite(n) && n > 0)));
          if (!task_nos.length) {
            alert("請輸入至少一個 task id");
            return;
          }
          const data = await api("/admin/api/tasks/batch-delete", {
            method: "POST",
            body: JSON.stringify({ chat_id, task_nos })
          });
          showResult(data);
          await loadTasks();
        } catch (err) {
          alert(err.message);
        }
      });

      (async () => {
        try {
          await loadWhitelist();
          await loadBindings();
        } catch (err) {
          showResult({ error: err.message });
        }
      })();
    </script>
  </body>
</html>
"""


def _render_legal_page(title: str, content: str) -> str:
    safe_title = escape(title)
    return f"""
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>{safe_title}</title>
    <style>
      :root {{
        --bg: #111111;
        --panel: #1f1f1f;
        --text: #ffffff;
        --muted: #9ca3af;
        --line: #374151;
        --accent: #f97316;
      }}
      * {{ box-sizing: border-box; }}
      body {{
        margin: 0;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        background: linear-gradient(180deg, #0f0f0f 0%, #181818 100%);
        color: var(--text);
        padding: 24px;
      }}
      .container {{
        max-width: 900px;
        margin: 0 auto;
        background: var(--panel);
        border: 1px solid var(--line);
        border-radius: 14px;
        padding: 20px;
      }}
      h1 {{ margin-top: 0; }}
      h2 {{ margin-top: 24px; font-size: 18px; }}
      p, li {{ color: #e5e7eb; line-height: 1.65; }}
      .muted {{ color: var(--muted); font-size: 13px; }}
      a {{ color: var(--accent); }}
      code {{
        background: #2b2b2b;
        border: 1px solid #444;
        border-radius: 6px;
        padding: 2px 6px;
      }}
    </style>
  </head>
  <body>
    <div class="container">
      <h1>{safe_title}</h1>
      <p class="muted">This page is publicly accessible and intended for Meta app compliance.</p>
      {content}
    </div>
  </body>
</html>
"""
