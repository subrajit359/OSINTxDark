# GAMERZONE Telegram Relay

Bot 2 is an automatic message relay. It has no public command menu or command restrictions.

## Message flow

1. Any message sent to Bot 2 in a private chat is forwarded to the configured private group.
2. The forwarded group message includes the sender's Telegram user ID and a unique request ID.
3. When Bot 1 replies directly to that forwarded message, the relay captures the reply.
4. Bot 2 sends the captured text or supported media to the original user.

Text, photos, videos, audio, documents, voice messages, stickers and other Telegram attachments supported by the Bot API are forwarded. Location, contact, venue, poll and dice messages are converted to readable text for the group.

## Environment variables

Keep these existing variable names in Railway:

- `BOT_TOKEN` — Bot 2 token
- `GROUP_ID` — private group ID
- `BOT1_ID` — Bot 1 numeric Telegram user ID; used to accept replies only from Bot 1
- `TELETHON_API_ID`
- `TELETHON_API_HASH`
- `TELETHON_PHONE` — retained for compatibility
- `DATABASE_URL` — PostgreSQL connection URL
- `RESPONSE_SETTLE_SECONDS` — optional, default `1.0`
- `RESPONSE_TIMEOUT_SECONDS` — retained for compatibility; no timeout message is sent

The Telethon user session must already be authorized and stored in the PostgreSQL `gz_telegram_session` table. The public bot has no `/login`, `/help`, `/start`, or lookup command handlers; every incoming private-chat message is treated as a user message and relayed.

## Railway deployment

- Root Directory: `/`
- Build command: `pip install -r requirements.txt`
- Start command: `python main.py`
- Attach PostgreSQL and set `DATABASE_URL`.

## Telegram setup

- Add Bot 1 to the private group and ensure it can process the forwarded messages.
- Add the Telethon account to the same group with permission to send and read messages.
- Configure `BOT1_ID` with Bot 1's numeric Telegram ID.
- Bot 1 must reply directly to the forwarded request message so the relay can associate the result with the correct user.
