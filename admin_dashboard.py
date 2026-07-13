import logging
import re
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from config import ADMIN_USER_IDS, AMAZON_DOMAIN
from ai_caption import (
    MODE_ARABIC,
    MODE_CONSERVATIVE,
    MODE_CUSTOM,
    MODE_FIXED_TEMPLATE,
    MODE_MARKETING,
    MODE_OFF,
)
from backup_restore import (
    apply_restore_and_restart,
    create_backup_archive,
    validate_backup_zip,
)
from conversation_states import (
    AWAIT_AI_CUSTOM,
    AWAIT_DESTINATION_ID,
    AWAIT_DRAFT_CAPTION,
    AWAIT_FORWARD,
    AWAIT_MANUAL_INPUT,
    AWAIT_RESTORE_UPLOAD,
    AWAIT_SOURCE_ID,
    AWAIT_AFFILIATE_TAG_VALUE,
    AWAIT_TELETHON_CODE,
    AWAIT_TELETHON_PASSWORD,
    AWAIT_CUSTOM_IMAGE_POST,
    AWAIT_FIXED_BUTTON_TITLE,
    AWAIT_FIXED_BUTTON_URL,
)
from telethon_auth import (
    AUTH_STATE_CODE,
    AUTH_STATE_PASSWORD,
    begin_login,
    clear_auth_state,
    delete_sensitive_message,
    is_telethon_connected,
    submit_code,
    submit_password,
)
from database import Database
from affiliate_tag import apply_affiliate_tag, is_valid_affiliate_tag, set_affiliate_settings
from manual_posts import (
    UD_EDITING_DRAFT,
    UD_MANUAL_MODE,
    handle_edit_draft,
    manual_state_handlers,
)
from custom_image_post import (
    custom_image_state_handlers,
    custom_image_callback_handlers,
)

logger = logging.getLogger(__name__)

CB_MAIN = "adm:main"
CB_ADD = "adm:add"
CB_ADD_MANUAL = "adm:add:manual"
CB_ADD_CURRENT = "adm:add:current"
CB_REMOVE = "adm:remove"
CB_REMOVE_ITEM = "adm:rm:"
CB_LIST = "adm:list"
CB_DEST = "adm:dest"
CB_DEST_MANUAL = "adm:dest:manual"
CB_DEST_CURRENT = "adm:dest:current"
CB_PAUSE = "adm:pause"
CB_RESUME = "adm:resume"
CB_STATUS = "adm:status"
CB_MANUAL = "adm:manual"
CB_AI = "adm:ai"
CB_AI_OFF = "adm:ai:off"
CB_AI_CONSERVATIVE = "adm:ai:conservative"
CB_AI_MARKETING = "adm:ai:marketing"
CB_AI_ARABIC = "adm:ai:arabic"
CB_AI_CUSTOM = "adm:ai:custom"
CB_AI_FIXED_TEMPLATE = "adm:ai:fixed"
CB_AFFILIATE_SETTINGS = "adm:affiliate"
CB_AFFILIATE_ON = "adm:affiliate:on"
CB_AFFILIATE_OFF = "adm:affiliate:off"
CB_AFFILIATE_CHANGE = "adm:affiliate:change"
CB_AFFILIATE_TEST = "adm:affiliate:test"
CB_COUPON_SETTINGS = "adm:coupon"
CB_COUPON_ON = "adm:coupon:on"
CB_COUPON_OFF = "adm:coupon:off"
CB_TELETHON_START = "adm:telethon:start"
CB_BACKUP = "adm:backup"
CB_RESTORE = "adm:restore"
CB_RESTORE_CONFIRM = "adm:restore:yes"
CB_RESTORE_CANCEL = "adm:restore:no"
CB_CUSTOM_IMAGE_POST = "adm:custom_image_post"
CB_INLINE_BUTTONS = "adm:inline_buttons"
CB_PRODUCT_BUTTONS_TOGGLE = "adm:inline_buttons:product_toggle"
CB_FIXED_BUTTONS_LIST = "adm:inline_buttons:fixed_list"
CB_FIXED_BUTTONS_ADD = "adm:inline_buttons:fixed_add"
CB_FIXED_BUTTONS_EDIT = "adm:inline_buttons:fixed_edit:"
CB_FIXED_BUTTONS_DELETE = "adm:inline_buttons:fixed_delete:"
CB_FIXED_BUTTONS_ENABLE = "adm:inline_buttons:fixed_enable:"
CB_FIXED_BUTTONS_DISABLE = "adm:inline_buttons:fixed_disable:"
CB_FIXED_BUTTONS_UP = "adm:inline_buttons:fixed_up:"
CB_FIXED_BUTTONS_DOWN = "adm:inline_buttons:fixed_down:"
CB_FIXED_POSITION_TOP = "adm:inline_buttons:fixed_pos:top"
CB_FIXED_POSITION_BOTTOM = "adm:inline_buttons:fixed_pos:bottom"
CB_PRODUCT_LAYOUT_VERTICAL = "adm:inline_buttons:prod_layout:vertical"
CB_PRODUCT_LAYOUT_TWO_COLUMNS = "adm:inline_buttons:prod_layout:two_columns"
CB_PRODUCT_TEMPLATE_SET = "adm:inline_buttons:prod_template:set"
CB_MAX_PRODUCT_1 = "adm:inline_buttons:max_prod:1"
CB_MAX_PRODUCT_2 = "adm:inline_buttons:max_prod:2"
CB_MAX_PRODUCT_3 = "adm:inline_buttons:max_prod:3"
CB_MAX_PRODUCT_4 = "adm:inline_buttons:max_prod:4"
CB_MAX_PRODUCT_5 = "adm:inline_buttons:max_prod:5"

UD_PENDING_RESTORE = "pending_restore_zip"


def _db(context: ContextTypes.DEFAULT_TYPE) -> Database:
    return context.application.bot_data["db"]


def is_admin(user_id: int | None) -> bool:
    return user_id is not None and user_id in ADMIN_USER_IDS


async def _deny(update: Update) -> None:
    text = "Unauthorized. Admin access only."
    if update.callback_query:
        await update.callback_query.answer(text, show_alert=True)
    elif update.message:
        await update.message.reply_text(text)


async def _safe_edit_message_text(query, text: str, **kwargs) -> None:
    """Edit callback message; ignore Telegram 'Message is not modified' errors."""
    try:
        await query.edit_message_text(text, **kwargs)
    except BadRequest as exc:
        if "message is not modified" in str(exc).lower():
            return
        raise


