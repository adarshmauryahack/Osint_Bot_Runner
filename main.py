import os
import re
import time
import threading
import logging
from datetime import datetime, timezone, timedelta
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

ADMIN_TELEGRAM_ID_RAW = os.environ.get("ADMIN_TELEGRAM_ID", "").strip()
if ADMIN_TELEGRAM_ID_RAW and not ADMIN_TELEGRAM_ID_RAW.isdigit():
    raise RuntimeError("ADMIN_TELEGRAM_ID must be your numeric Telegram user ID.")
ADMIN_TELEGRAM_ID = int(ADMIN_TELEGRAM_ID_RAW) if ADMIN_TELEGRAM_ID_RAW else None

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


class UsageEvent(db.Model):
    """Privacy-minimal event: never stores Telegram user IDs, chat IDs, or message text."""
    id = db.Column(db.Integer, primary_key=True)
    bot_id = db.Column(db.Integer, db.ForeignKey("bot_config.id", ondelete="CASCADE"), nullable=False, index=True)
    event_type = db.Column(db.String(40), nullable=False, index=True)
    success = db.Column(db.Boolean, nullable=False, default=True)
    duration_ms = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), index=True)


def record_usage(bot_id, event_type, success=True, duration_ms=0):
    """Record aggregate command metadata only; do not pass or persist user input."""
    db.session.add(UsageEvent(
        bot_id=bot_id,
        event_type=event_type[:40],
        success=bool(success),
        duration_ms=max(0, min(int(duration_ms or 0), 600000)),
    ))


def get_analytics(days=7):
    since = datetime.now(timezone.utc) - timedelta(days=days)
    events = UsageEvent.query.filter(UsageEvent.created_at >= since).all()
    daily = {}
    commands = {}
    by_bot = {}
    successful = 0
    failed = 0
    total_duration = 0
    for event in events:
        day = event.created_at.date().isoformat()
        daily[day] = daily.get(day, 0) + 1
        commands[event.event_type] = commands.get(event.event_type, 0) + 1
        by_bot[event.bot_id] = by_bot.get(event.bot_id, 0) + 1
        successful += int(event.success)
        failed += int(not event.success)
        total_duration += event.duration_ms
    bots = {b.id: b.name for b in BotConfig.query.all()}
    return {
        "period_days": days,
        "total_events": len(events),
        "successful_events": successful,
        "failed_events": failed,
        "success_rate": round(successful * 100 / len(events), 1) if events else 0,
        "average_duration_ms": round(total_duration / len(events)) if events else 0,
        "daily": [{"date": k, "count": v} for k, v in sorted(daily.items())],
        "commands": [{"name": k, "count": v} for k, v in sorted(commands.items(), key=lambda item: (-item[1], item[0]))],
        "bots": [{"bot_id": k, "name": bots.get(k, "Deleted bot"), "count": v} for k, v in sorted(by_bot.items(), key=lambda item: -item[1])],
    }


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
        raw_text = message["text"].strip()
        command = raw_text.split()[0].split("@")[0].lower() if raw_text else ""
        started = time.monotonic()
        with app.app_context():
            bot = db.session.get(BotConfig, bot_id)
            if not bot:
                return
            event_type = None
            event_success = True
            try:
                if command == "/start" and bot.verify_channel:
                    member = tg_call(token, "getChatMember", {
                        "chat_id": bot.channel_ref, "user_id": user_id
                    })
                    status = (member.get("result") or {}).get("status")
                    if not member.get("ok") or status not in ("creator", "administrator", "member"):
                        send_message(token, chat_id,
                            "Please join the required channel first, then send /start again:\\n" + bot.channel_link)
                        event_type, event_success = "start", False
                        elapsed = int((time.monotonic() - started) * 1000)
                        bot.error_count += 1
                        record_usage(bot_id, event_type, False, elapsed)
                        db.session.commit()
                        return
                    else:
                        event_type = "start"
                elif command == "/start":
                    event_type = "start"
                if command == "/start":
                    send_message(token, chat_id,
                        f"Welcome to {bot.name}!\\n\\nAvailable commands:\\n/help - Show help\\n/status - Bot status\\n/stats - Admin usage report (admin only)\\n\\nThis bot only supports public or explicitly authorized information.")
                elif command == "/help":
                    event_type = "help"
                    send_message(token, chat_id,
                        "Commands:\\n/start - Start the bot\\n/help - Show help\\n/status - Check bot status\\n/stats - Admin usage report (admin only)\\n\\nPrivate records, Aadhaar details, home addresses, live tracking, and other sensitive personal data are not provided.")
                elif command == "/status":
                    event_type = "status"
                    send_message(token, chat_id, "Bot is running and connected to Telegram.")
                elif command == "/lookup":
                    event_type, event_success = "lookup_disabled", False
                    send_message(token, chat_id,
                        "Lookup is not enabled in this build. Only public, non-sensitive information or data you are authorized to access should be integrated.")
                    bot.lookup_count += 1
                elif command in ("/admin", "/stats", "/report"):
                    event_type = "admin_report"
                    if ADMIN_TELEGRAM_ID is not None and user_id == ADMIN_TELEGRAM_ID:
                        report = get_analytics(days=7)
                        send_message(token, ADMIN_TELEGRAM_ID,
                            "ADMIN REPORT • LAST 7 DAYS\\n"
                            f"Events: {report['total_events']}\\n"
                            f"Successful: {report['successful_events']}\\n"
                            f"Failed: {report['failed_events']}\\n"
                            f"Success rate: {report['success_rate']}%\\n"
                            f"Average processing: {report['average_duration_ms']} ms\\n"
                            f"Configured bots: {BotConfig.query.count()}\\n"
                            f"Running bots: {BotConfig.query.filter_by(active=True).count()}\\n\\n"
                            "No user IDs, chat IDs, or message contents are stored in analytics.")
                    elif ADMIN_TELEGRAM_ID is None:
                        send_message(token, chat_id, "Telegram admin reports are not configured. Set ADMIN_TELEGRAM_ID on the server.")
                        event_success = False
                    else:
                        send_message(token, chat_id, "This command is restricted to the configured bot administrator.")
                        event_success = False
                if event_type:
                    elapsed = int((time.monotonic() - started) * 1000)
                    record_usage(bot_id, event_type, event_success, elapsed)
                    if event_success:
                        bot.success_count += 1
                    else:
                        bot.error_count += 1
                db.session.commit()
            except Exception:
                log.exception("Update handling failed for bot %s", bot_id)
                db.session.rollback()
                bot = db.session.get(BotConfig, bot_id)
                if bot:
                    bot.error_count += 1
                    record_usage(bot_id, event_type or "processing_error", False,
                                 int((time.monotonic() - started) * 1000))
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
        "total_errors": errors,
        "server_status": "online"
    })

