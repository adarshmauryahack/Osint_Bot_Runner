# API Manager extension for OSINT Bot Runner.
# Add `import api_manager` in main.py just before its `if __name__ == "__main__":` block.
import ipaddress, json, re, socket
from urllib.parse import urlparse
from datetime import datetime, timezone
import requests
from flask import jsonify, request, Response
from main import app, db, cipher, BotConfig, BotRunner, record_usage, send_message, login_required

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

def validate_url(url):
    p = urlparse(str(url or "").strip())
    if p.scheme != "https" or not p.hostname or p.username or p.password:
        raise ValueError("Use a public HTTPS API URL without embedded credentials.")
    host = p.hostname.lower().rstrip(".")
    if host == "localhost" or host.endswith((".localhost", ".local")):
        raise ValueError("Local API hosts are blocked.")
    try:
        ips = {x[4][0] for x in socket.getaddrinfo(host, p.port or 443, type=socket.SOCK_STREAM)}
    except socket.gaierror:
        raise ValueError("API hostname could not be resolved.")
    if not ips or any(not ipaddress.ip_address(x).is_global for x in ips):
        raise ValueError("API host must resolve only to public IP addresses.")
    return url.strip()

def request_api(row, query):
    validate_url(row.url)
    headers = {"Accept": "application/json"}
    if row.api_key_enc:
        headers[row.key_header] = row.get_key()
    payload = {row.query_param: query}
    s = requests.Session()
    s.trust_env = False
    if row.method == "GET":
        r = s.get(row.url, params=payload, headers=headers, timeout=(4, 12), allow_redirects=False)
    else:
        r = s.post(row.url, json=payload, headers=headers, timeout=(4, 12), allow_redirects=False)
    if 300 <= r.status_code < 400:
        raise ValueError("Redirect blocked.")
    r.raise_for_status()
    try:
        return r.json(), r.status_code
    except ValueError:
        raise ValueError("API did not return valid JSON.")

def serialize(row):
    return {"id":row.id,"bot_id":row.bot_id,"name":row.name,"url":row.url,
            "method":row.method,"query_param":row.query_param,"key_header":row.key_header,
            "has_api_key":bool(row.api_key_enc),"enabled":row.enabled}

@app.get("/api-manager")
@login_required
def api_manager_page():
    return Response(PAGE, mimetype="text/html")

@app.get("/api/bots/<int:bot_id>/apis")
@login_required
def api_list(bot_id):
    if not db.session.get(BotConfig, bot_id): return jsonify(error="Bot not found"),404
    rows=ApiConfig.query.filter_by(bot_id=bot_id).order_by(ApiConfig.id.desc()).all()
    return jsonify(apis=[serialize(x) for x in rows])

@app.post("/api/bots/<int:bot_id>/apis")
@login_required
def api_add(bot_id):
    if not db.session.get(BotConfig, bot_id): return jsonify(error="Bot not found"),404
    d=request.get_json(silent=True) or {}
    name=str(d.get("name","")).strip(); url=str(d.get("url","")).strip()
    method=str(d.get("method","GET")).upper()
    param=str(d.get("query_param","q")).strip()
    header=str(d.get("key_header","X-API-Key")).strip()
    key=str(d.get("api_key","")).strip()
    if not name or len(name)>80: return jsonify(error="API name required (max 80 characters)."),400
    if method not in ("GET","POST"): return jsonify(error="Only GET and POST supported."),400
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}",param): return jsonify(error="Invalid query parameter name."),400
    if not re.fullmatch(r"[A-Za-z0-9-]{1,80}",header): return jsonify(error="Invalid API key header name."),400
    try: validate_url(url)
    except ValueError as e: return jsonify(error=str(e)),400
    row=ApiConfig(bot_id=bot_id,name=name,url=url,method=method,query_param=param,key_header=header,
        api_key_enc=cipher.encrypt(key.encode()).decode() if key else None,enabled=bool(d.get("enabled",True)))
    db.session.add(row); db.session.commit()
    return jsonify(ok=True,api=serialize(row)),201

