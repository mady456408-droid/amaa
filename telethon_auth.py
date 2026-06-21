"""Telethon login through the admin bot (no terminal interaction)."""

import logging
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

from config import (
    TELEGRAM_API_HASH,
    TELEGRAM_API_ID,
    TELEGRAM_PHONE,
    TELEGRAM_SESSION_NAME,
)

logger = logging.getLogger(__name__)

LOGIN_IDLE = "idle"
LOGIN_AWAITING_CODE = "awaiting_code"
LOGIN_AWAITING_PASSWORD = "awaiting_password"
LOGIN_CONNECTED = "connected"

# Legacy keys used by conversation handlers
AUTH_STATE_CODE = "await_code"
AUTH_STATE_PASSWORD = "await_password"


def session_file_path() -> Path:
    return Path(f"{TELEGRAM_SESSION_NAME}.session")


def session_exists() -> bool:
    return session_file_path().is_file()


def is_telethon_connected(application) -> bool:
    return bool(application.bot_data.get("telethon_connected"))


def get_login_flow(application) -> str:
    if is_telethon_connected(application):
        return LOGIN_CONNECTED
    return application.bot_data.get("telethon_login_flow", LOGIN_IDLE)


def set_login_flow(application, flow: str) -> None:
    application.bot_data["telethon_login_flow"] = flow
    if flow == LOGIN_AWAITING_CODE:
        application.bot_data["telethon_auth_state"] = AUTH_STATE_CODE
    elif flow == LOGIN_AWAITING_PASSWORD:
        application.bot_data["telethon_auth_state"] = AUTH_STATE_PASSWORD
    else:
        application.bot_data.pop("telethon_auth_state", None)


async def get_client(application) -> TelegramClient:
    client: TelegramClient | None = application.bot_data.get("telethon_client")
    if client is None:
        if not TELEGRAM_API_ID or not TELEGRAM_API_HASH:
            raise RuntimeError("Set TELEGRAM_API_ID and TELEGRAM_API_HASH")
        client = TelegramClient(TELEGRAM_SESSION_NAME, TELEGRAM_API_ID, TELEGRAM_API_HASH)
        application.bot_data["telethon_client"] = client
    if not client.is_connected():
        await client.connect()
    return client


def clear_auth_state(application) -> None:
    application.bot_data.pop("telethon_phone_hash", None)
    application.bot_data.pop("telethon_auth_admin_id", None)
    application.bot_data.pop("telethon_auth_state", None)
    if not is_telethon_connected(application):
        set_login_flow(application, LOGIN_IDLE)


async def complete_login(application) -> None:
    from telegram_listener import attach_listener

    await attach_listener(application)
    application.bot_data["telethon_connected"] = True
    application.bot_data.pop("telethon_phone_hash", None)
    application.bot_data.pop("telethon_auth_admin_id", None)
    application.bot_data.pop("telethon_auth_state", None)
    application.bot_data["telethon_login_flow"] = LOGIN_CONNECTED
    logger.info("Telethon connected")


async def begin_login(application, admin_user_id: int) -> str | None:
    """
    Send Telegram verification code.
    Returns: None (code sent), already_connected, code_already_sent, or error text.
    """
    if not TELEGRAM_PHONE:
        return "Set TELEGRAM_PHONE in .env first."

    flow = get_login_flow(application)
    if flow == LOGIN_CONNECTED:
        return "already_connected"

    if flow == LOGIN_AWAITING_CODE:
        return "code_already_sent"

    if flow == LOGIN_AWAITING_PASSWORD:
        return "code_already_sent"

    client = await get_client(application)
    if await client.is_user_authorized():
        if not is_telethon_connected(application):
            await complete_login(application)
        return "already_connected"

    sent = await client.send_code_request(TELEGRAM_PHONE)
    application.bot_data["telethon_phone_hash"] = sent.phone_code_hash
    application.bot_data["telethon_auth_admin_id"] = admin_user_id
    set_login_flow(application, LOGIN_AWAITING_CODE)
    logger.info("Telethon verification code requested")
    return None


async def submit_code(application, code: str) -> tuple[str, bool]:
    """Returns (reply message, login_complete)."""
    client = await get_client(application)
    phone_hash = application.bot_data.get("telethon_phone_hash")
    if not phone_hash:
        set_login_flow(application, LOGIN_IDLE)
        return "Login session expired. Start login again from /admin.", False

    try:
        await client.sign_in(
            phone=TELEGRAM_PHONE,
            code=code.strip(),
            phone_code_hash=phone_hash,
        )
        await complete_login(application)
        return "✅ Telethon connected successfully", True
    except SessionPasswordNeededError:
        set_login_flow(application, LOGIN_AWAITING_PASSWORD)
        return "Enter your Telegram password (2FA):", False
    except Exception as exc:
        logger.warning("Telethon sign_in failed: %s", type(exc).__name__)
        return f"Login failed: {exc}", False


async def submit_password(application, password: str) -> tuple[str, bool]:
    """Returns (reply message, login_complete)."""
    client = await get_client(application)
    try:
        await client.sign_in(password=password)
        await complete_login(application)
        return "✅ Telethon connected successfully", True
    except Exception as exc:
        logger.warning("Telethon 2FA sign_in failed: %s", type(exc).__name__)
        return f"Login failed: {exc}", False


async def delete_sensitive_message(bot, chat_id: int, message_id: int) -> None:
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass
