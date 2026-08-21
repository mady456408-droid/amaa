import json
import os
import time
import threading
from datetime import datetime
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from telebot.apihelper import ApiTelegramException

from chatgpt_client import ChatGPT as ChatGPTClient

BOT_TOKEN = "Juc"
ADMIN_IDS = [1245687]
CONFIG_FILE = "config.json"
COOKIES_FILE = "cookies.json"
CHANNEL_USERNAME = "@LO5545GD"

PAYLOAD_LABELS = {
    "model": "النموذج",
    "history_and_training_disabled": "تعطيل السجل والتدريب",
    "enable_message_followups": "تفعيل متابعة الرسائل",
    "force_use_sse": "فرض استخدام SSE",
    "force_use_search": "فرض استخدام البحث",
    "force_paragen": "فرض paragen",
    "supports_buffering": "دعم التخزين المؤقت",
    "timezone": "المنطقة الزمنية",
    "timezone_offset_min": "إزاحة المنطقة الزمنية",
    "system_hints": "تلميحات النظام",
    "is_onboarding_conversation": "محادثة تهيئة",
    "no_auth_ad_preferences": "تفضيلات بدون مصادقة",
    "client_prepare_dispatch": "client_prepare_dispatch",
    "client_prepare_source": "client_prepare_source",
    "client_prepare_state": "client_prepare_state"
}

# Bot-specific ChatGPT instance using bot's config files
gpt = ChatGPTClient(config_file=CONFIG_FILE, cookies_file=COOKIES_FILE)


class UserSessions:
    def __init__(self):
        self.sessions = {}
        self.timeout = 1800

    def get(self, chat_id):
        if chat_id not in self.sessions:
            self.sessions[chat_id] = {"conversation_id":None, "parent_id":None,
                                      "history":[], "last_active":datetime.now()}
        self.sessions[chat_id]["last_active"] = datetime.now()
        return self.sessions[chat_id]

    def reset(self, chat_id):
        self.sessions[chat_id] = {"conversation_id":None, "parent_id":None,
                                  "history":[], "last_active":datetime.now()}

    def timeout_check(self):
        now = datetime.now()
        for cid in list(self.sessions.keys()):
            if (now - self.sessions[cid]["last_active"]).total_seconds() > self.timeout:
                del self.sessions[cid]

    def active_count(self): return len(self.sessions)


gpt = ChatGPT()
users = UserSessions()
bot = telebot.TeleBot(BOT_TOKEN)

def auto_saver():
    while True:
        time.sleep(300)
        users.timeout_check()
threading.Thread(target=auto_saver, daemon=True).start()

def is_admin(uid): return uid in ADMIN_IDS

# ---------- وظيفة فحص الاشتراك ----------
def check_subscription(user_id):
    try:
        member = bot.get_chat_member(CHANNEL_USERNAME, user_id)
        return member.status not in ['left', 'kicked']
    except ApiTelegramException as e:
        # إذا كانت القناة غير موجودة أو البوت ليس مشرفاً، نعتبر أن الاشتراك مفعل مؤقتاً
        return True

def bool_icon(value): return "✅" if value else "❌"
def search_icon(value): return "✅" if value else ("❌" if value is False else "⚪")

def safe_edit_text(text, chat_id, mid, **kwargs):
    try:
        return bot.edit_message_text(text, chat_id, mid, **kwargs)
    except ApiTelegramException as e:
        if "message is not modified" not in str(e):
            raise

# ---------- لوحات المفاتيح ----------
def main_menu():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("🆕 محادثة جديدة", callback_data="new_chat"),
        InlineKeyboardButton("ℹ️ حالة الجلسة", callback_data="session_status"),
        InlineKeyboardButton("⚙️ إعدادات Payload", callback_data="payload_menu")
    )
    return kb