@app.patch("/api/bots/<int:bot_id>/apis/<int:api_id>")
@login_required
def api_toggle(bot_id,api_id):
    row=ApiConfig.query.filter_by(id=api_id,bot_id=bot_id).first()
    if not row: return jsonify(error="API not found"),404
    d=request.get_json(silent=True) or {}
    if "enabled" in d: row.enabled=bool(d["enabled"])
    db.session.commit()
    return jsonify(ok=True,api=serialize(row))

@app.delete("/api/bots/<int:bot_id>/apis/<int:api_id>")
@login_required
def api_remove(bot_id,api_id):
    row=ApiConfig.query.filter_by(id=api_id,bot_id=bot_id).first()
    if not row: return jsonify(error="API not found"),404
    db.session.delete(row); db.session.commit()
    return jsonify(ok=True)

@app.post("/api/bots/<int:bot_id>/apis/<int:api_id>/test")
@login_required
def api_test(bot_id,api_id):
    row=ApiConfig.query.filter_by(id=api_id,bot_id=bot_id).first()
    if not row: return jsonify(error="API not found"),404
    try:
        data,status=request_api(row,"integration-test")
        return jsonify(ok=True,status_code=status,preview=json.dumps(data,ensure_ascii=False,default=str)[:1200])
    except requests.RequestException:
        return jsonify(ok=False,error="API request failed or timed out. Check its required parameters."),400
    except ValueError as e:
        return jsonify(ok=False,error=str(e)),400

