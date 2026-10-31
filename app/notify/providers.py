"""Messaging behind one interface (plan.md §18.1): WhatsApp first, SMS as the fallback.

`mock` (default) keeps every message in an in-memory outbox, which the tests and the demo console read.
The real adapters are stubs: they need credentials and are not called in this prototype.
"""

import logging
import uuid
from dataclasses import dataclass, field
from typing import Protocol

from app.config import get_settings

log = logging.getLogger("khata.messaging")


@dataclass
class Delivery:
    ok: bool
    ref: str | None = None
    error: str | None = None


class MessagingProvider(Protocol):
    channel: str

    async def send(self, to_phone: str, text: str) -> Delivery: ...


@dataclass
class MockProvider:
    """Records instead of sending. `fail_for` makes sends to those numbers fail (to exercise the fallback)."""

    channel: str
    outbox: list[dict] = field(default_factory=list)
    fail_for: set[str] = field(default_factory=set)

    async def send(self, to_phone: str, text: str) -> Delivery:
        if to_phone in self.fail_for:
            return Delivery(False, error=f"{self.channel} not available for {to_phone}")
        ref = f"mock-{self.channel}-{uuid.uuid4().hex[:8]}"
        self.outbox.append({"channel": self.channel, "to": to_phone, "text": text, "ref": ref})
        log.info("%s to=%s %s", self.channel.upper(), to_phone, text)
        return Delivery(True, ref=ref)


class WhatsAppCloudStub:
    """Adapter stub for the WhatsApp Business (Cloud) API. Needs WHATSAPP_TOKEN and approved templates."""

    channel = "whatsapp"

    async def send(self, to_phone: str, text: str) -> Delivery:
        if not get_settings().whatsapp_token:
            return Delivery(False, error="WHATSAPP_TOKEN not set")
        raise NotImplementedError("Wire the WhatsApp Cloud API template call here.")


class SmsGatewayStub:
    """Adapter stub for an SMS gateway (e.g. MSG91). Needs SMS_API_KEY and a DLT-registered template."""

    channel = "sms"

    async def send(self, to_phone: str, text: str) -> Delivery:
        if not get_settings().sms_api_key:
            return Delivery(False, error="SMS_API_KEY not set")
        raise NotImplementedError("Wire the SMS gateway call here.")


MOCK_WHATSAPP = MockProvider("whatsapp")
MOCK_SMS = MockProvider("sms")


def channels() -> tuple[MessagingProvider, MessagingProvider]:
    """(primary, fallback)."""
    if get_settings().messaging_provider == "mock":
        return MOCK_WHATSAPP, MOCK_SMS
    return WhatsAppCloudStub(), SmsGatewayStub()


def reset_mock() -> None:
    for p in (MOCK_WHATSAPP, MOCK_SMS):
        p.outbox.clear()
        p.fail_for.clear()