def payload_menu():
    kb = InlineKeyboardMarkup(row_width=2)
    for key, label in PAYLOAD_LABELS.items():
        val = gpt.payload_config.get(key)
        if isinstance(val, bool):
            icon = bool_icon(val)
        elif key == "force_use_search":
            icon = search_icon(val)
        elif isinstance(val, (list, dict)):
            icon = "⚙️"
        else:
            icon = f"`{val}`" if val else "⚪"
        kb.add(InlineKeyboardButton(f"{label}: {icon}", callback_data=f"edit|{key}"))
    kb.add(InlineKeyboardButton("📊 JSON كامل", callback_data="show_payload"),
           InlineKeyboardButton("🔙 القائمة", callback_data="main_menu"))
    return kb

def boolean_edit_menu(key):
    current = gpt.payload_config[key]
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(InlineKeyboardButton("✅ مفعل" if current else "✅ تفعيل", callback_data=f"toggle|{key}|true"),
           InlineKeyboardButton("❌ معطل" if not current else "❌ تعطيل", callback_data=f"toggle|{key}|false"))
    kb.add(InlineKeyboardButton("🔙 رجوع", callback_data="payload_menu"))
    return kb

def search_edit_menu(key):
    current = gpt.payload_config[key]
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(InlineKeyboardButton("✅ True" if current is True else "✅ True", callback_data=f"search_toggle|{key}|true"),
           InlineKeyboardButton("❌ False" if current is False else "❌ False", callback_data=f"search_toggle|{key}|false"),
           InlineKeyboardButton("⚪ None" if current is None else "⚪ None", callback_data=f"search_toggle|{key}|none"))
    kb.add(InlineKeyboardButton("🔙 رجوع", callback_data="payload_menu"))
    return kb

def model_edit_menu():
    current = gpt.payload_config["model"]
    models = ["auto", "gpt-4o", "gpt-5", "gpt-5-5"]
    kb = InlineKeyboardMarkup(row_width=2)
    for m in models:
        kb.add(InlineKeyboardButton(f"{m} {'✅' if m == current else ''}", callback_data=f"model_select|{m}"))
    kb.add(InlineKeyboardButton("🔙 رجوع", callback_data="payload_menu"))
    return kb

# ---------- الأوامر ----------
@bot.message_handler(commands=['start'])
def start(msg):
    if not check_subscription(msg.from_user.id):
        bot.send_message(msg.chat.id, 
            "⚠️ يجب عليك الاشتراك في القناة أولاً لاستخدام البوت:\n"
            "https://t.me/editortrue",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("اشترك الآن", url="https://t.me/editortrue")
            ))
        return
    bot.send_message(msg.chat.id, "🤖 **مرحباً!**\nبوت ChatGPT مع بث مباشر وإعدادات Payload.\nأرسل رسالتك.",
                     parse_mode="Markdown", reply_markup=main_menu())

@bot.message_handler(commands=['new','جديد'])
def new(msg):
    if not check_subscription(msg.from_user.id):
        bot.send_message(msg.chat.id, "⚠️ يجب الاشتراك في القناة @editortrue أولاً.")
        return
    users.reset(msg.chat.id)
    bot.reply_to(msg, "✅ محادثة جديدة.", reply_markup=main_menu())

