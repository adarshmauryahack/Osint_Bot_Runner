# OSINT Bot Runner — Telegram Bot Hosting Dashboard

This project recreates the dark navy/cyan mobile dashboard style from the supplied screenshots and adds a real Flask backend.

## Included real features

- Admin password login.
- Add Telegram bots and encrypt their bot tokens at rest.
- Validate bot tokens with Telegram when starting.
- Start/stop polling workers from the dashboard.
- Telegram `/start`, `/help`, and `/status` commands.
- Optional required-channel membership check using Telegram `getChatMember`.
- Delete bot configurations.
- Search/filter bot configurations in the dashboard.
- Statistics are calculated from the database, not hardcoded.
- Health endpoint at `/api/health`.

## Safety scope

This starter intentionally does not connect to private-record lookup services. It does not retrieve Aadhaar data, private addresses, phone subscriber records, live location, or other sensitive personal information. `/lookup` currently returns a notice that lookup integration is not enabled. Only public, non-sensitive information or data the operator is authorized to access should be integrated later.

YouTube subscriber verification is shown as an optional information link only. Subscriber status cannot be verified by this app without a properly configured YouTube API integration.

## Run locally

Python 3.11+ is recommended.

1. Create a virtual environment and install dependencies:

   ```bash
   python -m venv .venv
   # Windows:
   .venv\Scripts\activate
   # macOS/Linux:
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. Generate secrets:

   ```bash
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```

3. Set environment variables before starting:

   **Linux/macOS**
   ```bash
   export FLASK_SECRET_KEY="$(python -c 'import secrets; print(secrets.token_hex(32))')"
   export TOKEN_ENCRYPTION_KEY="PASTE_GENERATED_FERNET_KEY"
   export ADMIN_PASSWORD="Use-A-Unique-Password-At-Least-12-Chars"
   export COOKIE_SECURE="false"
   ```

   **Windows PowerShell**
   ```powershell
   $env:FLASK_SECRET_KEY = python -c "import secrets; print(secrets.token_hex(32))"
   $env:TOKEN_ENCRYPTION_KEY = "PASTE_GENERATED_FERNET_KEY"
   $env:ADMIN_PASSWORD = "Use-A-Unique-Password-At-Least-12-Chars"
   $env:COOKIE_SECURE = "false"
   ```

4. Start:

   ```bash
   python main.py
   ```

5. Open `http://127.0.0.1:5000`.

Keep the encryption key safe. If `TOKEN_ENCRYPTION_KEY` is lost or changed, previously stored bot tokens cannot be decrypted.

## Deploy on Render

1. Upload the contents of this folder to a new GitHub repository.
2. In Render, choose **New + → Blueprint** and select the repository, or create a Python Web Service.
3. Use:
   - Build command: `pip install -r requirements.txt`
   - Start command: `gunicorn main:app --workers 1 --threads 8 --timeout 120`
4. Add environment variables:
   - `FLASK_SECRET_KEY`: long random secret.
   - `TOKEN_ENCRYPTION_KEY`: output of the Fernet key command above.
   - `ADMIN_PASSWORD`: unique password (12+ characters).
   - `DATABASE_URL`: PostgreSQL connection URL. Use a managed PostgreSQL database such as Supabase; Render's local filesystem may be ephemeral.
   - `COOKIE_SECURE`: `true`.
5. Deploy and open `/api/health`; it should return `{"ok":true,...}`.
6. Open the site and log in with `ADMIN_PASSWORD`.

### Database note

For reliable persistence across Render restarts/redeploys, configure `DATABASE_URL` to a PostgreSQL database. If omitted, the app uses a local SQLite file, which may be lost on ephemeral hosting filesystems.

### Telegram setup

1. Create a bot with Telegram's official `@BotFather`.
2. Add the bot to the channel that will be used for membership verification.
3. Make the bot an administrator in that channel so Telegram can answer `getChatMember` reliably.
4. Add the bot in the dashboard with its token, channel link, and channel username or numeric channel ID.
5. Click **Start**. The app calls Telegram `getMe` first; an invalid token will not be marked as running.

## Operational limitations

- The Render free web service may sleep when idle; it is not guaranteed 24/7 hosting.
- The polling workers run inside this single web process. Keep one Gunicorn worker as configured; multiple web instances/workers would require a dedicated worker architecture and shared coordination.
- Store secrets only in Render environment variables. Never paste a bot token into public GitHub files or screenshots.
- Telegram bot tokens are secrets. If a token was exposed, revoke it via BotFather and issue a new one.
