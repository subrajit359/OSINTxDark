# Telegram Relay Bot (single-folder project)

This is the first implementation of the agreed text-only relay workflow.

## What it does

- Bot 2 accepts plain text and command-style text requests, such as `/Info 8283839229` and `/details Jdjsj728`.
- A logged-in Telegram user account (Telethon) sends each request to the configured private group.
- Only messages from `BOT1_ID` are considered Bot 1 responses.
- Bot 1's response is delivered as its original text to the originating user.
- Each user has a 30-second cooldown.
- `/login`, `/logout`, and `/status` are restricted to `OWNER_ID`.
- Telethon session data is encrypted and stored in PostgreSQL, not in an environment variable.
- A saved session is restored when the Railway service restarts.

## Critical response-matching requirement

For safe concurrent users, **Bot 1 must reply directly to the exact group message sent by the Telethon account** (Telegram's reply-to feature). The code matches Bot 1's response using `reply_to_msg_id`.

If Bot 1 sends a standalone message that is not a reply, the bot deliberately ignores it. Without a reply reference or another unique request identifier, it is not possible to reliably know which user should receive a standalone response when multiple requests are active.

## Files

Everything is in this one project folder:
- `main.py` — application
- `requirements.txt` — Python dependencies
- `.env.example` — required environment variables
- `Procfile` — Railway start command

## Railway setup

1. Create a Railway project and add a PostgreSQL database.
2. Deploy this folder from a GitHub repository.
3. Set the variables shown in `.env.example` in the Railway service. The session encryption key is hardcoded in `main.py`, so it is not a Railway variable.
4. Keep the hardcoded encryption key unchanged across deploys/restarts. It encrypts the database session.
5. Set the Railway service start command to `python main.py` (or use the included Procfile).
7. Set the service to **one replica**. Do not run multiple copies of the same Telethon account/session concurrently.

## First login

1. Start the service.
2. Open Bot 2 as the owner.
3. Send `/login`.
4. Enter the phone number in international format.
5. Enter the Telegram login code.
6. If requested, enter the Telegram 2-step verification password.
7. Use `/status` to verify the connection.

Admin commands:
- `/login`
- `/logout`
- `/status`
- `/cancel` (cancels an in-progress login)

## Notes and limitations in this initial version

- Text only, including slash-command-style requests. For example, `/Info 8283839229` and `/details Jdjsj728` are forwarded unchanged to the processing group.
- Bot management commands such as `/start`, `/login`, `/status`, `/logout`, and `/cancel` remain reserved for Bot 2 and are not forwarded.
- Photos, videos, documents, voice/audio, stickers and other non-text messages are rejected.
- Default Bot 1 response timeout is 120 seconds. Change `REQUEST_TIMEOUT_SECONDS` if needed.
- The 30-second cooldown is per Telegram user.
- A response must be a text reply to the exact forwarded group message.
- Bot 1's ID must be the numeric sender ID, not the username.
- Telegram login codes and 2FA passwords are entered into the private Bot 2 admin chat. Do not share them. Use a dedicated Telegram account for the relay if possible.
- The Fernet session encryption key is hardcoded in `main.py`. Keep it private and unchanged; changing it makes the existing encrypted session in PostgreSQL unreadable.
- Keep `DATABASE_URL` and all credentials private.

## Before production

This is a starter implementation and should be tested in a private test group first. Configure Telegram IDs correctly, confirm Bot 1 replies to the triggering message, and inspect Railway logs. Add a persistent queue/worker if you later need multiple replicas or high traffic.