def _main_keyboard(paused: bool, telethon_connected: bool = True) -> InlineKeyboardMarkup:
    pause_btn = (
        InlineKeyboardButton("▶ Resume Bot", callback_data=CB_RESUME)
        if paused
        else InlineKeyboardButton("⏸ Pause Bot", callback_data=CB_PAUSE)
    )
    rows = []
    if not telethon_connected:
        rows.append(
            [InlineKeyboardButton("🔑 Start Telethon Login", callback_data=CB_TELETHON_START)]
        )
    rows.extend(
        [
            [
                InlineKeyboardButton("➕ Add Source", callback_data=CB_ADD),
                InlineKeyboardButton("➖ Remove Source", callback_data=CB_REMOVE),
            ],
            [
                InlineKeyboardButton("📋 List Sources", callback_data=CB_LIST),
                InlineKeyboardButton("🎯 Set Destination", callback_data=CB_DEST),
            ],
            [
                pause_btn,
                InlineKeyboardButton("📊 Status", callback_data=CB_STATUS),
            ],
            [
                InlineKeyboardButton("🛠 Manual Post", callback_data=CB_MANUAL),
                InlineKeyboardButton("🤖 AI Caption", callback_data=CB_AI),
            ],
            [
                InlineKeyboardButton("🖼 Custom Image Post", callback_data=CB_CUSTOM_IMAGE_POST),
            ],
            [
                InlineKeyboardButton(
                    "🔗 Affiliate Tag Settings",
                    callback_data=CB_AFFILIATE_SETTINGS,
                ),
                InlineKeyboardButton(
                    "🎟 Coupon Detection",
                    callback_data=CB_COUPON_SETTINGS,
                ),
            ],
            [
                InlineKeyboardButton(
                    "🔘 Inline Buttons",
                    callback_data=CB_INLINE_BUTTONS,
                ),
            ],
            [
                InlineKeyboardButton("💾 Backup", callback_data=CB_BACKUP),
                InlineKeyboardButton("♻ Restore Backup", callback_data=CB_RESTORE),
            ],
        ]
    )
    return InlineKeyboardMarkup(rows)


def _restore_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Confirm", callback_data=CB_RESTORE_CONFIRM),
                InlineKeyboardButton("❌ Cancel", callback_data=CB_RESTORE_CANCEL),
            ],
        ]
    )


def _keyboard_for_app(app, paused: bool | None = None) -> InlineKeyboardMarkup:
    if paused is None:
        paused = app.bot_data.get("paused", False)
    return _main_keyboard(paused, is_telethon_connected(app))


async def _inline_buttons_menu_text(db: Database) -> str:
    product_enabled = db.get_product_buttons_enabled()
    product_status = "✅ ON" if product_enabled else "❌ OFF"
    fixed_count = len(db.list_fixed_buttons(enabled_only=True))
    fixed_position = db.get_fixed_buttons_position()
    product_layout = db.get_product_button_layout()
    product_template = db.get_product_button_template()
    max_product_buttons = db.get_max_product_buttons()
    return (
        f"🔘 <b>Inline Buttons Settings</b>\n\n"
        f"<b>Product Buttons:</b> {product_status}\n"
        f"<b>Max Product Buttons:</b> {max_product_buttons}\n"
        f"<b>Fixed Buttons:</b> {fixed_count} enabled\n"
        f"<b>Fixed Position:</b> {fixed_position}\n"
        f"<b>Product Layout:</b> {product_layout}\n"
        f"<b>Product Template:</b> {product_template}\n\n"
        f"Product buttons add purchase links for each product.\n"
        f"Fixed buttons are always shown."
    )


def _inline_buttons_keyboard(db: Database) -> InlineKeyboardMarkup:
    product_enabled = db.get_product_buttons_enabled()
    product_status = "ON" if product_enabled else "OFF"
    fixed_position = db.get_fixed_buttons_position()
    product_layout = db.get_product_button_layout()
    max_product_buttons = db.get_max_product_buttons()

    # Build max product buttons row
    max_buttons_row = []
    for i in range(1, 6):
        label = str(i) if i != max_product_buttons else f"✓{i}"
        callback = globals()[f"CB_MAX_PRODUCT_{i}"]
        max_buttons_row.append(
            InlineKeyboardButton(label, callback_data=callback)
        )

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"🛒 Product Buttons: {product_status}",
                    callback_data=CB_PRODUCT_BUTTONS_TOGGLE,
                ),
            ],
            max_buttons_row,
            [
                InlineKeyboardButton(
                    f"📍 Fixed Position: {fixed_position}",
                    callback_data=CB_FIXED_POSITION_TOP if fixed_position == "BOTTOM" else CB_FIXED_POSITION_BOTTOM,
                ),
            ],
            [
                InlineKeyboardButton(
                    f"📐 Product Layout: {product_layout}",
                    callback_data=CB_PRODUCT_LAYOUT_TWO_COLUMNS if product_layout == "VERTICAL" else CB_PRODUCT_LAYOUT_VERTICAL,
                ),
            ],
            [
                InlineKeyboardButton(
                    "📝 Product Template",
                    callback_data=CB_PRODUCT_TEMPLATE_SET,
                ),
            ],
            [
                InlineKeyboardButton(
                    "📋 Fixed Buttons",
                    callback_data=CB_FIXED_BUTTONS_LIST,
                ),
            ],
            [InlineKeyboardButton("« Back", callback_data=CB_MAIN)],
        ]
    )


async def _fixed_buttons_list_text(db: Database) -> str:
    buttons = db.list_fixed_buttons()
    if not buttons:
        return "📋 <b>Fixed Buttons</b>\n\nNo fixed buttons configured."

    text = "📋 <b>Fixed Buttons</b>\n\n"
    for i, btn in enumerate(buttons, 1):
        status = "✅" if btn["enabled"] else "❌"
        text += f"{status} {i}. {btn['title']}\n"
        text += f"   {btn['url']}\n\n"

    return text


def _fixed_buttons_list_keyboard(db: Database) -> InlineKeyboardMarkup:
    buttons = db.list_fixed_buttons()
    rows = []

    for btn in buttons:
        btn_row = []
        # Edit button
        btn_row.append(
            InlineKeyboardButton(
                "✏️", callback_data=f"{CB_FIXED_BUTTONS_EDIT}{btn['id']}"
            )
        )
        # Enable/Disable button
        if btn["enabled"]:
            btn_row.append(
                InlineKeyboardButton(
                    "❌", callback_data=f"{CB_FIXED_BUTTONS_DISABLE}{btn['id']}"
                )
            )
        else:
            btn_row.append(
                InlineKeyboardButton(
                    "✅", callback_data=f"{CB_FIXED_BUTTONS_ENABLE}{btn['id']}"
                )
            )
        # Up/Down buttons
        btn_row.append(
            InlineKeyboardButton("⬆️", callback_data=f"{CB_FIXED_BUTTONS_UP}{btn['id']}")
        )
        btn_row.append(
            InlineKeyboardButton("⬇️", callback_data=f"{CB_FIXED_BUTTONS_DOWN}{btn['id']}")
        )
        # Delete button
        btn_row.append(
            InlineKeyboardButton(
                "🗑", callback_data=f"{CB_FIXED_BUTTONS_DELETE}{btn['id']}"
            )
        )
        rows.append(btn_row)

    rows.append([InlineKeyboardButton("➕ Add Button", callback_data=CB_FIXED_BUTTONS_ADD)])
    rows.append([InlineKeyboardButton("« Back", callback_data=CB_INLINE_BUTTONS)])
    return InlineKeyboardMarkup(rows)


async def _move_fixed_button(db: Database, button_id: int, direction: int) -> None:
    """Move a fixed button up or down by swapping sort_order with neighbor."""
    buttons = db.list_fixed_buttons()
    button_index = None
    for i, btn in enumerate(buttons):
        if btn["id"] == button_id:
            button_index = i
            break

    if button_index is None:
        return

    target_index = button_index + direction
    if target_index < 0 or target_index >= len(buttons):
        return

    # Swap sort_order
    current_btn = buttons[button_index]
    target_btn = buttons[target_index]
    current_order = current_btn["sort_order"]
    target_order = target_btn["sort_order"]

    db.update_fixed_button(button_id, sort_order=target_order)
    db.update_fixed_button(target_btn["id"], sort_order=current_order)


