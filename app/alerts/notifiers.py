"""Alert delivery channels.

Three channels, one interface. Two security notes:

* **Webhooks are outbound requests to a user-supplied URL**, which is textbook
  SSRF. They go through the same :class:`UrlGuard` as ingestion, so a webhook
  pointing at ``http://169.254.169.254/`` is rejected before a socket opens.
* **Notification bodies are data, not markup.** They are plain text, so an
  alert cannot become an injection vector in whatever renders it.
"""

from __future__ import annotations

import asyncio
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Protocol, runtime_checkable

import httpx

from app.core.config import AlertSettings, IngestionSettings
from app.core.errors import AlertDeliveryError, UnsafeUrlError
from app.core.logging import get_logger
from app.ingestion.http import UrlGuard
from app.models.alerts import Notification
from app.models.enums import AlertChannel

log = get_logger(__name__)

MAX_BODY_CHARS = 4000


@runtime_checkable
class Notifier(Protocol):
    """One delivery channel."""

    channel: AlertChannel

    async def send(self, notification: Notification) -> Notification: ...


@dataclass(slots=True)
class InAppNotifier:
    """Stores the notification for retrieval through the API.

    Delivery is the write itself; the alert service persists the record.
    """

    channel: AlertChannel = AlertChannel.IN_APP

    async def send(self, notification: Notification) -> Notification:
        return notification.model_copy(update={"delivered": True, "error": None})


class WebhookNotifier:
    """POSTs a JSON document to a validated URL."""

    channel = AlertChannel.WEBHOOK

    def __init__(
        self,
        settings: AlertSettings,
        *,
        client: httpx.AsyncClient | None = None,
        guard: UrlGuard | None = None,
    ) -> None:
        self._settings = settings
        self._client = client
        self._owns_client = client is None
        self._guard = guard or UrlGuard(
            IngestionSettings(
                allow_private_networks=settings.webhook_allow_private_networks,
                allowed_schemes=("https",)
                if not settings.webhook_allow_private_networks
                else ("http", "https"),
                request_timeout_seconds=settings.webhook_timeout_seconds,
            )
        )

    async def send(self, notification: Notification) -> Notification:
        url = str(notification.context.get("webhook_url") or "")
        if not url:
            return notification.model_copy(
                update={"delivered": False, "error": "no webhook_url in context"}
            )
        try:
            await self._guard.validate(url)
        except UnsafeUrlError as exc:
            log.warning("alerts.webhook_rejected", error=exc.message)
            return notification.model_copy(
                update={"delivered": False, "error": f"unsafe webhook url: {exc.message}"}
            )

        payload = {
            "id": notification.id,
            "rule_id": notification.rule_id,
            "title": notification.title,
            "body": notification.body[:MAX_BODY_CHARS],
            "created_at": notification.created_at.isoformat(),
            "context": {
                key: value for key, value in notification.context.items() if key != "webhook_url"
            },
        }

        client = self._client or httpx.AsyncClient(
            timeout=self._settings.webhook_timeout_seconds, follow_redirects=False
        )
        try:
            response = await client.post(
                url, json=payload, headers={"Content-Type": "application/json"}
            )
            if response.status_code >= 400:
                return notification.model_copy(
                    update={
                        "delivered": False,
                        "error": f"webhook returned HTTP {response.status_code}",
                    }
                )
            return notification.model_copy(update={"delivered": True, "error": None})
        except httpx.HTTPError as exc:
            return notification.model_copy(
                update={"delivered": False, "error": f"webhook failed: {exc}"[:512]}
            )
        finally:
            if self._owns_client:
                await client.aclose()


class EmailNotifier:
    """Sends a plain-text e-mail over SMTP."""

    channel = AlertChannel.EMAIL

    def __init__(self, settings: AlertSettings) -> None:
        self._settings = settings

    @property
    def configured(self) -> bool:
        return bool(self._settings.smtp_host)

    async def send(self, notification: Notification) -> Notification:
        recipient = str(notification.context.get("email_to") or "")
        if not recipient:
            return notification.model_copy(update={"delivered": False, "error": "no recipient"})
        if not self.configured:
            return notification.model_copy(
                update={"delivered": False, "error": "SMTP is not configured"}
            )

        message = EmailMessage()
        message["Subject"] = notification.title[:200]
        message["From"] = self._settings.smtp_from_address
        message["To"] = recipient
        message.set_content(notification.body[:MAX_BODY_CHARS])

        try:
            # smtplib is blocking; a worker thread keeps the event loop free.
            await asyncio.to_thread(self._deliver, message)
        except (OSError, smtplib.SMTPException) as exc:
            log.warning("alerts.email_failed", error=str(exc))
            return notification.model_copy(
                update={"delivered": False, "error": f"smtp failed: {exc}"[:512]}
            )
        return notification.model_copy(update={"delivered": True, "error": None})

    def _deliver(self, message: EmailMessage) -> None:
        settings = self._settings
        host = settings.smtp_host or ""
        with smtplib.SMTP(host, settings.smtp_port, timeout=20) as server:
            if settings.smtp_use_tls:
                server.starttls()
            if settings.smtp_username and settings.smtp_password:
                server.login(settings.smtp_username, settings.smtp_password.get_secret_value())
            server.send_message(message)


class NotifierRegistry:
    """Routes a notification to the notifier for its channel."""

    def __init__(self, notifiers: list[Notifier] | None = None) -> None:
        self._notifiers: dict[AlertChannel, Notifier] = {}
        for notifier in notifiers or []:
            self._notifiers[notifier.channel] = notifier

    @classmethod
    def default(cls, settings: AlertSettings) -> NotifierRegistry:
        """The standard channel set."""
        return cls([InAppNotifier(), WebhookNotifier(settings), EmailNotifier(settings)])

    def register(self, notifier: Notifier) -> None:
        self._notifiers[notifier.channel] = notifier

    def get(self, channel: AlertChannel) -> Notifier | None:
        return self._notifiers.get(channel)

    async def deliver(self, notification: Notification) -> Notification:
        """Send through the notification's channel, or mark it undeliverable."""
        notifier = self._notifiers.get(notification.channel)
        if notifier is None:
            raise AlertDeliveryError(
                f"no notifier registered for channel {notification.channel}",
                details={"channel": str(notification.channel)},
            )
        return await notifier.send(notification)


__all__ = [
    "MAX_BODY_CHARS",
    "EmailNotifier",
    "InAppNotifier",
    "Notifier",
    "NotifierRegistry",
    "WebhookNotifier",
]
