from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from datetime import datetime, timezone
from html import escape

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from app.config import get_settings
from app.openai_planner import OpenAIPlanner
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
}


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
    _update_webhook_runtime(last_status="received", last_error="", last_messages_count=0, last_processed=0)

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
    failed = 0
    _update_webhook_runtime(last_status="processing", last_messages_count=len(messages), last_processed=0, last_error="")

    for message in messages:
        try:
            reply = await service.handle_message(
                chat_id=message.chat_id,
                text=message.text,
                source_message_id=message.message_id,
            )
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
        last_error=runtime_error,
    )

    return {"status": "ok", "processed": processed, "failed": failed}


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
) -> None:
    WEBHOOK_RUNTIME["last_attempt_utc"] = _utc_now_iso()
    WEBHOOK_RUNTIME["last_status"] = last_status
    WEBHOOK_RUNTIME["last_error"] = last_error
    if last_messages_count is not None:
        WEBHOOK_RUNTIME["last_messages_count"] = int(last_messages_count)
    if last_processed is not None:
        WEBHOOK_RUNTIME["last_processed"] = int(last_processed)


def _webhook_runtime_check() -> dict[str, object]:
    last_status = str(WEBHOOK_RUNTIME.get("last_status", "none"))
    last_attempt = str(WEBHOOK_RUNTIME.get("last_attempt_utc", ""))
    last_error = str(WEBHOOK_RUNTIME.get("last_error", ""))
    last_messages = int(WEBHOOK_RUNTIME.get("last_messages_count", 0) or 0)
    last_processed = int(WEBHOOK_RUNTIME.get("last_processed", 0) or 0)

    if last_status == "none":
        return {
            "ok": True,
            "detail": "No webhook events seen since current instance started",
        }

    ok = last_status not in {"invalid_signature", "invalid_json", "partial_error"}
    detail = f"status={last_status}, processed={last_processed}/{last_messages}, at={last_attempt}"
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