# ---------- الرسائل العادية مع البث المباشر ----------
@bot.message_handler(func=lambda m: True, content_types=['text'])
def handle_text(msg):
    chat_id = msg.chat.id
    if not check_subscription(msg.from_user.id):
        bot.send_message(chat_id, 
            "⚠️ يجب عليك الاشتراك في القناة أولاً:\nhttps://t.me/editortrue",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("اشترك الآن", url="https://t.me/editortrue")
            ))
        return

    text = msg.text.strip()
    if text.startswith("/"): return

    session = users.get(chat_id)
    bot.send_chat_action(chat_id, 'typing')

    draft = bot.reply_to(msg, "⏳")
    draft_id = draft.message_id
    full = ""
    last = time.time()

    def on_chunk(chunk):
        nonlocal full, last
        full += chunk
        now = time.time()
        if now - last > 0.8 or len(chunk) > 30:
            try:
                bot.edit_message_text(full + "▌", chat_id, draft_id)
                last = now
            except ApiTelegramException:
                pass

    reply, new_cid, new_pid, model, error = gpt.send_message(
        text, session["conversation_id"], session["parent_id"], on_token=on_chunk
    )

    if error:
        if any(str(code) in str(error) for code in ["422", "401", "403"]):
            safe_edit_text("🔄 جاري تجديد الجلسة وإعادة المحاولة...", chat_id, draft_id)
            gpt.init_session()
            reply2, new_cid2, new_pid2, model2, error2 = gpt.send_message(
                text, session["conversation_id"], session["parent_id"], on_token=on_chunk, retry=False
            )
            if error2:
                safe_edit_text(f"❌ فشل بعد التجديد: {error2}", chat_id, draft_id)
            else:
                if reply2 is None:
                    safe_edit_text("❌ فشل الاتصال (لم يتم استقبال رد).", chat_id, draft_id)
                else:
                    try:
                        bot.edit_message_text(reply2, chat_id, draft_id)
                    except ApiTelegramException:
                        bot.delete_message(chat_id, draft_id)
                        bot.send_message(chat_id, reply2, reply_markup=main_menu())
                    session["conversation_id"] = new_cid2
                    session["parent_id"] = new_pid2
                    ts = datetime.now().isoformat()
                    session["history"].append({"role":"user","content":text,"timestamp":ts})
                    session["history"].append({"role":"assistant","content":reply2,"model":model2,"timestamp":ts})
            return

        safe_edit_text(f"❌ خطأ: {error}", chat_id, draft_id)
        return

    if reply is None:
        safe_edit_text("❌ فشل الاتصال (لم يتم استقبال رد).", chat_id, draft_id)
        return

    try:
        bot.edit_message_text(reply, chat_id, draft_id)
    except ApiTelegramException:
        bot.delete_message(chat_id, draft_id)
        bot.send_message(chat_id, reply, reply_markup=main_menu())

    session["conversation_id"] = new_cid
    session["parent_id"] = new_pid
    ts = datetime.now().isoformat()
    session["history"].append({"role":"user","content":text,"timestamp":ts})
    session["history"].append({"role":"assistant","content":reply,"model":model,"timestamp":ts})

