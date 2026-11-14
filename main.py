import os
import re
import time
import threading
import logging
from datetime import datetime, timezone
from functools import wraps

import requests
from cryptography.fernet import Fernet, InvalidToken
from flask import Flask, jsonify, request, session, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot-runner")

APP_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder="static", static_url_path="")
app.config["SECRET_KEY"] = os.environ.get("FLASK_SECRET_KEY", "")
if not app.config["SECRET_KEY"]:
    raise RuntimeError("Set FLASK_SECRET_KEY environment variable.")

db_url = os.environ.get("DATABASE_URL", f"sqlite:///{os.path.join(APP_DIR, 'bots.db')}")
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql+psycopg://", 1)
elif db_url.startswith("postgresql://") and "+psycopg" not in db_url:
    db_url = db_url.replace("postgresql://", "postgresql+psycopg://", 1)

app.config.update(
    SQLALCHEMY_DATABASE_URI=db_url,
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "true").lower() == "true",
)
db = SQLAlchemy(app)

fernet_key = os.environ.get("TOKEN_ENCRYPTION_KEY", "")
if not fernet_key:
    raise RuntimeError("Set TOKEN_ENCRYPTION_KEY environment variable.")
try:
    cipher = Fernet(fernet_key.encode())
except Exception as exc:
    raise RuntimeError("TOKEN_ENCRYPTION_KEY is invalid. Generate it with Fernet.generate_key().") from exc

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
if len(ADMIN_PASSWORD) < 12:
    raise RuntimeError("Set ADMIN_PASSWORD to a password of at least 12 characters.")

