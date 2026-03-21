from __future__ import annotations

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


def extract_inbound_messages(payload: dict[str, Any]) -> list[InboundMessage]:
    messages: list[InboundMessage] = []

    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for message in value.get("messages", []):
                text = message.get("text", {}).get("body")
                if not text:
                    continue

                messages.append(
                    InboundMessage(
                        message_id=message.get("id", ""),
                        chat_id=message.get("from", ""),
                        text=text,
                        raw_payload=message,
                    )
                )
    return messages