def _ai_mode_label(mode: str) -> str:
    labels = {
        MODE_OFF: "OFF",
        MODE_CONSERVATIVE: "Conservative",
        MODE_MARKETING: "Marketing",
        MODE_ARABIC: "Arabic Translate",
        MODE_CUSTOM: "Custom",
        MODE_FIXED_TEMPLATE: "Fixed Template",
    }
    return labels.get(mode, mode)


def _ai_caption_keyboard(current_mode: str) -> InlineKeyboardMarkup:
    def btn(label: str, cb: str, mode_key: str) -> InlineKeyboardButton:
        prefix = "✓ " if current_mode == mode_key else ""
        return InlineKeyboardButton(f"{prefix}{label}", callback_data=cb)

    return InlineKeyboardMarkup(
        [
            [btn("OFF", CB_AI_OFF, MODE_OFF)],
            [btn("Conservative", CB_AI_CONSERVATIVE, MODE_CONSERVATIVE)],
            [btn("Marketing", CB_AI_MARKETING, MODE_MARKETING)],
            [btn("Arabic Translate", CB_AI_ARABIC, MODE_ARABIC)],
            [btn("Custom", CB_AI_CUSTOM, MODE_CUSTOM)],
            [btn("Fixed Template", CB_AI_FIXED_TEMPLATE, MODE_FIXED_TEMPLATE)],
            [InlineKeyboardButton("« Back", callback_data=CB_MAIN)],
        ]
    )


async def _ai_caption_menu_text(db: Database) -> str:
    mode = db.get_ai_caption_mode()
    custom = db.get_ai_custom_prompt()
    lines = [
        "🤖 <b>AI Caption</b>\n",
        f"Current mode: <b>{_ai_mode_label(mode)}</b>\n",
        "Select a mode for new captions.\n",
        "OFF uses the standard scraped title format.",
    ]
    if mode == MODE_CUSTOM and custom:
        preview = custom[:200] + ("…" if len(custom) > 200 else "")
        lines.append(f"\nCustom instructions:\n<i>{preview}</i>")
    return "\n".join(lines)


def refresh_runtime_config(application) -> None:
    db: Database = application.bot_data["db"]
    application.bot_data["active_source_ids"] = db.get_active_channel_ids()
    application.bot_data["paused"] = db.is_paused()
    application.bot_data["destination_channel_id"] = db.get_destination_channel_id()
    # Keep affiliate tag helper in sync for worker threads/caption building.
    try:
        application.bot_data["affiliate_tag_enabled"] = db.get_affiliate_tag_enabled()
        application.bot_data["affiliate_tag_value"] = db.get_affiliate_tag_value()
        set_affiliate_settings(
            application.bot_data["affiliate_tag_enabled"],
            application.bot_data["affiliate_tag_value"],
        )
    except Exception:
        logger.exception("Failed to refresh affiliate tag settings")


async def _dashboard_text(application) -> str:
    db: Database = application.bot_data["db"]
    sources = db.list_sources(active_only=True)
    dest = db.get_destination_channel_id()
    paused = application.bot_data.get("paused", False)
    queue = application.bot_data.get("queue")
    qsize = queue.qsize() if queue else 0
    status = "PAUSED" if paused else "RUNNING"
    telethon_ok = is_telethon_connected(application)
    telethon_line = (
        "Telethon: <b>✅ Connected</b>"
        if telethon_ok
        else "Telethon: <b>🔐 Login required</b>"
    )
    lines = [
        "🛠 <b>Admin Dashboard</b>\n",
        f"Status: <b>{status}</b>",
        telethon_line,
        f"Active sources: <b>{len(sources)}</b>",
        f"Destination: <code>{dest or 'not set'}</code>",
        f"Queue size: <b>{qsize}</b>",
    ]
    if not telethon_ok:
        lines.append(
            "\n🔐 <b>Telethon Login Required</b>\n"
            "Source channels are listened via your Telegram user account.\n"
            "Tap <b>Start Telethon Login</b> below."
        )
    return "\n".join(lines)


def _affiliate_tag_menu_text(db: Database) -> str:
    enabled = db.get_affiliate_tag_enabled()
    value = db.get_affiliate_tag_value()
    status = "ON" if enabled else "OFF"
    if enabled and value:
        return (
            "🔗 <b>Affiliate Tag Settings</b>\n\n"
            f"Status: <b>{status}</b>\n"
            f"Current tag: <code>{value}</code>\n"
            "\n"
            "Toggle and set your Amazon affiliate tag."
        )
    if enabled and not value:
        return (
            "🔗 <b>Affiliate Tag Settings</b>\n\n"
            f"Status: <b>{status}</b>\n"
            "Current tag: <code>(empty)</code>\n\n"
            "Set a tag value below."
        )
    return (
        "🔗 <b>Affiliate Tag Settings</b>\n\n"
        f"Status: <b>{status}</b>\n"
        "Current tag: <code>(disabled)</code>\n"
        "\n"
        "Enable to append `tag=` to published/display links."
    )


def _coupon_detection_menu_text(db: Database) -> str:
    enabled = db.get_coupon_detection_enabled()
    status = "ON" if enabled else "OFF"
    return (
        "🎟 <b>Coupon Detection</b>\n\n"
        f"Status: <b>{status}</b>\n\n"
        "When ON, the scraper looks for Amazon coupons and adds a "
        "🎟 line to captions when found."
    )


def _coupon_detection_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Coupon Detection ON", callback_data=CB_COUPON_ON)],
            [InlineKeyboardButton("❌ Coupon Detection OFF", callback_data=CB_COUPON_OFF)],
            [InlineKeyboardButton("« Back", callback_data=CB_MAIN)],
        ]
    )