@app.get("/api/analytics")
@login_required
def analytics():
    try:
        days = int(request.args.get("days", "7"))
    except ValueError:
        return jsonify({"error": "days must be an integer from 1 to 90"}), 400
    if days < 1 or days > 90:
        return jsonify({"error": "days must be between 1 and 90"}), 400
    return jsonify(get_analytics(days))


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
    # Keep aggregate analytics for at most 90 days; no user identifiers or message bodies are stored.
    analytics_cutoff = datetime.now(timezone.utc) - timedelta(days=90)
    UsageEvent.query.filter(UsageEvent.created_at < analytics_cutoff).delete(synchronize_session=False)
    db.session.commit()
    # Restore bots marked active after a process restart.
    active_ids = [b.id for b in BotConfig.query.filter_by(active=True).all()]
    for active_id in active_ids:
        try:
            runner.start(active_id)
        except Exception:
            log.exception("Could not restore bot %s", active_id)


# ---------------- API MANAGER EXTENSION ----------------
# Integrated directly to avoid circular-import problems from a separate module.
import ipaddress
import json
import socket
from urllib.parse import urlparse
from flask import Response

class ApiConfig(db.Model):
    __tablename__ = "api_config"
    id = db.Column(db.Integer, primary_key=True)
    bot_id = db.Column(db.Integer, db.ForeignKey("bot_config.id", ondelete="CASCADE"), nullable=False, index=True)
    name = db.Column(db.String(80), nullable=False)
    url = db.Column(db.String(1000), nullable=False)
    method = db.Column(db.String(8), nullable=False, default="GET")
    query_param = db.Column(db.String(80), nullable=False, default="q")
    key_header = db.Column(db.String(80), nullable=False, default="X-API-Key")
    api_key_enc = db.Column(db.Text)
    enabled = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))

    def get_key(self):
        return cipher.decrypt(self.api_key_enc.encode()).decode() if self.api_key_enc else ""

