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


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


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
    raw_body = await request.body()
    signature = request.headers.get("x-hub-signature-256")

    if not _verify_signature(raw_body, signature):
        raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        logger.warning("Invalid JSON payload: %s", exc)
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc

    messages = extract_inbound_messages(payload)
    processed = 0

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
            logger.exception("Failed to process message %s: %s", message.message_id, exc)

    return {"status": "ok", "processed": processed}


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
    healthy = all(item.get("ok") for item in checks.values())

    return {
        "healthy": healthy,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
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
