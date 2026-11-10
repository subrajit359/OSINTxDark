import asyncio
import ipaddress
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Optional

import asyncpg
from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError
from telethon.sessions import StringSession
from telegram import Update
from telegram.constants import ChatType
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

load_dotenv()
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("gamerzone")

# Keep the user's existing Railway variable names unchanged.
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = os.getenv("OWNER_ID", "").strip()
GROUP_ID = os.getenv("GROUP_ID", "").strip()
BOT1_ID = os.getenv("BOT1_ID", "").strip()
TELETHON_API_ID = os.getenv("TELETHON_API_ID", "").strip()
TELETHON_API_HASH = os.getenv("TELETHON_API_HASH", "").strip()
TELETHON_PHONE = os.getenv("TELETHON_PHONE", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
RESPONSE_TIMEOUT_SECONDS = float(os.getenv("RESPONSE_TIMEOUT_SECONDS", "15"))
RESPONSE_SETTLE_SECONDS = float(os.getenv("RESPONSE_SETTLE_SECONDS", "1.0"))
SUPPORT_FOOTER = "\n\n📩 Any issue? Contact @subro98 / @gudugaming8069"

# Public lookup is intentionally limited to non-sensitive public information.
# Identity, personal contact, family, Aadhaar/PAN, owner tracing and device-ID
# lookups are not exposed by this public bot.
PUBLIC_COMMANDS = {"ip": "Public IP address information"}
RESTRICTED_COMMANDS = {
    "num", "phoneinfo", "hp", "gas", "aadhar", "family", "a2r",
    "numtoupi", "pan", "pan2", "rc", "rc2", "v2n", "v2vn",
    "challan", "tgusername", "tgid", "imei",
}

pool: Optional[asyncpg.Pool] = None
telethon_client: Optional[TelegramClient] = None
bot_app: Optional[Application] = None
login_state: dict = {}


def owner_ok(user_id: int) -> bool:
    return bool(OWNER_ID) and str(user_id) == OWNER_ID


async def send_text(update: Update, text: str, *, reply_markup=None):
    msg = update.effective_message
    if not msg:
        return
    text = str(text or "No result returned.")
    max_body = 3900 - len(SUPPORT_FOOTER)
    chunks = [text[i:i + max_body] for i in range(0, len(text), max_body)] or ["No result returned."]
    for chunk in chunks:
        await msg.reply_text(chunk + SUPPORT_FOOTER, disable_web_page_preview=True, reply_markup=reply_markup)


async def db_init():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5, command_timeout=30)
    async with pool.acquire() as con:
        await con.execute("""
        CREATE TABLE IF NOT EXISTS gz_telegram_session (
            id SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
            session_string TEXT NOT NULL,
            phone TEXT,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """)
        await con.execute("""
        CREATE TABLE IF NOT EXISTS gz_query_requests (
            request_id TEXT PRIMARY KEY,
            command TEXT NOT NULL,
            query_text TEXT NOT NULL,
            telegram_message_id BIGINT,
            bot_reply_message_id BIGINT,
            status TEXT NOT NULL DEFAULT 'pending',
            result TEXT,
            error_message TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """)
        await con.execute("ALTER TABLE gz_query_requests ADD COLUMN IF NOT EXISTS bot_reply_message_id BIGINT")
        await con.execute("""
        CREATE TABLE IF NOT EXISTS gz_bot_deliveries (
            request_id TEXT PRIMARY KEY REFERENCES gz_query_requests(request_id) ON DELETE CASCADE,
            chat_id BIGINT NOT NULL,
            user_id BIGINT NOT NULL,
            delivered BOOLEAN NOT NULL DEFAULT FALSE
        )
        """)


async def load_session() -> str:
    async with pool.acquire() as con:
        row = await con.fetchrow("SELECT session_string FROM gz_telegram_session WHERE id=1")
    return row["session_string"] if row else ""