def _affiliate_tag_keyboard(db: Database) -> InlineKeyboardMarkup:
    enabled = db.get_affiliate_tag_enabled()
    on_btn = (
        InlineKeyboardButton("✅ Affiliate Tag: ON", callback_data=CB_AFFILIATE_ON)
        if not enabled
        else InlineKeyboardButton("✅ Affiliate Tag: ON", callback_data=CB_AFFILIATE_ON)
    )
    off_btn = (
        InlineKeyboardButton("❌ Affiliate Tag: OFF", callback_data=CB_AFFILIATE_OFF)
        if enabled
        else InlineKeyboardButton("❌ Affiliate Tag: OFF", callback_data=CB_AFFILIATE_OFF)
    )
    return InlineKeyboardMarkup(
        [
            [on_btn],
            [off_btn],
            [InlineKeyboardButton("✏ Change Tag", callback_data=CB_AFFILIATE_CHANGE)],
            [InlineKeyboardButton("🧪 Test Affiliate Link", callback_data=CB_AFFILIATE_TEST)],
            [InlineKeyboardButton("« Back", callback_data=CB_MAIN)],
        ]
    )


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    if not is_admin(user.id if user else None):
        await _deny(update)
        return ConversationHandler.END

    refresh_runtime_config(context.application)
    paused = context.application.bot_data.get("paused", False)
    app = context.application
    await update.message.reply_text(
        await _dashboard_text(app),
        reply_markup=_keyboard_for_app(app, paused),
        parse_mode="HTML",
    )
    return ConversationHandler.END


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    if not is_admin(user.id if user else None):
        await _deny(update)
        return ConversationHandler.END

    app = context.application
    db = _db(context)
    data = query.data or ""
    paused = app.bot_data.get("paused", False)

    if data == CB_MAIN:
        refresh_runtime_config(app)
        await _safe_edit_message_text(
            query,
            await _dashboard_text(app),
            reply_markup=_keyboard_for_app(app),
            parse_mode="HTML",
        )
        return ConversationHandler.END

    if data == CB_BACKUP:
        chat_id = user.id if user else None
        if not chat_id:
            return ConversationHandler.END
        zip_path = None
        try:
            zip_path = await create_backup_archive()
            with open(zip_path, "rb") as archive:
                await context.bot.send_document(
                    chat_id=chat_id,
                    document=archive,
                    filename=zip_path.name,
                    caption="📦 Amazon bot backup",
                )
            await query.message.reply_text("✅ Backup sent to this chat.")
        except Exception as exc:
            logger.exception("Backup failed")
            await query.message.reply_text(f"Backup failed: {exc}")
        finally:
            if zip_path and zip_path.exists():
                zip_path.unlink(missing_ok=True)
        return ConversationHandler.END

    if data == CB_RESTORE:
        context.user_data.pop(UD_PENDING_RESTORE, None)
        app.bot_data.pop("pending_restore_zip", None)
        await query.message.reply_text(
            "♻ <b>Restore Backup</b>\n\n"
            "Upload your <code>amazon_bot_backup_*.zip</code> file.\n"
            "/cancel to abort.",
            parse_mode="HTML",
        )
        return AWAIT_RESTORE_UPLOAD

    if data == CB_RESTORE_CONFIRM:
        zip_path = context.user_data.get(UD_PENDING_RESTORE) or app.bot_data.get(
            "pending_restore_zip"
        )
        if not zip_path:
            await query.answer("No backup pending.", show_alert=True)
            return ConversationHandler.END
        path = Path(zip_path)
        await _safe_edit_message_text(
            query,
            "♻ Restoring backup and restarting…\n\n"
            "You will see progress updates here.",
            parse_mode="HTML",
        )
        admin_id = user.id if user else None
        try:
            await apply_restore_and_restart(app, path, admin_id)
        finally:
            if path.exists():
                path.unlink(missing_ok=True)
            context.user_data.pop(UD_PENDING_RESTORE, None)
            app.bot_data.pop("pending_restore_zip", None)
        return ConversationHandler.END

    if data == CB_RESTORE_CANCEL:
        zip_path = context.user_data.pop(UD_PENDING_RESTORE, None)
        app.bot_data.pop("pending_restore_zip", None)
        if zip_path:
            Path(zip_path).unlink(missing_ok=True)
        await _safe_edit_message_text(
            query,
            "❌ Restore cancelled.",
            reply_markup=_keyboard_for_app(app),
            parse_mode="HTML",
        )
        return ConversationHandler.END

    if data == CB_TELETHON_START:
        user_id = user.id if user else None
        err = await begin_login(app, user_id or 0)
        if err == "already_connected":
            await _safe_edit_message_text(
                query,
                await _dashboard_text(app),
                reply_markup=_keyboard_for_app(app),
                parse_mode="HTML",
            )
            await query.message.reply_text("✅ Telethon is already connected.")
            return ConversationHandler.END
        if err == "code_already_sent":
            await query.answer(
                "Verification code already sent. Please enter it.",
                show_alert=True,
            )
            return AWAIT_TELETHON_CODE
        if err:
            await query.answer(err, show_alert=True)
            return ConversationHandler.END
        await query.message.reply_text(
            "Enter the verification code sent to your Telegram account.\n/cancel to abort."
        )
        return AWAIT_TELETHON_CODE

    if data == CB_ADD:
        await _safe_edit_message_text(
            query,
            "➕ <b>Add source channel</b>",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "Add Current Channel", callback_data=CB_ADD_CURRENT
                        ),
                    ],
                    [
                        InlineKeyboardButton(
                            "Enter Channel ID", callback_data=CB_ADD_MANUAL
                        ),
                    ],
                    [InlineKeyboardButton("« Back", callback_data=CB_MAIN)],
                ]
            ),
            parse_mode="HTML",
        )
        return ConversationHandler.END

    if data == CB_ADD_MANUAL:
        await _safe_edit_message_text(
            query,
            "Send the source channel ID (e.g. <code>-1001234567890</code>).\n"
            "/cancel to abort.",
            parse_mode="HTML",
        )
        return AWAIT_SOURCE_ID

    if data == CB_ADD_CURRENT:
        chat = query.message.chat if query.message else None
        if chat and chat.type in ("channel", "supergroup", "group"):
            name = chat.title or str(chat.id)
            if db.add_source(chat.id, name):
                refresh_runtime_config(app)
                await _safe_edit_message_text(
            query,
                    f"✅ Added source <b>{name}</b>\n<code>{chat.id}</code>",
                    reply_markup=_keyboard_for_app(app, paused),
                    parse_mode="HTML",
                )
            else:
                await _safe_edit_message_text(
            query,
                    "Channel already exists.",
                    reply_markup=_keyboard_for_app(app, paused),
                )
            return ConversationHandler.END

        context.user_data["forward_mode"] = "source"
        await _safe_edit_message_text(
            query,
            "Forward a post from the source channel here,\n"
            "or send the channel ID.\n/cancel to abort.",
        )
        return AWAIT_FORWARD

    if data == CB_REMOVE:
        sources = db.list_sources()
        if not sources:
            await _safe_edit_message_text(
            query,
                "No source channels.",
                reply_markup=_keyboard_for_app(app, paused),
            )
            return ConversationHandler.END
        rows = [
            [
                InlineKeyboardButton(
                    f"{'✅' if s['active'] else '⏸'} {s['channel_name']}",
                    callback_data=f"{CB_REMOVE_ITEM}{s['channel_id']}",
                )
            ]
            for s in sources
        ]
        rows.append([InlineKeyboardButton("« Back", callback_data=CB_MAIN)])
        await _safe_edit_message_text(
            query,
            "➖ Tap a channel to remove:",
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return ConversationHandler.END

    if data.startswith(CB_REMOVE_ITEM):
        channel_id = int(data[len(CB_REMOVE_ITEM) :])
        src = db.get_source_by_channel_id(channel_id)
        if db.remove_source(channel_id):
            refresh_runtime_config(app)
            name = src["channel_name"] if src else channel_id
            await _safe_edit_message_text(
            query,
                f"✅ Removed <b>{name}</b>",
                reply_markup=_keyboard_for_app(app, paused),
                parse_mode="HTML",
            )
        else:
            await _safe_edit_message_text(
            query,
                "Not found.",
                reply_markup=_keyboard_for_app(app, paused),
            )
        return ConversationHandler.END

    if data == CB_LIST:
        sources = db.list_sources()
        if not sources:
            text = "📋 No sources configured."
        else:
            lines = ["📋 <b>Source channels</b>\n"]
            for s in sources:
                st = "active" if s["active"] else "inactive"
                lines.append(
                    f"• <b>{s['channel_name']}</b>\n"
                    f"  ID: <code>{s['channel_id']}</code>\n"
                    f"  Status: {st}"
                )
            text = "\n".join(lines)
        await _safe_edit_message_text(
            query,
            text,
            reply_markup=_keyboard_for_app(app, paused),
            parse_mode="HTML",
        )
        return ConversationHandler.END

    if data == CB_DEST:
        await _safe_edit_message_text(
            query,
            "🎯 <b>Set destination channel</b>",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "Use Current Channel", callback_data=CB_DEST_CURRENT
                        ),
                    ],
                    [
                        InlineKeyboardButton(
                            "Enter Channel ID", callback_data=CB_DEST_MANUAL
                        ),
                    ],
                    [InlineKeyboardButton("« Back", callback_data=CB_MAIN)],
                ]
            ),
            parse_mode="HTML",
        )
        return ConversationHandler.END

    if data == CB_DEST_MANUAL:
        await _safe_edit_message_text(
            query,
            "Send the destination channel ID.\n/cancel to abort.",
            parse_mode="HTML",
        )
        return AWAIT_DESTINATION_ID

    if data == CB_DEST_CURRENT:
        chat = query.message.chat if query.message else None
        if chat and chat.type in ("channel", "supergroup", "group"):
            db.set_destination_channel_id(chat.id)
            refresh_runtime_config(app)
            await _safe_edit_message_text(
            query,
                f"✅ Destination: <code>{chat.id}</code>",
                reply_markup=_keyboard_for_app(app, paused),
                parse_mode="HTML",
            )
            return ConversationHandler.END

        context.user_data["forward_mode"] = "destination"
        await _safe_edit_message_text(
            query,
            "Forward a post from the destination channel here,\n"
            "or send the channel ID.\n/cancel to abort.",
        )
        return AWAIT_FORWARD

    if data == CB_PAUSE:
        db.set_paused(True)
        refresh_runtime_config(app)
        await _safe_edit_message_text(
            query,
            "⏸ Bot paused.",
            reply_markup=_keyboard_for_app(app, True),
            parse_mode="HTML",
        )
        return ConversationHandler.END

    if data == CB_RESUME:
        db.set_paused(False)
        refresh_runtime_config(app)
        await _safe_edit_message_text(
            query,
            "▶ Bot resumed.",
            reply_markup=_keyboard_for_app(app, False),
            parse_mode="HTML",
        )
        return ConversationHandler.END

    if data == CB_STATUS:
        refresh_runtime_config(app)
        await _safe_edit_message_text(
            query,
            await _dashboard_text(app),
            reply_markup=_keyboard_for_app(app),
            parse_mode="HTML",
        )
        return ConversationHandler.END

    if data == CB_MANUAL:
        context.user_data[UD_MANUAL_MODE] = True
        await _safe_edit_message_text(
            query,
            "🛠 <b>Manual Post</b>\n\n"
            "Send Amazon link or ASIN.\n"
            "You can send multiple URLs in one message.\n\n"
            "/cancel to abort.",
            parse_mode="HTML",
        )
        return AWAIT_MANUAL_INPUT

    if data == CB_CUSTOM_IMAGE_POST:
        await _safe_edit_message_text(
            query,
            "🖼 <b>Custom Image Post</b>\n\n"
            "Send a photo with its caption.\n"
            "Any Amazon links in the caption will be shortened automatically.\n\n"
            "/cancel to abort.",
            parse_mode="HTML",
        )
        return AWAIT_CUSTOM_IMAGE_POST

    if data == CB_INLINE_BUTTONS:
        await _safe_edit_message_text(
            query,
            await _inline_buttons_menu_text(db),
            reply_markup=_inline_buttons_keyboard(db),
            parse_mode="HTML",
        )
        return ConversationHandler.END

    if data == CB_AI:
        await _safe_edit_message_text(
            query,
            await _ai_caption_menu_text(db),
            reply_markup=_ai_caption_keyboard(db.get_ai_caption_mode()),
            parse_mode="HTML",
        )
        return ConversationHandler.END

    if data == CB_AI_OFF:
        db.set_ai_caption_mode(MODE_OFF)
        await _safe_edit_message_text(
            query,
            await _ai_caption_menu_text(db),
            reply_markup=_ai_caption_keyboard(MODE_OFF),
            parse_mode="HTML",
        )
        return ConversationHandler.END

    if data == CB_AI_CONSERVATIVE:
        db.set_ai_caption_mode(MODE_CONSERVATIVE)
        await _safe_edit_message_text(
            query,
            await _ai_caption_menu_text(db),
            reply_markup=_ai_caption_keyboard(MODE_CONSERVATIVE),
            parse_mode="HTML",
        )
        return ConversationHandler.END

    if data == CB_AI_MARKETING:
        db.set_ai_caption_mode(MODE_MARKETING)
        await _safe_edit_message_text(
            query,
            await _ai_caption_menu_text(db),
            reply_markup=_ai_caption_keyboard(MODE_MARKETING),
            parse_mode="HTML",
        )
        return ConversationHandler.END

    if data == CB_AI_ARABIC:
        db.set_ai_caption_mode(MODE_ARABIC)
        await _safe_edit_message_text(
            query,
            await _ai_caption_menu_text(db),
            reply_markup=_ai_caption_keyboard(MODE_ARABIC),
            parse_mode="HTML",
        )
        return ConversationHandler.END

    if data == CB_AI_CUSTOM:
        db.set_ai_caption_mode(MODE_CUSTOM)
        await _safe_edit_message_text(
            query,
            "🤖 <b>Custom Brand Tone</b>\n\n"
            "Send custom brand instructions.\n"
            "Example: Always start with 🔥 لقطة اليوم\n\n"
            "/cancel to abort.",
            parse_mode="HTML",
        )
        return AWAIT_AI_CUSTOM

    if data == CB_AI_FIXED_TEMPLATE:
        db.set_ai_caption_mode(MODE_FIXED_TEMPLATE)
        await _safe_edit_message_text(
            query,
            await _ai_caption_menu_text(db),
            reply_markup=_ai_caption_keyboard(MODE_FIXED_TEMPLATE),
            parse_mode="HTML",
        )
        return ConversationHandler.END

    if data == CB_AFFILIATE_SETTINGS:
        refresh_runtime_config(app)
        await _safe_edit_message_text(
            query,
            _affiliate_tag_menu_text(db),
            reply_markup=_affiliate_tag_keyboard(db),
            parse_mode="HTML",
        )
        return ConversationHandler.END

    if data == CB_AFFILIATE_ON:
        db.set_affiliate_tag_enabled(True)
        refresh_runtime_config(app)
        await _safe_edit_message_text(
            query,
            _affiliate_tag_menu_text(db),
            reply_markup=_affiliate_tag_keyboard(db),
            parse_mode="HTML",
        )
        return ConversationHandler.END

    if data == CB_AFFILIATE_OFF:
        db.set_affiliate_tag_enabled(False)
        refresh_runtime_config(app)
        await _safe_edit_message_text(
            query,
            _affiliate_tag_menu_text(db),
            reply_markup=_affiliate_tag_keyboard(db),
            parse_mode="HTML",
        )
        return ConversationHandler.END

    if data == CB_AFFILIATE_CHANGE:
        await _safe_edit_message_text(
            query,
            "✏ <b>Change Affiliate Tag</b>\n\n"
            "Send new affiliate tag value.\n"
            "Valid chars: letters, numbers, hyphen (-), underscore (_)\n\n"
            "Example: <code>sallaa-21</code>\n"
            "/cancel to abort.",
            parse_mode="HTML",
        )
        return AWAIT_AFFILIATE_TAG_VALUE

    if data == CB_AFFILIATE_TEST:
        sample = f"https://{AMAZON_DOMAIN}/dp/B0G1ZC6Z3L"
        transformed = apply_affiliate_tag(sample)
        await query.message.reply_text(
            "🧪 Test Affiliate Link\n\n"
            f"Original: {sample}\n"
            f"Transformed: {transformed}"
        )
        return ConversationHandler.END

    if data == CB_COUPON_SETTINGS:
        await _safe_edit_message_text(
            query,
            _coupon_detection_menu_text(db),
            reply_markup=_coupon_detection_keyboard(),
            parse_mode="HTML",
        )
        return ConversationHandler.END

    if data == CB_COUPON_ON:
        db.set_coupon_detection_enabled(True)
        await _safe_edit_message_text(
            query,
            _coupon_detection_menu_text(db),
            reply_markup=_coupon_detection_keyboard(),
            parse_mode="HTML",
        )
        return ConversationHandler.END

    if data == CB_COUPON_OFF:
        db.set_coupon_detection_enabled(False)
        await _safe_edit_message_text(
            query,
            _coupon_detection_menu_text(db),
            reply_markup=_coupon_detection_keyboard(),
            parse_mode="HTML",
        )
        return ConversationHandler.END

    # Inline Buttons handlers
    if data == CB_PRODUCT_BUTTONS_TOGGLE:
        current = db.get_product_buttons_enabled()
        db.set_product_buttons_enabled(not current)
        await _safe_edit_message_text(
            query,
            await _inline_buttons_menu_text(db),
            reply_markup=_inline_buttons_keyboard(db),
            parse_mode="HTML",
        )
        return ConversationHandler.END

    if data == CB_FIXED_BUTTONS_LIST:
        await _safe_edit_message_text(
            query,
            await _fixed_buttons_list_text(db),
            reply_markup=_fixed_buttons_list_keyboard(db),
            parse_mode="HTML",
        )
        return ConversationHandler.END

    if data == CB_FIXED_BUTTONS_ADD:
        context.user_data["adding_fixed_button"] = True
        await _safe_edit_message_text(
            query,
            "➕ <b>Add Fixed Button</b>\n\n"
            "Send the button title:\n"
            "Example: 🔥 جميع العروض\n\n"
            "/cancel to abort.",
            parse_mode="HTML",
        )
        return AWAIT_FIXED_BUTTON_TITLE

    if data.startswith(CB_FIXED_BUTTONS_EDIT):
        button_id = int(data.split(":")[-1])
        button = db.get_fixed_button(button_id)
        if button:
            context.user_data["editing_fixed_button_id"] = button_id
            await _safe_edit_message_text(
                query,
                f"✏️ <b>Edit Fixed Button</b>\n\n"
                f"Current title: {button['title']}\n"
                f"Current URL: {button['url']}\n\n"
                f"Send new button title (or /cancel to abort):",
                parse_mode="HTML",
            )
            return AWAIT_FIXED_BUTTON_TITLE
        else:
            await _safe_edit_message_text(
                query,
                "Button not found.",
                reply_markup=_inline_buttons_keyboard(db),
                parse_mode="HTML",
            )
            return ConversationHandler.END

    if data.startswith(CB_FIXED_BUTTONS_DELETE):
        button_id = int(data.split(":")[-1])
        if db.delete_fixed_button(button_id):
            await _safe_edit_message_text(
                query,
                "✅ Button deleted.",
                reply_markup=_inline_buttons_keyboard(db),
                parse_mode="HTML",
            )
        else:
            await _safe_edit_message_text(
                query,
                "Failed to delete button.",
                reply_markup=_inline_buttons_keyboard(db),
                parse_mode="HTML",
            )
        return ConversationHandler.END

    if data.startswith(CB_FIXED_BUTTONS_ENABLE):
        button_id = int(data.split(":")[-1])
        db.update_fixed_button(button_id, enabled=True)
        await _safe_edit_message_text(
            query,
            await _fixed_buttons_list_text(db),
            reply_markup=_fixed_buttons_list_keyboard(db),
            parse_mode="HTML",
        )
        return ConversationHandler.END

    if data.startswith(CB_FIXED_BUTTONS_DISABLE):
        button_id = int(data.split(":")[-1])
        db.update_fixed_button(button_id, enabled=False)
        await _safe_edit_message_text(
            query,
            await _fixed_buttons_list_text(db),
            reply_markup=_fixed_buttons_list_keyboard(db),
            parse_mode="HTML",
        )
        return ConversationHandler.END

    if data.startswith(CB_FIXED_BUTTONS_UP):
        button_id = int(data.split(":")[-1])
        await _move_fixed_button(db, button_id, -1)
        await _safe_edit_message_text(
            query,
            await _fixed_buttons_list_text(db),
            reply_markup=_fixed_buttons_list_keyboard(db),
            parse_mode="HTML",
        )
        return ConversationHandler.END

    if data.startswith(CB_FIXED_BUTTONS_DOWN):
        button_id = int(data.split(":")[-1])
        await _move_fixed_button(db, button_id, 1)
        await _safe_edit_message_text(
            query,
            await _fixed_buttons_list_text(db),
            reply_markup=_fixed_buttons_list_keyboard(db),
            parse_mode="HTML",
        )
        return ConversationHandler.END

    if data == CB_FIXED_POSITION_TOP:
        db.set_fixed_buttons_position("TOP")
        await _safe_edit_message_text(
            query,
            await _inline_buttons_menu_text(db),
            reply_markup=_inline_buttons_keyboard(db),
            parse_mode="HTML",
        )
        return ConversationHandler.END

    if data == CB_FIXED_POSITION_BOTTOM:
        db.set_fixed_buttons_position("BOTTOM")
        await _safe_edit_message_text(
            query,
            await _inline_buttons_menu_text(db),
            reply_markup=_inline_buttons_keyboard(db),
            parse_mode="HTML",
        )
        return ConversationHandler.END

    if data == CB_PRODUCT_LAYOUT_VERTICAL:
        db.set_product_button_layout("VERTICAL")
        await _safe_edit_message_text(
            query,
            await _inline_buttons_menu_text(db),
            reply_markup=_inline_buttons_keyboard(db),
            parse_mode="HTML",
        )
        return ConversationHandler.END

    if data == CB_PRODUCT_LAYOUT_TWO_COLUMNS:
        db.set_product_button_layout("TWO_COLUMNS")
        await _safe_edit_message_text(
            query,
            await _inline_buttons_menu_text(db),
            reply_markup=_inline_buttons_keyboard(db),
            parse_mode="HTML",
        )
        return ConversationHandler.END

    if data == CB_PRODUCT_TEMPLATE_SET:
        context.user_data["setting_product_template"] = True
        await _safe_edit_message_text(
            query,
            "📝 <b>Set Product Button Template</b>\n\n"
            "Current template:\n"
            f"{db.get_product_button_template()}\n\n"
            "Send new template.\n"
            "Use {name} as placeholder for product name.\n\n"
            "Examples:\n"
            "🛒 شراء {name}\n"
            "🔥 {name}\n"
            "💰 اشتري {name}\n"
            "{name}\n\n"
            "/cancel to abort.",
            parse_mode="HTML",
        )
        return AWAIT_FIXED_BUTTON_TITLE  # Reuse existing state for text input

    if data == CB_MAX_PRODUCT_1:
        db.set_max_product_buttons(1)
        await _safe_edit_message_text(
            query,
            await _inline_buttons_menu_text(db),
            reply_markup=_inline_buttons_keyboard(db),
            parse_mode="HTML",
        )
        return ConversationHandler.END

    if data == CB_MAX_PRODUCT_2:
        db.set_max_product_buttons(2)
        await _safe_edit_message_text(
            query,
            await _inline_buttons_menu_text(db),
            reply_markup=_inline_buttons_keyboard(db),
            parse_mode="HTML",
        )
        return ConversationHandler.END

    if data == CB_MAX_PRODUCT_3:
        db.set_max_product_buttons(3)
        await _safe_edit_message_text(
            query,
            await _inline_buttons_menu_text(db),
            reply_markup=_inline_buttons_keyboard(db),
            parse_mode="HTML",
        )
        return ConversationHandler.END

    if data == CB_MAX_PRODUCT_4:
        db.set_max_product_buttons(4)
        await _safe_edit_message_text(
            query,
            await _inline_buttons_menu_text(db),
            reply_markup=_inline_buttons_keyboard(db),
            parse_mode="HTML",
        )
        return ConversationHandler.END

    if data == CB_MAX_PRODUCT_5:
        db.set_max_product_buttons(5)
        await _safe_edit_message_text(
            query,
            await _inline_buttons_menu_text(db),
            reply_markup=_inline_buttons_keyboard(db),
            parse_mode="HTML",
        )
        return ConversationHandler.END

    return ConversationHandler.END


