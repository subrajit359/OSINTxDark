# GAMERZONE Smart Lookup — Python Telegram Bot

Single-folder Python service for Render (Background Worker) or Railway. It uses:
- `python-telegram-bot` for the public Bot 2 interface.
- Telethon for the owner-authorized Telegram account relay into the private group.
- PostgreSQL for the Telethon session and request/delivery tracking.

## Existing environment variable names

The application keeps the original variable names unchanged:

- `BOT_TOKEN`
- `GROUP_ID`
- `BOT1_ID` (retained for compatibility; reply correlation uses the reply-to message ID)
- `TELETHON_API_ID`
- `TELETHON_API_HASH`
- `TELETHON_PHONE` (optional default for `/login`)
- `DATABASE_URL`
- `RESPONSE_TIMEOUT_SECONDS`
- `RESPONSE_SETTLE_SECONDS`

Also set `OWNER_ID` to the owner's numeric Telegram user ID. Do not paste secrets into GitHub or share them in chat.

## Railway

- Root Directory: `/`
- Build: `pip install -r requirements.txt`
- Start: `python main.py`
- Add a PostgreSQL service and set `DATABASE_URL` to its valid connection URL (or use your existing Neon URL).


## Render deployment

Deploy this folder as a **Background Worker** (not a Web Service):

- Runtime: Python 3
- Build command: `pip install -r requirements.txt`
- Start command: `python main.py`
- Add all required environment variables listed above in the Render service settings. Set `LOG_LEVEL` to `INFO` (uppercase is recommended; lowercase is also accepted).
- Attach a PostgreSQL database and set `DATABASE_URL` to its internal connection URL when the database is in the same Render region.

The included `render.yaml` provides the worker build and start commands. It does not create or populate your secrets/database settings automatically.

## Owner login

In a private chat with Bot 2:

1. `/login` (or `/login +countrycode...`)
2. If prompted, send your phone number.
3. `/otp 12345`
4. If Telegram requires 2-step verification, `/password your-2fa-password`
5. `/session` checks connection status.

The OTP and password are held only in process memory during the login flow. Telegram message deletion is best-effort and cannot guarantee removal from every client notification/history.

## Commands and privacy

`/start`, `/help`, `/id`, `/login`, `/otp`, `/password`, `/cancel_login`, `/session`, `/ip`.

The legacy personal-data commands (phone-owner lookup, Aadhaar, PAN, family/ration records, phone-to-UPI, Telegram ID/username-to-phone, vehicle-owner-to-number and IMEI lookups) are deliberately not exposed by this public bot because they can disclose sensitive personal data. `/ip` accepts a syntactically valid IP address; actual lookup processing still depends on a permitted upstream service replying to the request in the private group.

## Important operational notes

- Bot 1 must already be present in the private group and process commands there.
- Add the Telethon account to that group and ensure it can send/read messages.
- The bot API account (Bot 2) and the Telethon user account are separate identities.
- The relay correlates replies by Telegram `reply_to_msg_id`. If Bot 1 does not reply to the exact request message (or its tracked intermediate reply), this minimal relay will not match it; test the actual Bot 1 reply pattern before production use.
- The Telethon session stored in PostgreSQL is highly sensitive. Restrict database access and rotate it if exposed.
- This is a Python migration scaffold and has not been tested against your live Telegram group, Bot 1, or Railway database.