async def save_session(phone: str):
    session_string = telethon_client.session.save()
    async with pool.acquire() as con:
        await con.execute("""
            INSERT INTO gz_telegram_session(id, session_string, phone, updated_at)
            VALUES(1, $1, $2, NOW())
            ON CONFLICT(id) DO UPDATE SET session_string=EXCLUDED.session_string,
              phone=EXCLUDED.phone, updated_at=NOW()
        """, session_string, phone)


async def connect_telethon(session_string: str = ""):
    global telethon_client
    if not TELETHON_API_ID or not TELETHON_API_HASH:
        raise RuntimeError("TELETHON_API_ID and TELETHON_API_HASH must be configured")
    telethon_client = TelegramClient(StringSession(session_string), int(TELETHON_API_ID), TELETHON_API_HASH)
    await telethon_client.connect()
    if session_string and not await telethon_client.is_user_authorized():
        log.warning("Saved Telethon session is no longer authorized; owner must run /login")
    telethon_client.add_event_handler(on_group_message, events.NewMessage(chats=int(GROUP_ID)))


async def on_group_message(event):
    """Capture only replies that are linked to a request sent by this relay."""
    try:
        reply_id = event.message.reply_to_msg_id
        if not reply_id:
            return
        sender = await event.get_sender()
        sender_id = getattr(sender, "id", None)
        # Correlation to our own outgoing group message is the primary filter.
        async with pool.acquire() as con:
            row = await con.fetchrow(
                "SELECT request_id, status FROM gz_query_requests WHERE (telegram_message_id=$1 OR bot_reply_message_id=$1) AND status IN ('pending','processing') LIMIT 1",
                int(reply_id),
            )
            if not row:
                return
            text = (event.raw_text or "").strip()
            if event.message.media:
                try:
                    downloaded = await event.download_media(bytes)
                    if downloaded:
                        text = downloaded.decode("utf-8", errors="replace").strip()
                except Exception:
                    log.exception("Could not download reply attachment")
            if not text:
                return
            # Ignore obvious progress-only messages; accept final text/JSON.
            if not ("{" in text or re.search(r"success|failed|invalid|not found|error|result|records|rejected|no data", text, re.I)):
                await con.execute("UPDATE gz_query_requests SET status='processing', bot_reply_message_id=$2, updated_at=NOW() WHERE request_id=$1", row["request_id"], int(event.message.id))
                return
            await asyncio.sleep(RESPONSE_SETTLE_SECONDS)
            await con.execute("UPDATE gz_query_requests SET status='completed', result=$2, updated_at=NOW() WHERE request_id=$1", row["request_id"], text[:50000])
            log.info("Captured reply for request %s (sender id %s)", row["request_id"], sender_id)
    except Exception:
        log.exception("Failed to process private-group reply")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = ["🎮 GAMERZONE Smart Lookup", "", "Available public-information commands:"]
    lines += [f"/{cmd} — {desc}" for cmd, desc in PUBLIC_COMMANDS.items()]
    lines += ["", "Example: /ip 8.8.8.8", "Personal identity, contact, family, owner-tracing and device-ID lookups are disabled."]
    await send_text(update, "\n".join(lines))


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await help_command(update, context)


async def id_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_text(update, f"Your Telegram user ID: {update.effective_user.id}")


async def login_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not owner_ok(user_id):
        await send_text(update, "⛔ This command is available to the configured owner only.")
        return
    if not update.effective_chat or update.effective_chat.type != ChatType.PRIVATE:
        await send_text(update, "Please use /login in a private chat with this bot.")
        return
    if not telethon_client:
        await send_text(update, "Telegram client is not initialized. Check Railway logs and API variables.")
        return
    phone = context.args[0] if context.args else TELETHON_PHONE
    if not phone:
        login_state[user_id] = {"stage": "phone"}
        await send_text(update, "📱 Send your phone number with country code, e.g. +919876543210")
        return
    await begin_phone_login(update, phone)


async def begin_phone_login(update: Update, phone: str):
    if not re.fullmatch(r"\+[1-9]\d{7,14}", phone):
        await send_text(update, "Invalid phone format. Use international format, e.g. +919876543210")
        return
    try:
        sent = await telethon_client.send_code_request(phone)
        login_state[update.effective_user.id] = {"stage": "code", "phone": phone, "phone_code_hash": sent.phone_code_hash}
        await send_text(update, "📩 Telegram sent a login code. Send /otp 12345. Never share this code with anyone.")
    except Exception as exc:
        log.exception("Could not request Telegram login code")
        await send_text(update, f"⚠️ Could not request login code: {str(exc)[:180]}")


