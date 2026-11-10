"""Generate a Telethon StringSession for the relay's Render environment."""
import asyncio
import os
from getpass import getpass

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.sessions import StringSession

load_dotenv()

async def main():
    api_id = os.getenv("TELETHON_API_ID", "").strip()
    api_hash = os.getenv("TELETHON_API_HASH", "").strip()
    phone = os.getenv("TELETHON_PHONE", "").strip() or input("Telegram phone (+countrycode): ").strip()
    if not api_id or not api_hash:
        raise SystemExit("Set TELETHON_API_ID and TELETHON_API_HASH in .env first.")
    client = TelegramClient(StringSession(), int(api_id), api_hash)
    await client.connect()
    try:
        await client.send_code_request(phone)
        code = input("Telegram login code: ").strip().replace(" ", "")
        try:
            await client.sign_in(phone=phone, code=code)
        except SessionPasswordNeededError:
            await client.sign_in(password=getpass("Telegram 2FA password: "))
        if not await client.is_user_authorized():
            raise SystemExit("Telegram authorization failed.")
        print("\nTELETHON_SESSION (copy into Render environment; keep it secret):\n")
        print(client.session.save())
    finally:
        await client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
