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
- `TELETHON_PHONE` — phone number used only by the local session generator
- `TELETHON_SESSION` — initial Telethon StringSession; after first successful boot it is saved to PostgreSQL
- `DATABASE_URL` — PostgreSQL connection URL
- `RESPONSE_SETTLE_SECONDS` — optional, default `1.0`
- `RESPONSE_TIMEOUT_SECONDS` — retained for compatibility; no timeout message is sent

The Telethon user session lets the relay observe Bot 1's group replies. The owner can authorize directly in Bot 2 with `/login`, then send the phone number, Telegram login code, and (if enabled) 2FA password. The temporary authorization key and phone-code hash are stored in PostgreSQL during the login flow, so a process restart does not silently replace the code request. Repeating `/login` while an attempt is pending does not request another code. `/cancel` clears the pending attempt. On success, the authorized session is saved in `gz_telegram_session`. Keep session credentials secret.

Run exactly one Render instance for this service. The PostgreSQL advisory lock prevents two workers sharing the same database from concurrently polling Bot 2 or handling the same login attempt.

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