async def otp_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    state = login_state.get(user_id)
    if not owner_ok(user_id) or not state or state.get("stage") != "code":
        await send_text(update, "Start with /login first.")
        return
    code = "".join(context.args)
    if not re.fullmatch(r"\d{4,8}", code):
        await send_text(update, "Usage: /otp 12345")
        return
    try:
        await telethon_client.sign_in(phone=state["phone"], code=code, phone_code_hash=state["phone_code_hash"])
        await save_session(state["phone"])
        login_state.pop(user_id, None)
        await send_text(update, "✅ Telegram account connected. Telethon session saved in PostgreSQL.")
    except SessionPasswordNeededError:
        state["stage"] = "password"
        await send_text(update, "🔐 Two-Step Verification is enabled. Send /password YOUR_2FA_PASSWORD in this private chat.")
    except Exception as exc:
        log.exception("OTP verification failed")
        await send_text(update, f"⚠️ OTP verification failed: {str(exc)[:180]}")


async def password_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    state = login_state.get(user_id)
    if not owner_ok(user_id) or not state or state.get("stage") != "password":
        await send_text(update, "No 2FA step is pending. Start with /login.")
        return
    password = " ".join(context.args)
    if not password:
        await send_text(update, "Usage: /password YOUR_2FA_PASSWORD")
        return
    try:
        await telethon_client.sign_in(password=password)
        await save_session(state["phone"])
        login_state.pop(user_id, None)
        await send_text(update, "✅ Telegram account connected. Telethon session saved in PostgreSQL.")
    except Exception as exc:
        log.exception("2FA verification failed")
        await send_text(update, f"⚠️ 2FA verification failed: {str(exc)[:180]}")


async def cancel_login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    login_state.pop(update.effective_user.id, None)
    await send_text(update, "Login flow cancelled. If Telegram sent a code, do not share it.")


async def session_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not owner_ok(update.effective_user.id):
        await send_text(update, "⛔ Owner only.")
        return
    ok = bool(telethon_client and await telethon_client.is_user_authorized())
    await send_text(update, "✅ Telethon is connected." if ok else "❌ Not connected. Use /login.")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg or not msg.text:
        return
    if update.effective_chat.type != ChatType.PRIVATE:
        await send_text(update, "Please use this bot in a private chat.")
        return
    text = msg.text.strip()
    state = login_state.get(user.id)
    if owner_ok(user.id) and state and state.get("stage") == "phone" and not text.startswith("/"):
        await begin_phone_login(update, text)
        return
    if text.startswith("/"):
        return
    await send_text(update, "Unknown command. Send /help.")


async def lookup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_chat or update.effective_chat.type != ChatType.PRIVATE:
        await send_text(update, "Please use this bot in a private chat.")
        return
    command = update.effective_message.text.split()[0].split("@")[0].lstrip("/").lower()
    if command in RESTRICTED_COMMANDS:
        await send_text(update, "⛔ This lookup is disabled because it can expose sensitive personal, identity, family, contact, vehicle-owner, account or device information.")
        return
    if command not in PUBLIC_COMMANDS:
        await send_text(update, "Unknown or unavailable command. Send /help.")
        return
    query = " ".join(context.args).strip()
    if not query or len(query) > 250:
        await send_text(update, f"Usage: /{command} <value>\nMaximum query length: 250 characters.")
        return
    if command == "ip":
        try:
            ipaddress.ip_address(query)
        except ValueError:
            await send_text(update, "⚠️ Invalid IP address. Usage: /ip 8.8.8.8")
            return
    if not telethon_client or not await telethon_client.is_user_authorized():
        await send_text(update, "⚠️ Lookup relay is not connected. Please contact the administrator.")
        return
    request_id = str(uuid.uuid4())
    try:
        sent = await telethon_client.send_message(int(GROUP_ID), f"/{command} {query}")
        async with pool.acquire() as con:
            await con.execute("INSERT INTO gz_query_requests(request_id, command, query_text, telegram_message_id, status) VALUES($1,$2,$3,$4,'pending')", request_id, command, query, sent.id)
            await con.execute("INSERT INTO gz_bot_deliveries(request_id, chat_id, user_id) VALUES($1,$2,$3)", request_id, update.effective_chat.id, user.id)
        await send_text(update, "⏳ Request received. I'll send the response here when processing finishes.")
    except Exception:
        log.exception("Failed to send query to private group")
        await send_text(update, "⚠️ The lookup relay is temporarily unavailable. Please try again later.")


