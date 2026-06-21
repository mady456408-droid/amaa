"""Telethon (MTProto) listener for source channels — pushes to the existing worker queue."""

import asyncio
import logging
from dataclasses import dataclass, field

from telethon import TelegramClient, events
from telethon.tl.types import MessageEntityTextUrl, MessageEntityUrl

from config import (
    TELEGRAM_API_HASH,
    TELEGRAM_API_ID,
    TELEGRAM_PHONE,
    TELEGRAM_SESSION_NAME,
)
from link_resolver import extract_all_urls_from_text
from telethon_auth import LOGIN_IDLE, is_telethon_connected, session_exists

logger = logging.getLogger(__name__)


@dataclass
class QueuedChannelMessage:
    """Minimal message shape for the existing worker and link_resolver."""

    message_id: int
    chat_id: int
    text: str | None = None
    caption: str | None = None
    entities: tuple = field(default_factory=tuple)
    caption_entities: tuple = field(default_factory=tuple)
    urls: list[str] = field(default_factory=list)


def _normalize_channel_id(chat_id: int) -> int:
    """Align Telethon chat ids with Bot API-style channel ids stored in SQLite."""
    if chat_id > 0:
        return int(f"-100{chat_id}")
    return chat_id


def _extract_urls_from_telethon(message) -> list[str]:
    text = message.message or ""
    seen: set[str] = set()
    urls: list[str] = []

    for url in extract_all_urls_from_text(text):
        key = url.lower()
        if key not in seen:
            seen.add(key)
            urls.append(url)

    for ent in message.entities or []:
        if isinstance(ent, MessageEntityTextUrl) and ent.url:
            url = ent.url.strip()
            key = url.lower()
            if key and key not in seen:
                seen.add(key)
                urls.append(url)
        elif isinstance(ent, MessageEntityUrl) and text:
            url = text[ent.offset : ent.offset + ent.length].strip()
            key = url.lower()
            if key and key not in seen:
                seen.add(key)
                urls.append(url)

    return urls


def telethon_message_to_queued(message, chat_id: int) -> QueuedChannelMessage:
    text = message.message or ""
    return QueuedChannelMessage(
        message_id=message.id,
        chat_id=_normalize_channel_id(chat_id),
        text=text,
        caption=None,
        urls=_extract_urls_from_telethon(message),
    )


async def _on_new_message(event, application) -> None:
    if not event.is_channel:
        return
    if event.out:
        return

    message = event.message
    if not message:
        return

    chat_id = _normalize_channel_id(event.chat_id)
    message_id = message.id

    logger.info(
        "MESSAGE RECEIVED chat_id=%s message_id=%s",
        chat_id,
        message_id,
    )

    if not application.bot_data.get("ready"):
        logger.error("Pipeline not ready — dropping message_id=%s", message_id)
        return

    if not is_telethon_connected(application):
        return

    if application.bot_data.get("paused"):
        logger.info("Bot paused — ignoring message_id=%s", message_id)
        return

    active_sources: set[int] = application.bot_data.get("active_source_ids", set())
    if chat_id not in active_sources:
        logger.debug(
            "Ignored post from chat_id=%s (not in active sources)",
            chat_id,
        )
        return

    queued = telethon_message_to_queued(message, event.chat_id)
    if not queued.urls:
        logger.info("No URL in post %s — ignoring", message_id)
        return

    queue: asyncio.Queue = application.bot_data["queue"]
    await queue.put(queued)
    logger.info("QUEUE PUSH SUCCESS message_id=%s", message_id)


async def attach_listener(application) -> None:
    if application.bot_data.get("telethon_handler_registered"):
        return

    client: TelegramClient = application.bot_data["telethon_client"]

    async def handler(event) -> None:
        await _on_new_message(event, application)

    client.add_event_handler(handler, events.NewMessage())
    application.bot_data["telethon_handler_registered"] = True

    sources = application.bot_data.get("active_source_ids", set())
    logger.info("Listening to source channels: %s", sorted(sources) if sources else "none")


async def start_telethon_listener(application) -> None:
    if not TELEGRAM_API_ID or not TELEGRAM_API_HASH:
        raise RuntimeError("Set TELEGRAM_API_ID and TELEGRAM_API_HASH")

    client = TelegramClient(TELEGRAM_SESSION_NAME, TELEGRAM_API_ID, TELEGRAM_API_HASH)
    application.bot_data["telethon_client"] = client
    application.bot_data["telethon_handler_registered"] = False

    await client.connect()

    if await client.is_user_authorized():
        await attach_listener(application)
        application.bot_data["telethon_connected"] = True
        logger.info("Telethon connected")
        if session_exists():
            logger.info("Reusing Telethon session: %s.session", TELEGRAM_SESSION_NAME)
    else:
        application.bot_data["telethon_connected"] = False
        application.bot_data["telethon_login_flow"] = LOGIN_IDLE
        logger.warning(
            "Telethon login required — open /admin and tap Start Telethon Login"
        )


async def stop_telethon_listener(application) -> None:
    client: TelegramClient | None = application.bot_data.get("telethon_client")
    if client:
        await client.disconnect()
        logger.info("Telethon disconnected")
    application.bot_data.pop("telethon_client", None)
    application.bot_data.pop("telethon_handler_registered", None)
    application.bot_data.pop("telethon_connected", None)
