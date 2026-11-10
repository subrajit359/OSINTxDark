import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone

import asyncpg
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message
from cryptography.fernet import Fernet, InvalidToken
from telethon import TelegramClient, events
from telethon.errors import (
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)
from telethon.sessions import StringSession

# ----------------------------- Configuration -----------------------------

BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
GROUP_ID = int(os.environ["GROUP_ID"])
BOT1_ID = int(os.environ["BOT1_ID"])
DATABASE_URL = os.environ["DATABASE_URL"]
# Keep this key unchanged: it encrypts/decrypts the Telethon session stored in PostgreSQL.
SESSION_ENCRYPTION_KEY = "t9Shlqawj8CSegGkCtMyOczWAjsoR8jZ788t2Z_UO6g="
REQUEST_TIMEOUT_SECONDS = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "120"))
COOLDOWN_SECONDS = 30

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("telegram-relay")

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
db: asyncpg.Pool | None = None
user_client: TelegramClient | None = None
telethon_lock = asyncio.Lock()
cooldown_lock = asyncio.Lock()
pending_timeout_tasks: dict[int, asyncio.Task] = {}
fernet = Fernet(SESSION_ENCRYPTION_KEY.encode())


class LoginFlow(StatesGroup):
    phone = State()
    code = State()
    password = State()


# Admin login is intentionally in-memory: an interrupted OTP flow must be
# restarted rather than leaving an OTP/password challenge in the database.
login_attempt: dict = {}


def is_owner(message: Message) -> bool:
    return bool(message.from_user and message.from_user.id == OWNER_ID)


def encrypt_session(session_string: str) -> str:
    return fernet.encrypt(session_string.encode()).decode()


def decrypt_session(encrypted: str) -> str:
    return fernet.decrypt(encrypted.encode()).decode()