async def delivery_worker(app: Application):
    while True:
        try:
            async with pool.acquire() as con:
                rows = await con.fetch("""
                    SELECT d.request_id, d.chat_id, q.result, q.status
                    FROM gz_bot_deliveries d JOIN gz_query_requests q USING(request_id)
                    WHERE d.delivered=FALSE AND (q.status IN ('completed','error') OR q.created_at < NOW() - ($1 * INTERVAL '1 second'))
                    ORDER BY q.created_at LIMIT 25
                """, RESPONSE_TIMEOUT_SECONDS)
                for row in rows:
                    if row["status"] == "completed" and row["result"]:
                        body = "✅ SMART LOOKUP — RESULT\n\n" + row["result"]
                    elif row["status"] == "error":
                        body = "❌ LOOKUP FAILED\n\nThe upstream service could not complete this request. Please verify the input and retry."
                    else:
                        body = "⌛ LOOKUP TIMEOUT\n\nNo response was received before the timeout. Please try again later."
                    body = body[:3900 - len(SUPPORT_FOOTER)] + SUPPORT_FOOTER
                    try:
                        await app.bot.send_message(chat_id=row["chat_id"], text=body, disable_web_page_preview=True)
                        await con.execute("UPDATE gz_bot_deliveries SET delivered=TRUE WHERE request_id=$1", row["request_id"])
                        if row["status"] == "pending":
                            await con.execute("UPDATE gz_query_requests SET status='timeout', updated_at=NOW() WHERE request_id=$1", row["request_id"])
                    except Exception:
                        log.exception("Could not deliver result for %s", row["request_id"])
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Delivery worker failed")
        await asyncio.sleep(1)


async def post_init(app: Application):
    app.create_task(delivery_worker(app))


async def main():
    global bot_app
    required = ["BOT_TOKEN", "GROUP_ID", "TELETHON_API_ID", "TELETHON_API_HASH", "DATABASE_URL", "OWNER_ID"]
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise RuntimeError("Missing required environment variables: " + ", ".join(missing))
    await db_init()
    saved = await load_session()
    await connect_telethon(saved)
    if await telethon_client.is_user_authorized():
        log.info("Telethon session loaded and authorized")
    else:
        log.warning("Telethon is not authorized yet. Owner must use /login in Bot 2")
    bot_app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    bot_app.add_handler(CommandHandler(["start", "help"], start_command))
    bot_app.add_handler(CommandHandler("id", id_command))
    bot_app.add_handler(CommandHandler("login", login_command))
    bot_app.add_handler(CommandHandler("otp", otp_command))
    bot_app.add_handler(CommandHandler("password", password_command))
    bot_app.add_handler(CommandHandler(["cancel_login", "cancel"], cancel_login))
    bot_app.add_handler(CommandHandler("session", session_command))
    for cmd in set(PUBLIC_COMMANDS) | RESTRICTED_COMMANDS:
        bot_app.add_handler(CommandHandler(cmd, lookup_command))
    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    log.info("GAMERZONE Python bot starting")
    await bot_app.initialize()
    await bot_app.start()
    await bot_app.updater.start_polling(drop_pending_updates=True)
    try:
        await asyncio.Event().wait()
    finally:
        await bot_app.updater.stop()
        await bot_app.stop()
        await bot_app.shutdown()
        if telethon_client:
            await telethon_client.disconnect()
        if pool:
            await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
