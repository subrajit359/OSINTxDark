import asyncio
import io
import logging
import os
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
from telegram.ext import Application, MessageHandler, ContextTypes, filters

load_dotenv()
_log_level_name = os.getenv("LOG_LEVEL", "INFO").strip().upper()
_log_level = getattr(logging, _log_level_name, logging.INFO)
if not isinstance(_log_level, int):
    _log_level = logging.INFO
logging.basicConfig(level=_log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("gamerzone")

# Keep the user's existing Railway variable names unchanged.
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = os.getenv("OWNER_ID", "").strip()
GROUP_ID = os.getenv("GROUP_ID", "").strip()
BOT1_ID = os.getenv("BOT1_ID", "").strip()
TELETHON_API_ID = os.getenv("TELETHON_API_ID", "").strip()
TELETHON_API_HASH = os.getenv("TELETHON_API_HASH", "").strip()
TELETHON_PHONE = os.getenv("TELETHON_PHONE", "").strip()
TELETHON_SESSION = os.getenv("TELETHON_SESSION", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
RESPONSE_TIMEOUT_SECONDS = float(os.getenv("RESPONSE_TIMEOUT_SECONDS", "3600"))
RESPONSE_SETTLE_SECONDS = float(os.getenv("RESPONSE_SETTLE_SECONDS", "1.0"))

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
            result_media BYTEA,
            result_mime_type TEXT,
            result_file_name TEXT,
            result_caption TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """)
        await con.execute("ALTER TABLE gz_query_requests ADD COLUMN IF NOT EXISTS bot_reply_message_id BIGINT")
        await con.execute("ALTER TABLE gz_query_requests ADD COLUMN IF NOT EXISTS result_media BYTEA")
        await con.execute("ALTER TABLE gz_query_requests ADD COLUMN IF NOT EXISTS result_mime_type TEXT")
        await con.execute("ALTER TABLE gz_query_requests ADD COLUMN IF NOT EXISTS result_file_name TEXT")
        await con.execute("ALTER TABLE gz_query_requests ADD COLUMN IF NOT EXISTS result_caption TEXT")
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
    """Capture Bot 1's reply to a request and associate it with its sender."""
    try:
        reply_id = event.message.reply_to_msg_id
        if not reply_id:
            return
        sender = await event.get_sender()
        sender_id = getattr(sender, "id", None)
        if BOT1_ID and str(sender_id) != BOT1_ID:
            return
        async with pool.acquire() as con:
            row = await con.fetchrow(
                "SELECT request_id FROM gz_query_requests WHERE (telegram_message_id=$1 OR bot_reply_message_id=$1) AND status IN ('pending','processing') LIMIT 1",
                int(reply_id),
            )
            if not row:
                return
            message = event.message
            text = (message.raw_text or "").strip()
            media_bytes = None
            mime_type = None
            file_name = None
            if message.media:
                media_bytes = await event.download_media(bytes)
                if message.photo:
                    mime_type = "image/jpeg"
                    file_name = "response.jpg"
                elif message.document:
                    mime_type = message.document.mime_type or "application/octet-stream"
                    file_name = getattr(message.file, "name", None) or "response.bin"
            if not text and not media_bytes:
                return
            await asyncio.sleep(RESPONSE_SETTLE_SECONDS)
            await con.execute(
                "UPDATE gz_query_requests SET status='completed', result=$2, result_media=$3, result_mime_type=$4, result_file_name=$5, result_caption=$6, bot_reply_message_id=$7, updated_at=NOW() WHERE request_id=$1",
                row["request_id"], text[:50000], media_bytes, mime_type, file_name,
                (message.message or "")[:1000], int(message.id),
            )
            log.info("Captured Bot 1 reply for request %s", row["request_id"])
    except Exception:
        log.exception("Failed to process private-group reply")


async def begin_login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start the owner-only Telethon login flow inside Bot 2 private chat."""
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return
    if not owner_ok(user.id):
        await msg.reply_text("This setup action is available only to the bot owner.")
        return
    if not TELETHON_API_ID or not TELETHON_API_HASH:
        await msg.reply_text("Set TELETHON_API_ID and TELETHON_API_HASH in Render Environment first.")
        return
    login_state[user.id] = {"step": "phone"}
    await msg.reply_text("Send your Telegram phone number with country code (example: +91XXXXXXXXXX). Send /cancel to stop.")


async def process_login_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Consume owner login replies; return True when the message was handled."""
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg or not owner_ok(user.id) or user.id not in login_state:
        return False
    value = (msg.text or "").strip()
    state = login_state[user.id]
    if value.lower() == "/cancel":
        login_state.pop(user.id, None)
        await msg.reply_text("Login cancelled.")
        return True
    if not value:
        await msg.reply_text("Please send the requested value as text, or /cancel.")
        return True
    try:
        if state["step"] == "phone":
            phone = value.replace(" ", "")
            sent_code = await telethon_client.send_code_request(phone)
            state.update({"step": "code", "phone": phone, "phone_code_hash": sent_code.phone_code_hash})
            await msg.reply_text("Telegram login code requested. Send the code here.")
            return True
        if state["step"] == "code":
            await telethon_client.sign_in(
                phone=state["phone"], code=value,
                phone_code_hash=state["phone_code_hash"],
            )
        elif state["step"] == "password":
            await telethon_client.sign_in(password=value)
        if await telethon_client.is_user_authorized():
            await save_session(state["phone"])
            login_state.pop(user.id, None)
            await msg.reply_text("Telegram account authorized and session saved in PostgreSQL. Relay is ready.")
            log.info("Telethon login completed by owner; session saved to PostgreSQL")
            return True
    except SessionPasswordNeededError:
        state["step"] = "password"
        await msg.reply_text("Two-step verification is enabled. Send your Telegram 2FA password here.")
        return True
    except Exception as exc:
        log.warning("Telethon login step failed: %s", type(exc).__name__)
        await msg.reply_text(f"Login step failed ({type(exc).__name__}). Check the value and try again, or send /cancel.")
        return True
    await msg.reply_text("Authorization is not complete yet. Follow the next prompt or send /cancel.")
    return True


async def handle_incoming(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Forward all private-chat messages except /start, /help and owner-only /login."""
    user = update.effective_user
    chat = update.effective_chat
    msg = update.effective_message
    if not user or not chat or not msg or chat.type != ChatType.PRIVATE:
        return
    # These control commands are never relayed to the private group.
    # /login is accepted only from the configured owner.
    command = ""
    if msg.text and msg.text.lstrip().startswith("/"):
        command = msg.text.lstrip().split(maxsplit=1)[0].split("@", 1)[0].lower()
    if command in {"/start", "/help"}:
        return
    if command == "/login":
        if owner_ok(user.id):
            await begin_login(update, context)
        return
    if command == "/cancel" and owner_ok(user.id) and user.id in login_state:
        await process_login_message(update, context)
        return
    if await process_login_message(update, context):
        return
    if not telethon_client or not await telethon_client.is_user_authorized():
        if owner_ok(user.id):
            await msg.reply_text("Telegram account is not logged in yet. Send /login to connect it.")
        log.warning("Incoming message not relayed because Telethon is not authorized")
        return

    request_id = str(uuid.uuid4())
    body = msg.text or msg.caption or ""
    header = f"📨 New message\nUser ID: {user.id}\nName: {user.full_name}\nRequest: {request_id}"
    try:
        if msg.text is not None:
            sent = await telethon_client.send_message(int(GROUP_ID), header + "\n\n" + msg.text)
        elif msg.effective_attachment:
            attachment = msg.effective_attachment
            if isinstance(attachment, (list, tuple)):
                attachment = attachment[-1]
            file_obj = io.BytesIO()
            tg_file = await context.bot.get_file(attachment.file_id)
            await tg_file.download_to_memory(out=file_obj)
            file_obj.seek(0)
            sent = await telethon_client.send_file(
                int(GROUP_ID), file_obj,
                caption=(header + ("\n\n" + body if body else ""))[:1024],
                file_name=getattr(attachment, "file_name", None) or "user_upload.bin",
            )
        else:
            details = body
            if msg.location:
                details = f"Location: {msg.location.latitude}, {msg.location.longitude}"
            elif msg.contact:
                details = f"Contact: {msg.contact.first_name} {msg.contact.last_name or ''} | {msg.contact.phone_number}"
            elif msg.venue:
                details = f"Venue: {msg.venue.title} | {msg.venue.address} | {msg.venue.location.latitude}, {msg.venue.location.longitude}"
            elif msg.poll:
                details = f"Poll: {msg.poll.question}\n" + "\n".join(option.text for option in msg.poll.options)
            elif msg.dice:
                details = f"Dice: {msg.dice.emoji} {msg.dice.value}"
            sent = await telethon_client.send_message(int(GROUP_ID), header + "\n\n" + (details or "[Message received]"))
        group_message_id = sent.id if not isinstance(sent, list) else sent[0].id
        async with pool.acquire() as con:
            await con.execute(
                "INSERT INTO gz_query_requests(request_id, command, query_text, telegram_message_id, status) VALUES($1,$2,$3,$4,'pending')",
                request_id, "message", body[:50000], group_message_id,
            )
            await con.execute(
                "INSERT INTO gz_bot_deliveries(request_id, chat_id, user_id) VALUES($1,$2,$3)",
                request_id, chat.id, user.id,
            )
    except Exception:
        log.exception("Failed to forward incoming user message")


async def delivery_worker(app: Application):
    while True:
        try:
            async with pool.acquire() as con:
                rows = await con.fetch("""
                    SELECT d.request_id, d.chat_id, q.result, q.status,
                           q.result_media, q.result_mime_type, q.result_file_name, q.result_caption
                    FROM gz_bot_deliveries d JOIN gz_query_requests q USING(request_id)
                    WHERE d.delivered=FALSE AND q.status='completed'
                    ORDER BY q.created_at LIMIT 25
                """)
                for row in rows:
                    try:
                        caption = (row["result_caption"] or row["result"] or "Bot 1 response")[:1000]
                        media = row["result_media"]
                        if media:
                            file_obj = io.BytesIO(bytes(media))
                            file_obj.name = row["result_file_name"] or "response.bin"
                            mime = (row["result_mime_type"] or "").lower()
                            if mime.startswith("image/"):
                                await app.bot.send_photo(chat_id=row["chat_id"], photo=file_obj, caption=caption)
                            elif mime.startswith("video/"):
                                await app.bot.send_video(chat_id=row["chat_id"], video=file_obj, caption=caption)
                            elif mime.startswith("audio/"):
                                await app.bot.send_audio(chat_id=row["chat_id"], audio=file_obj, caption=caption)
                            else:
                                await app.bot.send_document(chat_id=row["chat_id"], document=file_obj, caption=caption)
                        elif row["result"]:
                            await app.bot.send_message(chat_id=row["chat_id"], text=row["result"][:4000], disable_web_page_preview=True)
                        else:
                            continue
                        await con.execute("UPDATE gz_bot_deliveries SET delivered=TRUE WHERE request_id=$1", row["request_id"])
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
    required = ["BOT_TOKEN", "GROUP_ID", "TELETHON_API_ID", "TELETHON_API_HASH", "DATABASE_URL"]
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise RuntimeError("Missing required environment variables: " + ", ".join(missing))
    await db_init()
    # Prefer the PostgreSQL session; use TELETHON_SESSION for the first deployment
    # or recovery when the database has no authorized session yet.
    saved = await load_session()
    if not saved:
        saved = TELETHON_SESSION
    await connect_telethon(saved)
    if await telethon_client.is_user_authorized():
        log.info("Telethon session loaded and authorized")
        if not await load_session():
            await save_session(TELETHON_PHONE or "configured-session")
            log.info("Initial Telethon session saved to PostgreSQL")
    else:
        log.warning("Telethon is not authorized yet. Bot 2 will start; the owner can send /login to authenticate.")
    bot_app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    bot_app.add_handler(MessageHandler(filters.ALL, handle_incoming))
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
