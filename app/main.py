import hashlib
import hmac
import json
import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse

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