# ---------- الكيبورد التفاعلي ----------
@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    chat_id = call.message.chat.id
    mid = call.message.message_id
    data = call.data
    uid = call.from_user.id

    # التحقق من الاشتراك
    if not check_subscription(uid):
        bot.answer_callback_query(call.id, "⚠️ يجب الاشتراك في القناة @editortrue أولاً", show_alert=True)
        return

    admin_actions = ["edit|", "toggle|", "search_toggle|", "model_select|", "payload_menu", "show_payload"]
    if any(data.startswith(p) for p in admin_actions):
        if not is_admin(uid):
            bot.answer_callback_query(call.id, "⛔ للمشرفين فقط", show_alert=True)
            return

    if data == "new_chat":
        users.reset(chat_id)
        safe_edit_text("✅ بدء محادثة جديدة.", chat_id, mid, reply_markup=main_menu())
    elif data == "session_status":
        s = users.get(chat_id)
        msgs = len(s["history"])
        conv = s["conversation_id"] or "جديدة"
        bot.answer_callback_query(call.id, f"الرسائل: {msgs}\nالمحادثة: {conv[:20]}...", show_alert=True)
    elif data == "payload_menu":
        safe_edit_text("⚙️ إعدادات Payload:", chat_id, mid, reply_markup=payload_menu())
    elif data == "main_menu":
        safe_edit_text("القائمة الرئيسية:", chat_id, mid, reply_markup=main_menu())
    elif data == "show_payload":
        cfg = json.dumps(gpt.payload_config, indent=2, ensure_ascii=False)[:4000]
        kb = InlineKeyboardMarkup().add(InlineKeyboardButton("🔙 رجوع", callback_data="payload_menu"))
        safe_edit_text(f"```json\n{cfg}\n```", chat_id, mid, parse_mode="Markdown", reply_markup=kb)

    elif data.startswith("edit|"):
        key = data.split("|", 1)[1]
        if key in ("history_and_training_disabled","enable_message_followups","force_use_sse",
                   "force_paragen","supports_buffering","is_onboarding_conversation"):
            safe_edit_text(f"تعديل: {PAYLOAD_LABELS[key]}", chat_id, mid, reply_markup=boolean_edit_menu(key))
        elif key == "force_use_search":
            safe_edit_text(f"تعديل: {PAYLOAD_LABELS[key]}", chat_id, mid, reply_markup=search_edit_menu(key))
        elif key == "model":
            safe_edit_text("اختر النموذج:", chat_id, mid, reply_markup=model_edit_menu())
        elif key in ("timezone","timezone_offset_min","client_prepare_dispatch","client_prepare_source","client_prepare_state"):
            label = PAYLOAD_LABELS[key]
            safe_edit_text(f"أرسل قيمة {label}:", chat_id, mid)
            bot.register_next_step_handler(call.message, process_text_setting, key, mid)
        elif key in ("system_hints","no_auth_ad_preferences"):
            label = PAYLOAD_LABELS[key]
            safe_edit_text(f"أرسل قيمة {label} (JSON):", chat_id, mid)
            bot.register_next_step_handler(call.message, process_json_setting, key, mid)

    elif data.startswith("toggle|"):
        _, key, value = data.split("|", 2)
        gpt.payload_config[key] = (value == "true")
        gpt.save_state()
        safe_edit_text(f"تعديل: {PAYLOAD_LABELS[key]}", chat_id, mid, reply_markup=boolean_edit_menu(key))
    elif data.startswith("search_toggle|"):
        _, key, value = data.split("|", 2)
        if value == "none":
            gpt.payload_config[key] = None
        else:
            gpt.payload_config[key] = (value == "true")
        gpt.save_state()
        safe_edit_text(f"تعديل: {PAYLOAD_LABELS[key]}", chat_id, mid, reply_markup=search_edit_menu(key))
    elif data.startswith("model_select|"):
        model = data.split("|", 1)[1]
        gpt.payload_config["model"] = model
        gpt.save_state()
        safe_edit_text("اختر النموذج:", chat_id, mid, reply_markup=model_edit_menu())

def process_text_setting(msg, key, mid):
    if not check_subscription(msg.from_user.id):
        bot.reply_to(msg, "⚠️ يجب الاشتراك في القناة @editortrue أولاً.")
        return
    val = msg.text.strip()
    if key == "timezone_offset_min":
        try:
            val = int(val)
        except:
            safe_edit_text("❌ رقم غير صحيح.", msg.chat.id, mid, reply_markup=payload_menu())
            return
    gpt.payload_config[key] = val
    gpt.save_state()
    safe_edit_text(f"✅ تم تحديث {PAYLOAD_LABELS[key]}", msg.chat.id, mid, reply_markup=payload_menu())

def process_json_setting(msg, key, mid):
    if not check_subscription(msg.from_user.id):
        bot.reply_to(msg, "⚠️ يجب الاشتراك في القناة @editortrue أولاً.")
        return
    try:
        parsed = json.loads(msg.text.strip())
        gpt.payload_config[key] = parsed
        gpt.save_state()
        safe_edit_text(f"✅ تم تحديث {PAYLOAD_LABELS[key]}", msg.chat.id, mid, reply_markup=payload_menu())
    except Exception as e:
        safe_edit_text(f"❌ JSON غير صالح: {e}", msg.chat.id, mid, reply_markup=payload_menu())

print("🤖 البوت يعمل...")
bot.infinity_polling()