def validate_api_url(url):
    parsed = urlparse(str(url or "").strip())
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Use a public HTTPS API URL without embedded credentials.")
    if parsed.port not in (None, 443):
        raise ValueError("Only standard HTTPS port 443 is allowed.")
    host = parsed.hostname.lower().rstrip(".")
    if host == "localhost" or host.endswith((".localhost", ".local")):
        raise ValueError("Local API hosts are blocked.")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
    except socket.gaierror:
        raise ValueError("API hostname could not be resolved.")
    if not addresses or any(not ipaddress.ip_address(addr).is_global for addr in addresses):
        raise ValueError("API host must resolve only to public IP addresses.")
    return url.strip()

def call_configured_api(row, query):
    validate_api_url(row.url)
    headers = {"Accept": "application/json"}
    if row.api_key_enc:
        headers[row.key_header] = row.get_key()
    payload = {row.query_param: query}
    client = requests.Session()
    client.trust_env = False
    kwargs = dict(headers=headers, timeout=(4, 12), allow_redirects=False)
    if row.method == "GET":
        response = client.get(row.url, params=payload, **kwargs)
    else:
        response = client.post(row.url, json=payload, **kwargs)
    if 300 <= response.status_code < 400:
        raise ValueError("API redirect blocked.")
    response.raise_for_status()
    try:
        return response.json(), response.status_code
    except ValueError as exc:
        raise ValueError("API did not return valid JSON.") from exc

def serialize_api(row):
    return {
        "id": row.id, "bot_id": row.bot_id, "name": row.name, "url": row.url,
        "method": row.method, "query_param": row.query_param,
        "key_header": row.key_header, "has_api_key": bool(row.api_key_enc),
        "enabled": row.enabled
    }

