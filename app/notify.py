"""Operator notification of new access requests (ADR 0010) via Telegram.

The message holds the request ID, name, client type, intended use, and competitions; never the
contact email or the free-text message. Sending runs after the request is stored, never fails the
request, and is counted in `football_results_access_requests_total` by outcome.
"""

import html
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass

import httpx
from pydantic import SecretStr

from app import observability
from app.config import Settings, telegram_errors
from app.observability import AccessRequestOutcome
from app.repository import StoredAccessRequest

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"
TIMEOUT_SECONDS = 5.0
_TOKEN_IN_URL = re.compile(re.escape(TELEGRAM_API) + r"/bot[^/?#\s]+/")


class _RedactTelegramToken(logging.Filter):
    """Hide the bot token in httpx's request log lines (the token is part of the URL path)."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if TELEGRAM_API in message:
            record.msg = _TOKEN_IN_URL.sub(f"{TELEGRAM_API}/bot***/", message)
            record.args = ()
        return True


logging.getLogger("httpx").addFilter(_RedactTelegramToken())


@dataclass(frozen=True)
class TelegramConfig:
    bot_token: SecretStr
    chat_id: str

    @classmethod
    def from_settings(cls, settings: Settings) -> "TelegramConfig | None":
        """The configured chat, or None when unset or invalid (`config check` reports why)."""
        if settings.telegram_bot_token is None or telegram_errors(settings):
            return None
        assert settings.telegram_chat_id is not None
        return cls(settings.telegram_bot_token, settings.telegram_chat_id)


def _escape(value: str) -> str:
    return html.escape(value, quote=False)


def access_request_message(request: StoredAccessRequest, instance: str | None = None) -> str:
    """The Telegram text (HTML parse mode) for a new request, with every user value escaped."""
    competitions: Sequence[str] = request.competitions or ()
    lines = [
        f"<b>New access request #{request.id}</b>",
        f"<b>Name:</b> {_escape(request.name or '')}",
        f"<b>Client type:</b> {_escape(request.client_type)}",
        f"<b>Intended use:</b> {_escape(request.intended_use or '')}",
        f"<b>Competitions:</b> {_escape(', '.join(competitions)) if competitions else 'none listed'}",
    ]
    if instance:
        lines.append(f"<b>Instance:</b> {_escape(instance)}")
    return "\n".join(lines)


def create_http_client() -> httpx.AsyncClient:
    """The HTTP client for the Telegram Bot API (a test seam)."""
    return httpx.AsyncClient(timeout=TIMEOUT_SECONDS, follow_redirects=False)


async def notify_access_request(
    request: StoredAccessRequest, config: TelegramConfig | None, instance: str | None = None
) -> AccessRequestOutcome:
    """Tell the operator about a stored request; logs and counts the outcome, never raises."""
    if config is None:
        outcome: AccessRequestOutcome = "not_configured"
    else:
        failure = await _send(config, access_request_message(request, instance))
        outcome = "failed" if failure else "notified"
    observability.record_access_request(outcome)
    if outcome == "failed":
        logger.warning(
            "Access request %d created (client type %s); Telegram notification failed: %s",
            request.id,
            request.client_type,
            failure,
        )
    else:
        logger.info(
            "Access request %d created (client type %s); %s",
            request.id,
            request.client_type,
            "operator notified via Telegram"
            if outcome == "notified"
            else "Telegram notifications are not configured",
        )
    return outcome


async def _send(config: TelegramConfig, text: str) -> str | None:
    """None on success, else a reason that names neither the token nor the message."""
    url = f"{TELEGRAM_API}/bot{config.bot_token.get_secret_value()}/sendMessage"
    payload = {
        "chat_id": config.chat_id,
        "text": text,
        "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": True},
    }
    try:
        async with create_http_client() as http:
            response = await http.post(url, json=payload)
    except Exception as error:  # noqa: BLE001 - a notification must never fail the request
        # Only the class: httpx messages can contain the URL, and with it the token.
        return type(error).__name__
    if response.is_success:
        return None
    return f"HTTP {response.status_code}{_telegram_description(response)}"


def _telegram_description(response: httpx.Response) -> str:
    try:
        description = response.json().get("description")
    except (ValueError, AttributeError):
        return ""
    return f" ({str(description)[:200]})" if description else ""