async def receive_source_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update.effective_user.id if update.effective_user else None):
        return ConversationHandler.END

    text = (update.message.text or "").strip()
    match = re.search(r"-?\d+", text)
    if not match:
        await update.message.reply_text("Send a valid numeric channel ID.")
        return AWAIT_SOURCE_ID

    channel_id = int(match.group())
    name = text if text != str(channel_id) else f"Channel {channel_id}"
    db = _db(context)
    if db.add_source(channel_id, name):
        refresh_runtime_config(context.application)
        await update.message.reply_text(
            f"✅ Added source <code>{channel_id}</code>",
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text("Source already exists.")
    return ConversationHandler.END


async def receive_destination_id(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if not is_admin(update.effective_user.id if update.effective_user else None):
        return ConversationHandler.END

    text = (update.message.text or "").strip()
    match = re.search(r"-?\d+", text)
    if not match:
        await update.message.reply_text("Send a valid numeric channel ID.")
        return AWAIT_DESTINATION_ID

    channel_id = int(match.group())
    db = _db(context)
    db.set_destination_channel_id(channel_id)
    refresh_runtime_config(context.application)
    await update.message.reply_text(
        f"✅ Destination set to <code>{channel_id}</code>",
        parse_mode="HTML",
    )
    return ConversationHandler.END


async def receive_forward(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update.effective_user.id if update.effective_user else None):
        return ConversationHandler.END

    msg = update.message
    db = _db(context)
    mode = context.user_data.pop("forward_mode", "source")

    if msg.forward_from_chat and msg.forward_from_chat.type == "channel":
        channel_id = msg.forward_from_chat.id
        name = msg.forward_from_chat.title or str(channel_id)
        if mode == "destination":
            db.set_destination_channel_id(channel_id)
            refresh_runtime_config(context.application)
            await msg.reply_text(
                f"✅ Destination: <b>{name}</b>\n<code>{channel_id}</code>",
                parse_mode="HTML",
            )
            return ConversationHandler.END

        if db.add_source(channel_id, name):
            refresh_runtime_config(context.application)
            await msg.reply_text(
                f"✅ Added source <b>{name}</b>\n<code>{channel_id}</code>",
                parse_mode="HTML",
            )
        else:
            await msg.reply_text("Source already exists.")
        return ConversationHandler.END

    text = (msg.text or "").strip()
    match = re.search(r"-?\d+", text)
    if match:
        channel_id = int(match.group())
        if mode == "destination":
            db.set_destination_channel_id(channel_id)
            refresh_runtime_config(context.application)
            await msg.reply_text(
                f"✅ Destination: <code>{channel_id}</code>",
                parse_mode="HTML",
            )
        elif db.add_source(channel_id, f"Channel {channel_id}"):
            refresh_runtime_config(context.application)
            await msg.reply_text(
                f"✅ Added source <code>{channel_id}</code>",
                parse_mode="HTML",
            )
        else:
            await msg.reply_text("Source already exists.")
        return ConversationHandler.END

    await msg.reply_text("Forward a channel post or send a channel ID.")
    return AWAIT_FORWARD


async def receive_ai_custom_prompt(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if not is_admin(update.effective_user.id if update.effective_user else None):
        return ConversationHandler.END

    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("Instructions cannot be empty. Try again or /cancel.")
        return AWAIT_AI_CUSTOM

    db = _db(context)
    db.set_ai_custom_prompt(text)
    db.set_ai_caption_mode(MODE_CUSTOM)
    paused = context.application.bot_data.get("paused", False)
    await update.message.reply_text(
        "✅ Custom brand instructions saved.\n\n" + await _ai_caption_menu_text(db),
        reply_markup=_ai_caption_keyboard(MODE_CUSTOM),
        parse_mode="HTML",
    )
    return ConversationHandler.END


async def receive_affiliate_tag_value(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    user = update.effective_user
    if not is_admin(user.id if user else None):
        return ConversationHandler.END

    msg = update.message
    if not msg or not msg.text:
        return AWAIT_AFFILIATE_TAG_VALUE

    tag = (msg.text or "").strip()
    if not is_valid_affiliate_tag(tag):
        await msg.reply_text(
            "❌ Invalid affiliate tag.\n\n"
            "Allowed characters only: letters, numbers, hyphen (-), underscore (_).\n"
            "Example: sallaa-21\n\n"
            "Try again or /cancel.",
        )
        return AWAIT_AFFILIATE_TAG_VALUE

    db = _db(context)
    db.set_affiliate_tag_value(tag)
    refresh_runtime_config(context.application)

    await msg.reply_text(
        f"✅ Affiliate tag saved: <code>{tag}</code>",
        parse_mode="HTML",
        reply_markup=_affiliate_tag_keyboard(db),
    )
    return ConversationHandler.END


async def receive_fixed_button_title(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if not is_admin(update.effective_user.id if update.effective_user else None):
        return ConversationHandler.END

    msg = update.message
    text = (msg.text or "").strip()

    # Check if this is for setting product template
    if context.user_data.pop("setting_product_template", False):
        db = _db(context)
        db.set_product_button_template(text)
        await msg.reply_text(
            "✅ Product button template saved.",
            reply_markup=_inline_buttons_keyboard(db),
            parse_mode="HTML",
        )
        return ConversationHandler.END

    # Original fixed button title handling
    if not text:
        await msg.reply_text("Title cannot be empty. Try again or /cancel.")
        return AWAIT_FIXED_BUTTON_TITLE

    context.user_data["fixed_button_title"] = text
    await msg.reply_text(
        "Now send the button URL:\n"
        "Example: https://t.me/loqtabgd\n\n"
        "/cancel to abort.",
    )
    return AWAIT_FIXED_BUTTON_URL


async def receive_fixed_button_url(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if not is_admin(update.effective_user.id if update.effective_user else None):
        return ConversationHandler.END

    msg = update.message
    url = (msg.text or "").strip()
    if not url:
        await msg.reply_text("URL cannot be empty. Try again or /cancel.")
        return AWAIT_FIXED_BUTTON_URL

    # Validate URL
    if not url.startswith("https://"):
        await msg.reply_text(
            "❌ Invalid URL.\n\n"
            "URL must start with https://\n"
            "Example: https://t.me/loqtabgd\n\n"
            "Try again or /cancel.",
        )
        return AWAIT_FIXED_BUTTON_URL

    title = context.user_data.pop("fixed_button_title", "")
    if not title:
        await msg.reply_text("Error: Title lost. Please start over.")
        return ConversationHandler.END

    # Validate title length
    if len(title) > 64:
        await msg.reply_text(
            f"❌ Title too long.\n\n"
            f"Title must be 64 characters or less.\n"
            f"Current length: {len(title)}\n\n"
            f"Try again or /cancel.",
        )
        return AWAIT_FIXED_BUTTON_URL

    db = _db(context)

    # Check if editing or adding
    editing_id = context.user_data.pop("editing_fixed_button_id", None)
    if editing_id:
        db.update_fixed_button(editing_id, title=title, url=url)
        await msg.reply_text(
            "✅ Button updated.",
            reply_markup=_inline_buttons_keyboard(db),
            parse_mode="HTML",
        )
    else:
        # Get max sort_order for new button
        buttons = db.list_fixed_buttons()
        max_order = max((b["sort_order"] for b in buttons), default=-1)
        db.create_fixed_button(title, url, sort_order=max_order + 1)
        await msg.reply_text(
            "✅ Button added.",
            reply_markup=_inline_buttons_keyboard(db),
            parse_mode="HTML",
        )

    return ConversationHandler.END


async def receive_telethon_code(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    user = update.effective_user
    if not is_admin(user.id if user else None):
        return ConversationHandler.END

    msg = update.message
    if not msg or not msg.text:
        return AWAIT_TELETHON_CODE

    app = context.application
    if app.bot_data.get("telethon_auth_state") != AUTH_STATE_CODE:
        return ConversationHandler.END

    await delete_sensitive_message(context.bot, msg.chat_id, msg.message_id)

    reply, done = await submit_code(app, msg.text.strip())
    await msg.reply_text(reply, parse_mode="HTML")

    if done:
        return ConversationHandler.END
    if app.bot_data.get("telethon_auth_state") == AUTH_STATE_PASSWORD:
        return AWAIT_TELETHON_PASSWORD
    return AWAIT_TELETHON_CODE


async def receive_telethon_password(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    user = update.effective_user
    if not is_admin(user.id if user else None):
        return ConversationHandler.END

    msg = update.message
    if not msg or not msg.text:
        return AWAIT_TELETHON_PASSWORD

    app = context.application
    if app.bot_data.get("telethon_auth_state") != AUTH_STATE_PASSWORD:
        return ConversationHandler.END

    await delete_sensitive_message(context.bot, msg.chat_id, msg.message_id)

    reply, done = await submit_password(app, msg.text)
    await msg.reply_text(reply, parse_mode="HTML")
    return ConversationHandler.END


async def receive_restore_upload(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    user = update.effective_user
    if not is_admin(user.id if user else None):
        return ConversationHandler.END

    msg = update.message
    if not msg:
        return AWAIT_RESTORE_UPLOAD
    if not msg.document:
        await msg.reply_text("Send a .zip backup file.")
        return AWAIT_RESTORE_UPLOAD

    doc = msg.document
    filename = (doc.file_name or "").lower()
    if not filename.endswith(".zip"):
        await msg.reply_text("Only .zip backup files are accepted.")
        return AWAIT_RESTORE_UPLOAD

    app = context.application
    tmp_dir = Path("restore_uploads")
    tmp_dir.mkdir(exist_ok=True)
    zip_path: Path | None = tmp_dir / f"restore_{user.id}_{msg.message_id}.zip"

    try:
        tg_file = await context.bot.get_file(doc.file_id)
        await tg_file.download_to_drive(str(zip_path))

        errors = validate_backup_zip(zip_path)
        if errors:
            zip_path.unlink(missing_ok=True)
            await msg.reply_text(
                "Invalid backup archive:\n• " + "\n• ".join(errors),
                parse_mode="HTML",
            )
            return AWAIT_RESTORE_UPLOAD

        context.user_data[UD_PENDING_RESTORE] = str(zip_path)
        app.bot_data["pending_restore_zip"] = str(zip_path)

        await msg.reply_text(
            "⚠️ <b>Restore backup and restart bot?</b>\n\n"
            f"Archive: <code>{doc.file_name}</code>\n"
            "This replaces bot.db, .env, session, frame, and config files.",
            reply_markup=_restore_confirm_keyboard(),
            parse_mode="HTML",
        )
        return ConversationHandler.END
    except Exception as exc:
        logger.exception("Restore upload failed")
        if zip_path.exists():
            zip_path.unlink(missing_ok=True)
        await msg.reply_text(f"Upload failed: {exc}")
        return AWAIT_RESTORE_UPLOAD


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop(UD_MANUAL_MODE, None)
    context.user_data.pop(UD_EDITING_DRAFT, None)
    pending = context.user_data.pop(UD_PENDING_RESTORE, None)
    context.application.bot_data.pop("pending_restore_zip", None)
    if pending:
        Path(pending).unlink(missing_ok=True)
    clear_auth_state(context.application)
    if update.message:
        await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


def build_admin_handlers() -> list:
    admin_filter = filters.User(user_id=ADMIN_USER_IDS) if ADMIN_USER_IDS else filters.ALL
    manual_states = manual_state_handlers(admin_filter)
    custom_image_states = custom_image_state_handlers(admin_filter)
    custom_image_callbacks = custom_image_callback_handlers()

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("admin", cmd_admin, filters=admin_filter),
            CallbackQueryHandler(on_callback, pattern=r"^adm:"),
            CallbackQueryHandler(handle_edit_draft, pattern=r"^edit_draft:\d+$"),
        ],
        states={
            AWAIT_SOURCE_ID: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND & admin_filter,
                    receive_source_id,
                ),
            ],
            AWAIT_DESTINATION_ID: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND & admin_filter,
                    receive_destination_id,
                ),
            ],
            AWAIT_FORWARD: [
                MessageHandler(
                    admin_filter & ~filters.COMMAND,
                    receive_forward,
                ),
            ],
            AWAIT_AI_CUSTOM: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND & admin_filter,
                    receive_ai_custom_prompt,
                ),
            ],
            AWAIT_TELETHON_CODE: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND & admin_filter,
                    receive_telethon_code,
                ),
            ],
            AWAIT_TELETHON_PASSWORD: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND & admin_filter,
                    receive_telethon_password,
                ),
            ],
            AWAIT_RESTORE_UPLOAD: [
                MessageHandler(
                    admin_filter & filters.Document.ALL,
                    receive_restore_upload,
                ),
            ],
            AWAIT_AFFILIATE_TAG_VALUE: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND & admin_filter,
                    receive_affiliate_tag_value,
                ),
            ],
            AWAIT_FIXED_BUTTON_TITLE: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND & admin_filter,
                    receive_fixed_button_title,
                ),
            ],
            AWAIT_FIXED_BUTTON_URL: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND & admin_filter,
                    receive_fixed_button_url,
                ),
            ],
            **manual_states,
            **custom_image_states,
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel, filters=admin_filter),
            CallbackQueryHandler(on_callback, pattern=r"^adm:"),
            CallbackQueryHandler(handle_edit_draft, pattern=r"^edit_draft:\d+$"),
            *custom_image_callbacks,
        ],
        allow_reentry=True,
    )
    return [conv]