API_MANAGER_PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>API Manager</title>
<style>body{background:#070913;color:#e2e8f0;font:14px system-ui;padding:18px}main{max-width:800px;margin:auto}.card{background:#111827;border:1px solid #334155;border-radius:14px;padding:16px;margin:12px 0}input,select,button{box-sizing:border-box;width:100%;padding:12px;margin:5px 0;border-radius:9px;border:1px solid #475569;background:#080f20;color:white}button{background:#075985;font-weight:bold}.danger{background:#7f1d1d}.grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}.muted{color:#94a3b8;font-size:12px}.item{border-top:1px solid #334155;padding:12px 0;overflow-wrap:anywhere}@media(max-width:550px){.grid{grid-template-columns:1fr}}</style></head>
<body><main><h2>🔌 API Manager</h2><p class="muted">Configure only public APIs or APIs you are authorized to use. API keys are encrypted at rest.</p>
<div class="card"><select id="bot"><option value="">Select bot</option></select><form id="form"><div class="grid"><input id="name" placeholder="API Name" required maxlength="80"><select id="method"><option>GET</option><option>POST</option></select></div>
<input id="url" type="url" placeholder="https://public-api.example/search" required><div class="grid"><input id="param" value="q" placeholder="Query parameter"><input id="header" value="X-API-Key" placeholder="API key header name"></div>
<input id="key" type="password" placeholder="API Key (optional)" autocomplete="off"><button>Save API</button></form>
<p class="muted">GET sends a query parameter. POST sends JSON. Test uses a harmless integration-test query.</p><div id="msg" class="muted"></div></div>
<div class="card"><h3>Configured APIs</h3><div id="list">Select a bot.</div></div><p><a href="/" style="color:#67e8f9">← Back to dashboard</a></p></main>
<script>
const $=x=>document.getElementById(x);
async function call(u,o={}){let r=await fetch(u,{credentials:'same-origin',headers:{'Content-Type':'application/json'},...o}),d={};try{d=await r.json()}catch{}if(!r.ok)throw Error(d.error||'Request failed');return d}
async function bots(){let d=await call('/api/dashboard');$('bot').innerHTML='<option value="">Select bot</option>'+d.bots.map(b=>'<option value="'+b.id+'">'+String(b.name).replace(/[&<>"]/g,'')+'</option>').join('')}
async function load(){let id=$('bot').value;if(!id){$('list').textContent='Select a bot.';return}try{let d=await call('/api/bots/'+id+'/apis');$('list').innerHTML=d.apis.length?d.apis.map(a=>'<div class="item"><b>'+String(a.name).replace(/[&<>"]/g,'')+'</b><p class="muted">'+a.method+' • '+String(a.url).replace(/[&<>"]/g,'')+'<br>Key: '+(a.has_api_key?'saved securely':'not set')+' • '+(a.enabled?'Enabled':'Disabled')+'</p><div class="grid"><button onclick="testApi('+a.id+')">Test</button><button onclick="toggleApi('+a.id+','+(!a.enabled)+')">'+(a.enabled?'Disable':'Enable')+'</button></div><button class="danger" onclick="removeApi('+a.id+')">Delete</button></div>').join(''):'No APIs configured.'}catch(e){$('list').textContent=e.message}}
$('bot').onchange=load;
$('form').onsubmit=async e=>{e.preventDefault();if(!$('bot').value){$('msg').textContent='Select a bot first.';return}try{await call('/api/bots/'+$('bot').value+'/apis',{method:'POST',body:JSON.stringify({name:$('name').value,url:$('url').value,method:$('method').value,query_param:$('param').value||'q',key_header:$('header').value||'X-API-Key',api_key:$('key').value})});$('msg').textContent='API saved.';$('form').reset();$('param').value='q';$('header').value='X-API-Key';await load()}catch(x){$('msg').textContent=x.message}};
async function testApi(id){try{let d=await call('/api/bots/'+$('bot').value+'/apis/'+id+'/test',{method:'POST',body:'{}'});$('msg').textContent='HTTP '+d.status_code+' • '+d.preview}catch(e){$('msg').textContent=e.message}}
async function toggleApi(id,enabled){try{await call('/api/bots/'+$('bot').value+'/apis/'+id,{method:'PATCH',body:JSON.stringify({enabled})});await load()}catch(e){$('msg').textContent=e.message}}
async function removeApi(id){if(!confirm('Delete this API?'))return;try{await call('/api/bots/'+$('bot').value+'/apis/'+id,{method:'DELETE'});await load()}catch(e){$('msg').textContent=e.message}}
bots().catch(e=>$('msg').textContent=e.message);
</script></body></html>"""

@app.get("/api-manager")
@login_required
def api_manager_page():
    return Response(API_MANAGER_PAGE, mimetype="text/html")

@app.get("/api/bots/<int:bot_id>/apis")
@login_required
def api_list(bot_id):
    if not db.session.get(BotConfig, bot_id):
        return jsonify(error="Bot not found"), 404
    rows = ApiConfig.query.filter_by(bot_id=bot_id).order_by(ApiConfig.id.desc()).all()
    return jsonify(apis=[serialize_api(row) for row in rows])

@app.post("/api/bots/<int:bot_id>/apis")
@login_required
def api_add(bot_id):
    if not db.session.get(BotConfig, bot_id):
        return jsonify(error="Bot not found"), 404
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()
    url = str(data.get("url", "")).strip()
    method = str(data.get("method", "GET")).upper()
    param = str(data.get("query_param", "q")).strip()
    header = str(data.get("key_header", "X-API-Key")).strip()
    key = str(data.get("api_key", "")).strip()
    if not name or len(name) > 80:
        return jsonify(error="API name required (max 80 characters)."), 400
    if method not in ("GET", "POST"):
        return jsonify(error="Only GET and POST are supported."), 400
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", param):
        return jsonify(error="Invalid query parameter name."), 400
    if not re.fullmatch(r"[A-Za-z0-9-]{1,80}", header):
        return jsonify(error="Invalid API key header name."), 400
    try:
        validate_api_url(url)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    row = ApiConfig(
        bot_id=bot_id, name=name, url=url, method=method,
        query_param=param, key_header=header,
        api_key_enc=cipher.encrypt(key.encode()).decode() if key else None,
        enabled=bool(data.get("enabled", True))
    )
    db.session.add(row)
    db.session.commit()
    return jsonify(ok=True, api=serialize_api(row)), 201

@app.patch("/api/bots/<int:bot_id>/apis/<int:api_id>")
@login_required
def api_toggle(bot_id, api_id):
    row = ApiConfig.query.filter_by(id=api_id, bot_id=bot_id).first()
    if not row:
        return jsonify(error="API not found"), 404
    data = request.get_json(silent=True) or {}
    if "enabled" in data:
        if not isinstance(data["enabled"], bool):
            return jsonify(error="enabled must be true or false."), 400
        row.enabled = data["enabled"]
    db.session.commit()
    return jsonify(ok=True, api=serialize_api(row))

@app.delete("/api/bots/<int:bot_id>/apis/<int:api_id>")
@login_required
def api_remove(bot_id, api_id):
    row = ApiConfig.query.filter_by(id=api_id, bot_id=bot_id).first()
    if not row:
        return jsonify(error="API not found"), 404
    db.session.delete(row)
    db.session.commit()
    return jsonify(ok=True)

@app.post("/api/bots/<int:bot_id>/apis/<int:api_id>/test")
@login_required
def api_test(bot_id, api_id):
    row = ApiConfig.query.filter_by(id=api_id, bot_id=bot_id).first()
    if not row:
        return jsonify(error="API not found"), 404
    try:
        data, status = call_configured_api(row, "integration-test")
        preview = json.dumps(data, ensure_ascii=False, default=str)[:1200]
        return jsonify(ok=True, status_code=status, preview=preview)
    except requests.RequestException:
        return jsonify(ok=False, error="API request failed or timed out. Check its required parameters."), 400
    except ValueError as exc:
        return jsonify(ok=False, error=str(exc)), 400

# Keep the Telegram search command limited to non-sensitive, short public queries.
_original_update_handler = BotRunner._handle_update
def _handler_with_public_api_search(self, bot_id, token, update):
    message = update.get("message") or update.get("edited_message")
    message_text = (message or {}).get("text", "").strip()
    command = message_text.split()[0].split("@")[0].lower() if message_text else ""
    if command == "/number":
        # Phone numbers and personal IDs can identify private individuals.
        # Do not forward them to third-party APIs or expose returned records.
        chat_id = message["chat"]["id"]
        send_message(
            token,
            chat_id,
            "The /number lookup command is disabled for private phone numbers "
            "and personal IDs. Use /search for supported public, non-sensitive topics."
        )
        with app.app_context():
            bot = db.session.get(BotConfig, bot_id)
            if bot:
                bot.lookup_count += 1
                record_usage(
                    bot_id,
                    "number_lookup_blocked",
                    False,
                    0
                )
                bot.error_count += 1
                db.session.commit()
        return

    if command != "/search":
        return _original_update_handler(self, bot_id, token, update)

    chat_id = message["chat"]["id"]
    query = message_text[len(message_text.split()[0]):].strip()
    with app.app_context():
        bot = db.session.get(BotConfig, bot_id)
        if not bot:
            return
        if not query:
            send_message(token, chat_id, "Usage: /search a public topic or organization")
            return
        if (len(query) > 120
            or re.search(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", query)
            or re.search(r"(?<!\d)\d{7,}(?!\d)", query)):
            send_message(token, chat_id, "This search format is not supported. Do not submit private personal identifiers.")
            return
        rows = ApiConfig.query.filter_by(bot_id=bot_id, enabled=True).order_by(ApiConfig.id.asc()).limit(3).all()
        if not rows:
            send_message(token, chat_id, "No enabled API is configured. Ask the bot administrator.")
            return
        outputs = []
        any_success = False
        for row in rows:
            try:
                result, status = call_configured_api(row, query)
                any_success = True
                outputs.append(row.name + " (HTTP " + str(status) + ")\n" +
                               json.dumps(result, ensure_ascii=False, indent=2, default=str)[:1800])
            except Exception:
                log.exception("Configured API search failed (API id %s)", row.id)
                outputs.append(row.name + ": API request failed.")
        send_message(token, chat_id, ("Public API results:\n\n" + "\n\n".join(outputs))[:3900])
        bot.lookup_count += 1
        record_usage(bot_id, "api_search", any_success, 0)
        if any_success:
            bot.success_count += 1
        else:
            bot.error_count += 1
        db.session.commit()

BotRunner._handle_update = _handler_with_public_api_search

with app.app_context():
    db.create_all()
# ---------------- END API MANAGER EXTENSION ----------------


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