# /search sends text to configured, enabled public/authorized APIs only.
# Email-like and long numeric identifiers are blocked to reduce private-person lookup misuse.
_original_handler=BotRunner._handle_update
def _handler_with_search(self,bot_id,token,update):
    msg=update.get("message") or update.get("edited_message")
    text=(msg or {}).get("text","").strip()
    command=text.split()[0].split("@")[0].lower() if text else ""
    if command!="/search":
        return _original_handler(self,bot_id,token,update)
    chat_id=msg["chat"]["id"]; query=text[len(text.split()[0]):].strip()
    with app.app_context():
        bot=db.session.get(BotConfig,bot_id)
        if not bot: return
        if not query:
            send_message(token,chat_id,"Usage: /search your public-information query"); return
        if len(query)>120 or re.search(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b",query) or re.search(r"(?<!\d)\d{7,}(?!\d)",query):
            send_message(token,chat_id,"This search format is not supported. Do not search private personal identifiers."); return
        rows=ApiConfig.query.filter_by(bot_id=bot_id,enabled=True).order_by(ApiConfig.id.asc()).limit(3).all()
        if not rows:
            send_message(token,chat_id,"No enabled search API is configured. Ask the bot administrator."); return
        results=[]
        for row in rows:
            try:
                data,status=request_api(row,query)
                results.append(row.name+" (HTTP "+str(status)+")\n"+json.dumps(data,ensure_ascii=False,indent=2,default=str)[:2200])
            except Exception:
                results.append(row.name+": API request failed.")
        send_message(token,chat_id,("Public/authorized API results:\n\n"+"\n\n".join(results))[:3900])
        bot.lookup_count+=1
        record_usage(bot_id,"api_search",True,0)
        db.session.commit()
BotRunner._handle_update=_handler_with_search

with app.app_context():
    db.create_all()

PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>API Manager</title>
<style>body{background:#070913;color:#e2e8f0;font:14px system-ui;padding:18px}main{max-width:800px;margin:auto}.card{background:#111827;border:1px solid #334155;border-radius:14px;padding:16px;margin:12px 0}input,select,button{box-sizing:border-box;width:100%;padding:12px;margin:5px 0;border-radius:9px;border:1px solid #475569;background:#080f20;color:white}button{background:#075985;font-weight:bold}.danger{background:#7f1d1d}.grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}.muted{color:#94a3b8;font-size:12px}.item{border-top:1px solid #334155;padding:12px 0;overflow-wrap:anywhere}@media(max-width:550px){.grid{grid-template-columns:1fr}}</style></head>
<body><main><h2>🔌 API Manager</h2><p class="muted">Use only public APIs or APIs you are authorized to use. API Key is optional and encrypted.</p>
<div class="card"><select id="bot"><option value="">Select bot</option></select><form id="form"><div class="grid"><input id="name" placeholder="API Name" required maxlength="80"><select id="method"><option>GET</option><option>POST</option></select></div>
<input id="url" type="url" placeholder="https://public-api.example/search" required><div class="grid"><input id="param" value="q" placeholder="Query parameter"><input id="header" value="X-API-Key" placeholder="API key header name"></div>
<input id="key" type="password" placeholder="API Key (optional)" autocomplete="off"><button>Save API</button></form>
<p class="muted">GET sends ?q=search. POST sends JSON {"q":"search"}. Test uses integration-test.</p><div id="msg" class="muted"></div></div>
<div class="card"><h3>Configured APIs</h3><div id="list">Select a bot.</div></div><p><a href="/" style="color:#67e8f9">← Back to dashboard</a></p></main>
<script>const $=x=>document.getElementById(x);async function call(u,o={}){let r=await fetch(u,{credentials:'same-origin',headers:{'Content-Type':'application/json'},...o}),d={};try{d=await r.json()}catch{}if(!r.ok)throw Error(d.error||'Request failed');return d}
async function bots(){let d=await call('/api/dashboard');$('bot').innerHTML='<option value="">Select bot</option>'+d.bots.map(b=>'<option value="'+b.id+'">'+b.name.replace(/[&<>"]/g,'')+'</option>').join('')}
async function load(){let id=$('bot').value;if(!id){$('list').textContent='Select a bot.';return}try{let d=await call('/api/bots/'+id+'/apis');$('list').innerHTML=d.apis.length?d.apis.map(a=>'<div class="item"><b>'+a.name.replace(/[&<>"]/g,'')+'</b><p class="muted">'+a.method+' • '+a.url.replace(/[&<>"]/g,'')+'<br>Key: '+(a.has_api_key?'saved securely':'not set')+' • '+(a.enabled?'Enabled':'Disabled')+'</p><div class="grid"><button onclick="testApi('+a.id+')">Test</button><button onclick="toggleApi('+a.id+','+(!a.enabled)+')">'+(a.enabled?'Disable':'Enable')+'</button></div><button class="danger" onclick="removeApi('+a.id+')">Delete</button></div>').join(''):'No APIs configured.'}catch(e){$('list').textContent=e.message}}
$('bot').onchange=load;$('form').onsubmit=async e=>{e.preventDefault();try{await call('/api/bots/'+$('bot').value+'/apis',{method:'POST',body:JSON.stringify({name:$('name').value,url:$('url').value,method:$('method').value,query_param:$('param').value||'q',key_header:$('header').value||'X-API-Key',api_key:$('key').value})});$('msg').textContent='API saved.';$('form').reset();$('param').value='q';$('header').value='X-API-Key';await load()}catch(x){$('msg').textContent=x.message}}
async function testApi(id){try{let d=await call('/api/bots/'+$('bot').value+'/apis/'+id+'/test',{method:'POST',body:'{}'});$('msg').textContent='HTTP '+d.status_code+' • '+d.preview}catch(e){$('msg').textContent=e.message}}
async function toggleApi(id,enabled){try{await call('/api/bots/'+$('bot').value+'/apis/'+id,{method:'PATCH',body:JSON.stringify({enabled})});await load()}catch(e){$('msg').textContent=e.message}}
async function removeApi(id){if(!confirm('Delete this API?'))return;try{await call('/api/bots/'+$('bot').value+'/apis/'+id,{method:'DELETE'});await load()}catch(e){$('msg').textContent=e.message}}
bots().catch(e=>$('msg').textContent=e.message);</script></body></html>"""