class BotConfig(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False)
    token_enc = db.Column(db.Text, nullable=False)
    channel_link = db.Column(db.String(300), nullable=False)
    channel_ref = db.Column(db.String(120), nullable=False)
    youtube_link = db.Column(db.String(300), nullable=True)
    verify_channel = db.Column(db.Boolean, nullable=False, default=True)
    active = db.Column(db.Boolean, nullable=False, default=False)
    username = db.Column(db.String(80), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    lookup_count = db.Column(db.Integer, nullable=False, default=0)
    success_count = db.Column(db.Integer, nullable=False, default=0)
    error_count = db.Column(db.Integer, nullable=False, default=0)

    def token(self):
        return cipher.decrypt(self.token_enc.encode()).decode()

class BotRunner:
    def __init__(self):
        self.lock = threading.RLock()
        self.workers = {}
        self.stops = {}

    def start(self, bot_id):
        with self.lock:
            if bot_id in self.workers and self.workers[bot_id].is_alive():
                return
            bot = db.session.get(BotConfig, bot_id)
            if not bot:
                raise ValueError("Bot not found")
            token = bot.token()
            me = tg_call(token, "getMe")
            if not me.get("ok"):
                raise ValueError("Telegram rejected this bot token.")
            bot.username = me["result"].get("username")
            bot.active = True
            db.session.commit()
            stop_event = threading.Event()
            thread = threading.Thread(
                target=self._poll, args=(bot_id, token, stop_event),
                daemon=True, name=f"telegram-bot-{bot_id}"
            )
            self.stops[bot_id] = stop_event
            self.workers[bot_id] = thread
            thread.start()

    def stop(self, bot_id):
        with self.lock:
            event = self.stops.get(bot_id)
            if event:
                event.set()
            bot = db.session.get(BotConfig, bot_id)
            if bot:
                bot.active = False
                db.session.commit()

    def _poll(self, bot_id, token, stop_event):
        offset = None
        log.info("Starting Telegram polling for bot %s", bot_id)
        try:
            while not stop_event.is_set():
                params = {"timeout": 20}
                if offset is not None:
                    params["offset"] = offset
                try:
                    response = requests.get(
                        f"https://api.telegram.org/bot{token}/getUpdates",
                        params=params, timeout=25
                    )
                    payload = response.json()
                    if not payload.get("ok"):
                        raise RuntimeError(payload.get("description", "Telegram polling failed"))
                    for update in payload.get("result", []):
                        offset = update["update_id"] + 1
                        self._handle_update(bot_id, token, update)
                except Exception:
                    log.exception("Polling error for bot %s", bot_id)
                    with app.app_context():
                        bot = db.session.get(BotConfig, bot_id)
                        if bot:
                            bot.error_count += 1
                            db.session.commit()
                    stop_event.wait(5)
        finally:
            with app.app_context():
                bot = db.session.get(BotConfig, bot_id)
                if bot and stop_event.is_set():
                    bot.active = False
                    db.session.commit()
            with self.lock:
                self.workers.pop(bot_id, None)
                self.stops.pop(bot_id, None)
            log.info("Stopped Telegram polling for bot %s", bot_id)

    def _handle_update(self, bot_id, token, update):
        message = update.get("message") or update.get("edited_message")
        if not message or not message.get("text"):
            return
        chat_id = message["chat"]["id"]
        user_id = message.get("from", {}).get("id")
        text = message["text"].strip()
        with app.app_context():
            bot = db.session.get(BotConfig, bot_id)
            if not bot:
                return
            try:
                if text.startswith("/start") and bot.verify_channel:
                    member = tg_call(token, "getChatMember", {
                        "chat_id": bot.channel_ref, "user_id": user_id
                    })
                    status = (member.get("result") or {}).get("status")
                    if not member.get("ok") or status not in ("creator", "administrator", "member"):
                        send_message(token, chat_id,
                            "Please join the required channel first, then send /start again:\n" + bot.channel_link)
                        return
                if text.startswith("/start"):
                    send_message(token, chat_id,
                        f"Welcome to {bot.name}!\n\nAvailable commands:\n/help - Show help\n/status - Bot status\n\nThis bot only supports public or explicitly authorized information.")
                elif text.startswith("/help"):
                    send_message(token, chat_id,
                        "Commands:\n/start - Start the bot\n/help - Show help\n/status - Check bot status\n\nPrivate records, Aadhaar details, home addresses, live tracking, and other sensitive personal data are not provided.")
                elif text.startswith("/status"):
                    send_message(token, chat_id, "Bot is running and connected to Telegram.")
                elif text.startswith("/lookup"):
                    send_message(token, chat_id,
                        "Lookup is not enabled in this starter build. Only public, non-sensitive information or data you are authorized to access should be integrated.")
                    bot.lookup_count += 1
                    bot.success_count += 1
                db.session.commit()
            except Exception:
                log.exception("Update handling failed for bot %s", bot_id)
                bot.error_count += 1
                db.session.commit()

runner = BotRunner()

def tg_call(token, method, data=None):
    response = requests.post(
        f"https://api.telegram.org/bot{token}/{method}",
        json=data or {}, timeout=15
    )
    return response.json()

def send_message(token, chat_id, text):
    return tg_call(token, "sendMessage", {"chat_id": chat_id, "text": text})

def login_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not session.get("admin"):
            return jsonify({"error": "Authentication required"}), 401
        return fn(*args, **kwargs)
    return wrapped

def serialize(bot):
    return {
        "id": bot.id, "name": bot.name, "username": bot.username or "Not checked",
        "channel_link": bot.channel_link, "channel_ref": bot.channel_ref,
        "youtube_link": bot.youtube_link or "", "verify_channel": bot.verify_channel,
        "active": bot.active, "created_at": bot.created_at.isoformat(),
    }

@app.get("/")
def index():
    return send_from_directory(APP_DIR, "index.html")

@app.get("/api/health")
def health():
    return jsonify({"ok": True, "service": "OSINT Bot Runner", "time": datetime.now(timezone.utc).isoformat()})

@app.post("/api/login")
def login():
    data = request.get_json(silent=True) or {}
    if data.get("password", "") != ADMIN_PASSWORD:
        return jsonify({"error": "Incorrect admin password"}), 401
    session.clear()
    session["admin"] = True
    return jsonify({"ok": True})

@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify({"ok": True})

@app.get("/api/me")
def me():
    return jsonify({"authenticated": bool(session.get("admin"))})

@app.get("/api/dashboard")
@login_required
def dashboard():
    bots = BotConfig.query.order_by(BotConfig.id.desc()).all()
    total = len(bots)
    running = sum(1 for b in bots if b.active)
    lookups = sum(b.lookup_count for b in bots)
    success = sum(b.success_count for b in bots)
    errors = sum(b.error_count for b in bots)
    attempts = success + errors
    rate = round((success / attempts) * 100, 1) if attempts else 0
    return jsonify({
        "bots": [serialize(b) for b in bots],
        "total_configs": total, "running_bots": running,
        "total_lookups": lookups, "api_success_rate": rate,
        "server_status": "online"
    })

@app.post("/api/bots")
@login_required
def add_bot():
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()
    token = str(data.get("token", "")).strip()
    channel_link = str(data.get("channel_link", "")).strip()
    channel_ref = str(data.get("channel_ref", "")).strip()
    youtube_link = str(data.get("youtube_link", "")).strip()
    if not name or len(name) > 80:
        return jsonify({"error": "Bot name is required (max 80 characters)."}), 400
    if not re.fullmatch(r"\d{5,15}:[A-Za-z0-9_-]{20,}", token):
        return jsonify({"error": "Telegram token format looks invalid."}), 400
    if not channel_link.startswith("https://t.me/"):
        return jsonify({"error": "Use a valid https://t.me/ channel link."}), 400
    if not channel_ref:
        return jsonify({"error": "Channel username or numeric ID is required."}), 400
    if channel_ref.startswith("https://"):
        return jsonify({"error": "Enter @channelname or numeric channel ID, not a URL."}), 400
    bot = BotConfig(
        name=name, token_enc=cipher.encrypt(token.encode()).decode(),
        channel_link=channel_link, channel_ref=channel_ref,
        youtube_link=youtube_link or None,
        verify_channel=bool(data.get("verify_channel", True)), active=False
    )
    db.session.add(bot)
    db.session.commit()
    return jsonify({"ok": True, "bot": serialize(bot)}), 201

@app.post("/api/bots/<int:bot_id>/start")
@login_required
def start_bot(bot_id):
    try:
        runner.start(bot_id)
        return jsonify({"ok": True, "message": "Telegram bot started."})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception:
        log.exception("Unable to start bot %s", bot_id)
        return jsonify({"error": "Could not start bot. Check token and server logs."}), 400

@app.post("/api/bots/<int:bot_id>/stop")
@login_required
def stop_bot(bot_id):
    if not db.session.get(BotConfig, bot_id):
        return jsonify({"error": "Bot not found"}), 404
    runner.stop(bot_id)
    return jsonify({"ok": True, "message": "Bot stopped."})

@app.delete("/api/bots/<int:bot_id>")
@login_required
def delete_bot(bot_id):
    bot = db.session.get(BotConfig, bot_id)
    if not bot:
        return jsonify({"error": "Bot not found"}), 404
    runner.stop(bot_id)
    db.session.delete(bot)
    db.session.commit()
    return jsonify({"ok": True})

with app.app_context():
    db.create_all()
    # Restore bots marked active after a process restart.
    active_ids = [b.id for b in BotConfig.query.filter_by(active=True).all()]
    for active_id in active_ids:
        try:
            runner.start(active_id)
        except Exception:
            log.exception("Could not restore bot %s", active_id)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
