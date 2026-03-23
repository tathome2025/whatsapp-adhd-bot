from __future__ import annotations

import re
from typing import Any

import httpx

from app.config import Settings
from app.models import InboundMessage


class WhatsAppClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    @property
    def message_api_url(self) -> str:
        return (
            f"https://graph.facebook.com/v20.0/"
            f"{self.settings.whatsapp_phone_number_id}/messages"
        )

    async def send_text_message(self, chat_id: str, body: str) -> None:
        if not self.settings.whatsapp_access_token or not self.settings.whatsapp_phone_number_id:
            raise RuntimeError("WhatsApp settings are not configured")

        payload = {
            "messaging_product": "whatsapp",
            "to": chat_id,
            "type": "text",
            "text": {"body": body[:4096]},
        }
        headers = {
            "Authorization": f"Bearer {self.settings.whatsapp_access_token}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(self.message_api_url, headers=headers, json=payload)
        resp.raise_for_status()

    async def health_check(self) -> dict[str, str | bool]:
        if not self.settings.whatsapp_access_token or not self.settings.whatsapp_phone_number_id:
            return {"ok": False, "error": "WHATSAPP_ACCESS_TOKEN or WHATSAPP_PHONE_NUMBER_ID is missing"}

        url = f"https://graph.facebook.com/v20.0/{self.settings.whatsapp_phone_number_id}"
        headers = {"Authorization": f"Bearer {self.settings.whatsapp_access_token}"}
        params = {"fields": "id,display_phone_number"}

        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.get(url, headers=headers, params=params)
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

        return {"ok": True}


def extract_inbound_messages(payload: dict[str, Any]) -> list[InboundMessage]:
    messages: list[InboundMessage] = []

    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for message in value.get("messages", []):
                text = message.get("text", {}).get("body")
                if not text:
                    continue

                chat_id = _extract_chat_id(message)
                sender_id = str(message.get("author") or message.get("from") or "")
                is_group = _is_group_message(message, chat_id)

                messages.append(
                    InboundMessage(
                        message_id=str(message.get("id", "")),
                        chat_id=chat_id,
                        sender_id=sender_id,
                        is_group=is_group,
                        text=text,
                        raw_payload=message,
                    )
                )
    return messages


def _extract_chat_id(message: dict[str, Any]) -> str:
    conversation = message.get("conversation")
    if isinstance(conversation, dict):
        cid = conversation.get("id")
        if cid:
            return str(cid)

    for key in ("group_id", "chat_id"):
        value = message.get(key)
        if value:
            return str(value)

    return str(message.get("from") or "")


def _is_group_message(message: dict[str, Any], chat_id: str) -> bool:
    if str(message.get("recipient_type", "")).lower() == "group":
        return True

    if message.get("author"):
        return True

    if message.get("group_id"):
        return True

    if chat_id.endswith("@g.us"):
        return True

    if re.fullmatch(r"\d+-\d+", chat_id):
        return True

    return False