async def init_db():
    global db
    db = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    async with db.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS telegram_account_session (
                id SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
                phone_number TEXT NOT NULL,
                encrypted_session TEXT NOT NULL,
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS relay_pending (
                group_message_id BIGINT PRIMARY KEY,
                user_id BIGINT NOT NULL,
                request_text TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS relay_pending_created_idx
                ON relay_pending(created_at);
            CREATE TABLE IF NOT EXISTS relay_cooldowns (
                user_id BIGINT PRIMARY KEY,
                accepted_at TIMESTAMPTZ NOT NULL
            );
        """)


async def load_saved_telethon_session() -> str | None:
    async with db.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT encrypted_session
            FROM telegram_account_session
            WHERE id = 1 AND is_active = TRUE
        """)
    if not row:
        return None
    try:
        return decrypt_session(row["encrypted_session"])
    except (InvalidToken, ValueError):
        log.exception("Stored Telethon session could not be decrypted.")
        raise RuntimeError(
            "The saved session cannot be decrypted. The hardcoded session encryption key may have changed."
        )


async def save_telethon_session(phone: str, session_string: str):
    encrypted = encrypt_session(session_string)
    async with db.acquire() as conn:
        await conn.execute("""
            INSERT INTO telegram_account_session
                (id, phone_number, encrypted_session, is_active, updated_at)
            VALUES (1, $1, $2, TRUE, NOW())
            ON CONFLICT (id) DO UPDATE SET
                phone_number = EXCLUDED.phone_number,
                encrypted_session = EXCLUDED.encrypted_session,
                is_active = TRUE,
                updated_at = NOW()
        """, phone, encrypted)


async def clear_telethon_session():
    async with db.acquire() as conn:
        await conn.execute("DELETE FROM telegram_account_session WHERE id = 1")


async def connect_user_client(session_string: str | None = None):
    """Create exactly one Telethon client in this process and verify its auth."""
    global user_client
    async with telethon_lock:
        if user_client is not None:
            try:
                if user_client.is_connected() and await user_client.is_user_authorized():
                    return user_client
            except Exception:
                log.exception("Existing Telethon client failed health check.")
            try:
                await user_client.disconnect()
            except Exception:
                log.exception("Error disconnecting old Telethon client.")
            user_client = None

        client = TelegramClient(
            StringSession(session_string or ""),
            API_ID,
            API_HASH,
            connection_retries=5,
            request_retries=3,
            auto_reconnect=True,
        )
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            return None

        @client.on(events.NewMessage(chats=GROUP_ID))
        async def on_group_message(event):
            try:
                # Only accept messages authored by the configured Bot 1.
                if event.sender_id != BOT1_ID:
                    return
                # Correlation is intentionally strict: Bot 1 must reply to the
                # exact group message sent by the user account.
                replied_to = event.message.reply_to_msg_id
                if not replied_to:
                    log.warning(
                        "Ignored Bot 1 message %s: it was not a reply to a request.",
                        event.message.id,
                    )
                    return
                async with db.acquire() as conn:
                    row = await conn.fetchrow("""
                        DELETE FROM relay_pending
                        WHERE group_message_id = $1
                        RETURNING user_id
                    """, replied_to)
                if not row:
                    log.info("No pending request found for group message %s", replied_to)
                    return
                response_text = event.raw_text or ""
                if not response_text:
                    return
                try:
                    await bot.send_message(
                        chat_id=row["user_id"],
                        text=response_text,
                        parse_mode=None,
                        disable_web_page_preview=True,
                    )
                except Exception:
                    log.exception("Could not deliver Bot 1 response to user %s", row["user_id"])
                task = pending_timeout_tasks.pop(replied_to, None)
                if task:
                    task.cancel()
            except Exception:
                log.exception("Error processing a group response.")

        user_client = client
        log.info("Telethon account connected and authorized.")
        return user_client


async def timeout_request(group_message_id: int, user_id: int):
    try:
        await asyncio.sleep(REQUEST_TIMEOUT_SECONDS)
        async with db.acquire() as conn:
            row = await conn.fetchrow("""
                DELETE FROM relay_pending
                WHERE group_message_id = $1
                RETURNING user_id
            """, group_message_id)
        if row:
            await bot.send_message(
                user_id,
                "⏳ No response received yet. Please try again later.",
                parse_mode=None,
            )
    except asyncio.CancelledError:
        pass
    except Exception:
        log.exception("Request timeout handler failed.")


async def check_and_set_cooldown(user_id: int) -> int:
    """Return remaining cooldown seconds; zero means accepted."""
    now = datetime.now(timezone.utc)
    async with cooldown_lock:
        async with db.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow("""
                    SELECT accepted_at FROM relay_cooldowns
                    WHERE user_id = $1 FOR UPDATE
                """, user_id)
                if row:
                    elapsed = (now - row["accepted_at"]).total_seconds()
                    remaining = int(COOLDOWN_SECONDS - elapsed + 0.999)
                    if remaining > 0:
                        return remaining
                await conn.execute("""
                    INSERT INTO relay_cooldowns(user_id, accepted_at)
                    VALUES($1, $2)
                    ON CONFLICT(user_id) DO UPDATE
                    SET accepted_at = EXCLUDED.accepted_at
                """, user_id, now)
    return 0


# --------------------------------- Admin ----------------------------------

@dp.message(Command("start"))
async def start_command(message: Message):
    await message.answer(
        "Send me a text message and I'll pass it to the processing group."
    )


@dp.message(Command("login"))
async def login_command(message: Message, state: FSMContext):
    if not is_owner(message):
        await message.answer("⛔ Unauthorized access.")
        return
    if user_client is not None:
        try:
            if user_client.is_connected() and await user_client.is_user_authorized():
                await message.answer("✅ Telegram account is already logged in. Use /status.")
                return
        except Exception:
            pass
    await state.clear()
    login_attempt.clear()
    await state.set_state(LoginFlow.phone)
    await message.answer("Send the phone number with country code, e.g. +91XXXXXXXXXX")


@dp.message(LoginFlow.phone)
async def login_phone(message: Message, state: FSMContext):
    if not is_owner(message):
        await state.clear()
        return
    if (message.text or "").strip() == "/cancel":
        client = login_attempt.get("client")
        if client:
            await client.disconnect()
        login_attempt.clear()
        await state.clear()
        await message.answer("Login flow cancelled.")
        return
    phone = (message.text or "").strip()
    if not phone.startswith("+") or not phone[1:].isdigit():
        await message.answer("Invalid phone number. Send it in international format, e.g. +91XXXXXXXXXX")
        return
    async with telethon_lock:
        global user_client
        if user_client is not None:
            try:
                await user_client.disconnect()
            except Exception:
                pass
            user_client = None
        client = TelegramClient(StringSession(), API_ID, API_HASH)
        await client.connect()
        try:
            sent = await client.send_code_request(phone)
        except PhoneNumberInvalidError:
            await client.disconnect()
            await message.answer("❌ Telegram rejected that phone number. Try /login again.")
            await state.clear()
            return
        except Exception:
            await client.disconnect()
            log.exception("Could not request Telegram login code.")
            await message.answer("❌ Could not request a login code. Check logs and try /login again.")
            await state.clear()
            return
        login_attempt["client"] = client
        login_attempt["phone"] = phone
        login_attempt["phone_code_hash"] = sent.phone_code_hash
    await state.set_state(LoginFlow.code)
    await message.answer("Telegram sent a login code. Send the code here.")


@dp.message(LoginFlow.code)
async def login_code(message: Message, state: FSMContext):
    if not is_owner(message):
        await state.clear()
        return
    if (message.text or "").strip() == "/cancel":
        client = login_attempt.get("client")
        if client:
            await client.disconnect()
        login_attempt.clear()
        await state.clear()
        await message.answer("Login flow cancelled.")
        return
    code = (message.text or "").replace(" ", "").strip()
    client = login_attempt.get("client")
    if client is None:
        await state.clear()
        await message.answer("Login attempt expired. Start again with /login.")
        return
    try:
        await client.sign_in(
            phone=login_attempt["phone"],
            code=code,
            phone_code_hash=login_attempt["phone_code_hash"],
        )
    except SessionPasswordNeededError:
        await state.set_state(LoginFlow.password)
        await message.answer("Two-step verification is enabled. Send your Telegram 2FA password.")
        return
    except (PhoneCodeInvalidError, PhoneCodeExpiredError):
        await message.answer("❌ Invalid or expired code. Send the latest code, or /cancel and restart.")
        return
    except Exception:
        log.exception("Telegram sign-in failed.")
        await message.answer("❌ Login failed. Use /cancel and try /login again.")
        return
    await finish_login(message, state, client)


@dp.message(LoginFlow.password)
async def login_password(message: Message, state: FSMContext):
    if not is_owner(message):
        await state.clear()
        return
    if (message.text or "").strip() == "/cancel":
        client = login_attempt.get("client")
        if client:
            await client.disconnect()
        login_attempt.clear()
        await state.clear()
        await message.answer("Login flow cancelled.")
        return
    client = login_attempt.get("client")
    if client is None:
        await state.clear()
        await message.answer("Login attempt expired. Start again with /login.")
        return
    try:
        await client.sign_in(password=message.text or "")
    except Exception:
        log.exception("Telegram 2FA sign-in failed.")
        await message.answer("❌ Incorrect password or login error. Try again, or /cancel.")
        return
    await finish_login(message, state, client)


async def finish_login(message: Message, state: FSMContext, client: TelegramClient):
    global user_client
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("Telegram did not authorize the account.")
        me = await client.get_me()
        session_string = client.session.save()
        await save_telethon_session(login_attempt["phone"], session_string)
        # Attach the group listener and promote this authenticated client.
        await client.disconnect()
        login_attempt.clear()
        await state.clear()
        connected = await connect_user_client(session_string)
        if connected is None:
            raise RuntimeError("Could not restore the newly saved session.")
        await message.answer(
            f"✅ Telegram account connected successfully.\n"
            f"Account: {me.phone or 'Hidden'}\n"
            f"Session securely saved in PostgreSQL."
        )
    except Exception:
        log.exception("Could not finish Telegram login.")
        try:
            await client.disconnect()
        except Exception:
            pass
        login_attempt.clear()
        await state.clear()
        await message.answer("❌ Login could not be completed. Check Railway logs and try /login again.")


@dp.message(Command("cancel"))
async def cancel_command(message: Message, state: FSMContext):
    if not is_owner(message):
        await message.answer("⛔ Unauthorized access.")
        return
    client = login_attempt.get("client")
    if client:
        try:
            await client.disconnect()
        except Exception:
            pass
    login_attempt.clear()
    await state.clear()
    await message.answer("Login flow cancelled.")


@dp.message(Command("status"))
async def status_command(message: Message):
    if not is_owner(message):
        await message.answer("⛔ Unauthorized access.")
        return
    try:
        if user_client and user_client.is_connected() and await user_client.is_user_authorized():
            me = await user_client.get_me()
            await message.answer(f"✅ Connected as {me.phone or 'Telegram account'}.")
            return
        saved = await load_saved_telethon_session()
        if saved:
            await message.answer("⚠️ A session is saved, but the Telegram client is not connected.")
        else:
            await message.answer("❌ No Telegram account is logged in. Use /login.")
    except Exception:
        log.exception("Status check failed.")
        await message.answer("❌ Could not check Telegram account status.")


@dp.message(Command("logout"))
async def logout_command(message: Message):
    global user_client
    if not is_owner(message):
        await message.answer("⛔ Unauthorized access.")
        return
    async with telethon_lock:
        if user_client:
            try:
                await user_client.disconnect()
            except Exception:
                log.exception("Telethon disconnect failed.")
            user_client = None
        await clear_telethon_session()
    await message.answer("✅ Telethon session removed from the database. Use /login to connect again.")


# ----------------------------- Public requests ----------------------------

@dp.message(F.text)
async def handle_user_text(message: Message):
    if not message.from_user:
        return
    # Admin commands and other slash commands are not forwarded as requests.
    if (message.text or "").startswith("/"):
        return
    remaining = await check_and_set_cooldown(message.from_user.id)
    if remaining:
        await message.answer(f"⏳ Please wait {remaining} seconds before sending another message.")
        return

    client = user_client
    if client is None or not client.is_connected():
        await message.answer("⚠️ The service is temporarily unavailable. Please try again later.")
        return
    try:
        sent = await client.send_message(GROUP_ID, message.text)
        async with db.acquire() as conn:
            await conn.execute("""
                INSERT INTO relay_pending(group_message_id, user_id, request_text)
                VALUES($1, $2, $3)
                ON CONFLICT(group_message_id) DO UPDATE
                SET user_id = EXCLUDED.user_id,
                    request_text = EXCLUDED.request_text,
                    created_at = NOW()
            """, sent.id, message.from_user.id, message.text)
        task = asyncio.create_task(timeout_request(sent.id, message.from_user.id))
        pending_timeout_tasks[sent.id] = task
        await message.answer("✅ Your text request has been sent.")
    except Exception:
        log.exception("Could not forward request to the private group.")
        await message.answer("❌ Could not send your request right now. Please try again later.")


@dp.message()
async def reject_non_text(message: Message):
    if message.from_user and message.text is None:
        await message.answer("Please send a text message only. Photos, videos, files, audio and stickers aren't supported.")


async def restore_telethon():
    try:
        saved = await load_saved_telethon_session()
        if not saved:
            log.info("No saved Telegram user session. Owner can use /login.")
            return
        client = await connect_user_client(saved)
        if client is None:
            log.warning("Saved Telethon session is no longer authorized. Owner should use /login.")
        else:
            log.info("Saved Telethon session restored.")
    except Exception:
        log.exception("Could not restore saved Telethon session.")


async def main():
    await init_db()
    await restore_telethon()
    try:
        await dp.start_polling(bot)
    finally:
        global user_client
        if user_client:
            await user_client.disconnect()
        await bot.session.close()
        if db:
            await db.close()


if __name__ == "__main__":
    asyncio.run(main())
