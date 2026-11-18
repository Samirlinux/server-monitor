"""
checks.py - فحوصات إضافية لكل سيرفر (Ping / Port / HTTP / Keyword) + وضع سيرفرات ويندوز
Developed by Eng. Samir Abo El-Khair | Version 4.0

- الفحوصات بتتخزن جوه جدول servers (عمود extra_checks) وبتظهر في اللوحة كخدمات عادية،
  فالتنبيهات والاب تايم بيشتغلوا عليها زي باقي الخدمات.
- صفحة الإدارة:  /checks
"""
import asyncio
import json
import re
import ssl
import time
import urllib.error
import urllib.request
import uuid

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse

CONFIRM_FAILS = 2          # عدد مرات الفشل المتتالية قبل ما الفحص يتعلّم DOWN (يمنع الإنذارات الكاذبة)
WEB_MIN_INTERVAL = 60      # فحوصات الموقع (HTTP/Keyword) بتتكرر كل 60 ثانية بحد أدنى
MAX_BODY = 1_500_000
TYPES = ("ping", "port", "http", "keyword")
DEFAULT_NAMES = {"ping": "Ping", "port": "Port", "http": "Website (HTTP)", "keyword": "Keyword"}

_state = {}
_schema_done = False
_get_db = None


# ───────────────────────── قاعدة البيانات ─────────────────────────
def ensure_schema(get_db):
    global _schema_done
    if _schema_done:
        return
    conn = get_db()
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(servers)").fetchall()]
        if not cols:
            return  # الجدول لسه ما اتعملش، هنجرب تاني بعدين
        if "extra_checks" not in cols:
            conn.execute("ALTER TABLE servers ADD COLUMN extra_checks TEXT DEFAULT '[]'")
        if "windows_mode" not in cols:
            conn.execute("ALTER TABLE servers ADD COLUMN windows_mode INTEGER DEFAULT 0")
        conn.commit()
        _schema_done = True
    finally:
        conn.close()


def parse_checks(server):
    raw = server.get("extra_checks")
    if not raw:
        return []
    try:
        lst = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return []
    return [c for c in (lst or []) if isinstance(c, dict) and c.get("type") in TYPES and c.get("id")]


def clean_check(b):
    """بيتحقق من بيانات الفحص ويرجّع نسخة نضيفة، أو بيرفع ValueError(code)."""
    t = str(b.get("type", "")).strip().lower()
    if t not in TYPES:
        raise ValueError("bad_type")
    chk = {"type": t, "name": str(b.get("name", "")).strip()[:60] or DEFAULT_NAMES[t]}
    try:
        timeout = float(b.get("timeout") or 8)
    except (TypeError, ValueError):
        raise ValueError("bad_timeout")
    chk["timeout"] = max(1.0, min(20.0, timeout))
    if t == "port":
        try:
            port = int(b.get("port"))
        except (TypeError, ValueError):
            raise ValueError("bad_port")
        if not 1 <= port <= 65535:
            raise ValueError("bad_port")
        chk["port"] = port
    if t in ("http", "keyword"):
        url = str(b.get("url", "")).strip()
        if not re.match(r"^https?://[^\s/]+", url, re.I) or len(url) > 500:
            raise ValueError("bad_url")
        chk["url"] = url
        chk["verify_ssl"] = bool(b.get("verify_ssl", True))
        expect = str(b.get("expect", "")).strip()
        if expect and not re.fullmatch(r"[0-9xX,\- ]{3,40}", expect):
            raise ValueError("bad_expect")
        chk["expect"] = expect or "200-399"
    if t == "keyword":
        kw = str(b.get("keyword", "")).strip()
        if not kw or len(kw) > 200:
            raise ValueError("bad_keyword")
        chk["keyword"] = kw
        mode = str(b.get("mode", "exists"))
        chk["mode"] = mode if mode in ("exists", "absent") else "exists"
        chk["case_sensitive"] = bool(b.get("case_sensitive", False))
    return chk


# ───────────────────────── تنفيذ الفحوصات ─────────────────────────
def status_ok(code, expect):
    expect = (expect or "200-399").replace(" ", "")
    for part in expect.split(","):
        if not part:
            continue
        m = re.fullmatch(r"([1-5])xx", part, re.I)
        if m:
            if code // 100 == int(m.group(1)):
                return True
            continue
        m = re.fullmatch(r"([0-9]{3})-([0-9]{3})", part)
        if m:
            if int(m.group(1)) <= code <= int(m.group(2)):
                return True
            continue
        if part.isdigit() and code == int(part):
            return True
    return False


async def _ping(ip, timeout):
    if not ip or ip.startswith("-"):
        return False, 0.0, "Invalid address"
    try:
        proc = await asyncio.create_subprocess_exec(
            "ping", "-c", "1", "-W", str(max(1, int(timeout))), ip,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return False, 0.0, "ping command not installed (apt install iputils-ping)"
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout + 2)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return False, 0.0, "Ping timeout"
    text = out.decode("utf-8", "ignore")
    m = re.search(r"time[=<]\s*([0-9.]+)\s*ms", text)
    if proc.returncode == 0:
        if m:
            ms = float(m.group(1))
            return True, ms, f"Reply in {ms} ms"
        return True, 0.0, "Reply received"
    return False, 0.0, "No ping reply"


async def _tcp(ip, port, timeout):
    t0 = time.perf_counter()
    try:
        _, w = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout=timeout)
        ms = round((time.perf_counter() - t0) * 1000, 1)
        w.close()
        try:
            await w.wait_closed()
        except Exception:
            pass
        return True, ms, f"Port {port} open ({ms} ms)"
    except asyncio.TimeoutError:
        return False, 0.0, f"Port {port} timeout"
    except Exception as e:
        return False, 0.0, f"Port {port} closed/unreachable ({type(e).__name__})"


def _http_sync(url, timeout, verify, want_body):
    ctx = None
    if url.lower().startswith("https://") and not verify:
        ctx = ssl._create_unverified_context()
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (ServerMonitor/4.0)", "Accept": "*/*"})
    t0 = time.perf_counter()
    try:
        resp = urllib.request.urlopen(req, timeout=timeout, context=ctx)
    except urllib.error.HTTPError as e:
        resp = e
    code = getattr(resp, "status", None) or getattr(resp, "code", 0)
    body = ""
    if want_body:
        data = resp.read(MAX_BODY)
        charset = resp.headers.get_content_charset() or "utf-8"
        try:
            body = data.decode(charset, errors="ignore")
        except LookupError:
            body = data.decode("utf-8", errors="ignore")
    ms = round((time.perf_counter() - t0) * 1000, 1)
    try:
        resp.close()
    except Exception:
        pass
    return code, body, ms


def _short_err(e):
    if isinstance(e, asyncio.TimeoutError):
        return "Timeout"
    reason = getattr(e, "reason", None) or e
    return (str(reason) or type(e).__name__)[:120]


async def _run_once(server, chk):
    t = chk["type"]
    ip = str(server.get("ip", "")).strip()
    timeout = float(chk.get("timeout") or 8)
    if t == "ping":
        return await _ping(ip, min(timeout, 5))
    if t == "port":
        return await _tcp(ip, int(chk["port"]), min(timeout, 10))
    try:
        code, body, ms = await asyncio.wait_for(
            asyncio.to_thread(_http_sync, chk["url"], timeout, bool(chk.get("verify_ssl", True)), t == "keyword"),
            timeout=timeout + 3,
        )
    except Exception as e:
        return False, 0.0, _short_err(e)
    expect = chk.get("expect") or "200-399"
    if t == "http":
        ok = status_ok(code, expect)
        return ok, ms, f"HTTP {code} ({ms} ms)" + ("" if ok else f" - expected {expect}")
    if not status_ok(code, expect):
        return False, ms, f"HTTP {code} - expected {expect}"
    kw = chk["keyword"]
    if chk.get("case_sensitive"):
        found = kw in body
    else:
        found = kw.lower() in body.lower()
    ok = (found == (chk.get("mode", "exists") == "exists"))
    return ok, ms, f"Keyword '{kw}' {'found' if found else 'not found'} ({ms} ms)"


async def evaluate(server, chk):
    """بيشغّل الفحص مع الكاش (للمواقع) وتأكيد الفشل مرتين قبل ما يتعلّم DOWN."""
    key = (server.get("id"), chk["id"])
    st = _state.setdefault(key, {"fails": 0, "reported": None, "ts": 0.0, "last_ok": False, "result": None})
    now = time.time()
    if chk["type"] in ("http", "keyword") and st["last_ok"] and st["result"] and now - st["ts"] < WEB_MIN_INTERVAL:
        return st["result"]
    ok, ms, detail = await _run_once(server, chk)
    if ok:
        st["fails"] = 0
        reported = "UP"
        st["ts"] = now
    else:
        st["fails"] += 1
        if st["reported"] == "UP" and st["fails"] < CONFIRM_FAILS:
            reported = "UP"
            detail += f" (retry {st['fails']}/{CONFIRM_FAILS})"
        else:
            reported = "DOWN"
    st["last_ok"] = ok
    st["reported"] = reported
    st["result"] = {"status": reported, "latency_ms": ms, "detail": detail}
    return st["result"]


def service_entry(chk, res):
    label = {"ping": "Ping", "port": "Port", "http": "HTTP", "keyword": "Keyword"}[chk["type"]]
    return {
        "key": "chk_" + str(chk["id"]),
        "name": chk.get("name") or DEFAULT_NAMES[chk["type"]],
        "category": "web" if chk["type"] in ("http", "keyword") else "system",
        "port": int(chk.get("port") or 0),
        "status": res["status"],
        "latency_ms": res["latency_ms"],
        "detail": res["detail"],
        "source": "Check: " + label,
    }


async def augment(server, inspection):
    """بيتنادى من app.py بعد inspect_server: بيضيف الفحوصات الإضافية ويظبط حالة السيرفر."""
    try:
        return await _augment(server, inspection)
    except Exception as e:  # ما نوقفش حلقة المراقبة لو فحص فشل
        print(f"[checks] augment error: {e}")
        return inspection


async def _augment(server, inspection):
    if _get_db:
        ensure_schema(_get_db)
    windows = bool(int(server.get("windows_mode") or 0))
    checks = parse_checks(server)
    if windows and not checks:  # ويندوز من غير فحوصات: نستخدم Ping تلقائياً عشان يبقى فيه إشارة
        checks = [{"id": "autoping", "type": "ping", "name": "Ping", "timeout": 4}]
    if not checks and not windows:
        return inspection

    services = list(inspection.get("services") or [])
    if windows:  # سيرفر ويندوز: شيل فحوصات بورتات لينكس الافتراضية (22/3306/25...)
        services = [s for s in services if s.get("source") != "TCP Port"]

    results = await asyncio.gather(*(evaluate(server, c) for c in checks), return_exceptions=True)
    any_up = False
    ping_ms = None
    for c, r in zip(checks, results):
        if isinstance(r, Exception):
            r = {"status": "DOWN", "latency_ms": 0.0, "detail": f"check error: {r}"}
        services.append(service_entry(c, r))
        if r["status"] == "UP":
            any_up = True
            if c["type"] == "ping" and ping_ms is None and r["latency_ms"]:
                ping_ms = r["latency_ms"]
    inspection["services"] = services

    if windows:
        inspection["status"] = "UP" if (any_up or inspection.get("ssh_connected")) else "DOWN"
        inspection["uptime_str"] = "Online (monitor checks)" if inspection["status"] == "UP" else "Offline (DOWN)"
        if ping_ms:
            inspection["ping_ms"] = ping_ms
        ms = inspection.get("mail_stats") or {}
        ms["has_spam_issue"] = False
        ms["spam_warnings"] = []
        inspection["mail_stats"] = ms
    elif inspection.get("status") == "DOWN" and any_up:
        inspection["status"] = "UP"
        inspection["uptime_str"] = "Online (monitor checks)"
    return inspection


# ───────────────────────── صفحة الإدارة + الـ API ─────────────────────────
PAGE_HTML = r"""<!DOCTYPE html>
<html lang="ar" dir="rtl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Extra checks | الفحوصات الإضافية</title>
<style>
:root{--bg:#f5f7fa;--card:#fff;--text:#1f2933;--muted:#6b7785;--line:#e3e8ee;--green:#16a34a;--red:#dc2626;--blue:#2563eb;--gray:#94a3b8}
@media (prefers-color-scheme: dark){:root{--bg:#0f141a;--card:#171e26;--text:#e6edf3;--muted:#8b98a5;--line:#26303b}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:system-ui,Tahoma,Arial,sans-serif}
.wrap{max-width:980px;margin:0 auto;padding:22px 16px}a{color:var(--blue);text-decoration:none}
h1{margin:0 0 6px;font-size:22px}.sub{color:var(--muted);font-size:14px;margin-bottom:18px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:14px}
.head{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap}
.name{font-weight:700;font-size:17px}.ip{color:var(--muted);font-size:13px}
label{font-size:13px;color:var(--muted);display:block;margin:8px 0 3px}
input,select{width:100%;padding:8px 10px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--text);font:inherit}
input[type=checkbox]{width:auto}
.row{display:flex;align-items:center;gap:10px;padding:9px 0;border-bottom:1px solid var(--line);flex-wrap:wrap}
.row:last-child{border-bottom:0}.row .t{flex:1;min-width:200px}.row small{color:var(--muted);display:block;word-break:break-all}
.chip{font-size:11px;padding:2px 8px;border-radius:999px;background:var(--line);color:var(--text)}
.st{font-size:12px;padding:2px 10px;border-radius:999px;color:#fff}.st.UP{background:var(--green)}.st.DOWN{background:var(--red)}.st.NA{background:var(--gray)}
.btn{border:0;border-radius:8px;padding:7px 14px;cursor:pointer;font:inherit;font-size:13px;color:#fff;background:var(--blue)}
.btn.g{background:var(--green)}.btn.r{background:var(--red)}.btn.s{background:var(--gray)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px}
details{margin-top:10px}summary{cursor:pointer;color:var(--blue);font-size:14px}
.msg{font-size:13px;margin-top:8px}.ok{color:var(--green)}.err{color:var(--red)}
.win{display:flex;align-items:center;gap:6px;font-size:13px}
</style></head><body><div class="wrap">
<h1>&#128270; Extra checks &middot; الفحوصات الإضافية</h1>
<div class="sub">Ping &middot; Port &middot; HTTP &middot; Keyword — بتتضاف على السيرفر نفسه، وبتظهر في اللوحة كخدمات وبتبعت تنبيهات لو وقعت. <a href="/">&larr; Dashboard</a></div>
<div id="list">...</div>
</div>
<script>
var ERR = {bad_type:'Invalid type', bad_port:'Port must be 1-65535 / المنفذ غير صحيح', bad_url:'URL must start with http:// or https://', bad_keyword:'Keyword is required / الكلمة مطلوبة',
  bad_expect:'Expected status format: 200 or 200-299 or 2xx', bad_timeout:'Invalid timeout', too_many:'Max 20 checks per server', not_found:'Not found', bad_server:'Invalid server'};
var servers = [], builtFor = '';
function esc(s){var d=document.createElement('div');d.textContent=s==null?'':String(s);return d.innerHTML;}
function api(url, opts){
  return fetch(url, opts).then(function(r){ return r.json().then(function(j){ if(!r.ok){ var e=new Error(j.detail||'error'); e.code=j.detail; throw e; } return j; }); });
}
function post(url, body){ return api(url, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)}); }
function em(e){ return ERR[e.code] || e.message || 'Error'; }

function target(c){
  if(c.type==='port') return 'Port ' + c.port;
  if(c.type==='ping') return 'ICMP';
  if(c.type==='http') return c.url + '  (expect ' + (c.expect||'200-399') + ')';
  return c.url + '  \u2192 ' + (c.mode==='absent' ? 'must NOT contain' : 'must contain') + ' "' + c.keyword + '"';
}
function resHtml(r){
  if(!r) return '<span class="st NA">...</span>';
  return '<span class="st ' + (r.status==='UP'?'UP':'DOWN') + '">' + esc(r.status) + '</span>';
}
function listHtml(s){
  var rows = '';
  if(s.windows_mode && s.auto_ping){
    rows += '<div class="row"><div class="t"><b>Ping</b> <span class="chip">auto</span><small>' + esc(s.auto_ping.detail||'') + '</small></div>' + resHtml(s.auto_ping) + '</div>';
  }
  rows += s.checks.map(function(c){
    return '<div class="row"><div class="t"><b>' + esc(c.name) + '</b> <span class="chip">' + esc(c.type) + '</span><small>' + esc(target(c)) +
      (c.result && c.result.detail ? ' &mdash; ' + esc(c.result.detail) : '') + '</small></div>' + resHtml(c.result) +
      '<button class="btn r" data-del="' + esc(s.id) + ':' + esc(c.id) + '">Delete</button></div>';
  }).join('');
  return rows || '<div class="sub">No extra checks yet / لا توجد فحوصات إضافية.</div>';
}
function formHtml(s){
  var id = s.id;
  return '<details><summary>+ Add check / إضافة فحص</summary><div data-form="' + id + '">' +
    '<div class="grid">' +
    '<div><label>Type / النوع</label><select class="f-type"><option value="ping">Ping (بينج)</option><option value="port">Port (منفذ)</option><option value="http">HTTP / website</option><option value="keyword">Keyword (كلمة في الموقع)</option></select></div>' +
    '<div><label>Name / الاسم</label><input class="f-name" placeholder="optional"></div>' +
    '<div class="f-port" style="display:none"><label>Port / المنفذ</label><input class="f-portv" type="number" placeholder="3389"></div>' +
    '<div class="f-url" style="display:none"><label>URL</label><input class="f-urlv" placeholder="https://example.com/"></div>' +
    '<div class="f-http" style="display:none"><label>Expected status / الرد المتوقع</label><input class="f-expect" placeholder="200-399"></div>' +
    '<div class="f-kw" style="display:none"><label>Keyword / الكلمة</label><input class="f-kwv"></div>' +
    '<div class="f-kw" style="display:none"><label>Mode / الوضع</label><select class="f-mode"><option value="exists">Must contain / لازم تكون موجودة</option><option value="absent">Must NOT contain / لازم تكون مش موجودة</option></select></div>' +
    '</div>' +
    '<div class="f-url" style="display:none"><label class="win"><input type="checkbox" class="f-ssl" checked> Verify SSL certificate / تحقق من الشهادة</label></div>' +
    '<div class="f-kw" style="display:none"><label class="win"><input type="checkbox" class="f-case"> Case sensitive / حساس لحالة الحروف</label></div>' +
    '<div style="margin-top:10px;display:flex;gap:8px;flex-wrap:wrap"><button class="btn s" data-test="' + id + '">Test now / جرّب</button><button class="btn g" data-add="' + id + '">Add / إضافة</button></div>' +
    '<div class="msg" data-msg="' + id + '"></div></div></details>';
}
function build(){
  document.getElementById('list').innerHTML = servers.map(function(s){
    return '<div class="card" data-card="' + s.id + '"><div class="head"><div><span class="name">' + esc(s.name) + '</span> <span class="ip">' + esc(s.ip) + '</span></div>' +
      '<label class="win"><input type="checkbox" data-win="' + s.id + '"' + (s.windows_mode ? ' checked' : '') + '> Windows server / سيرفر ويندوز (من غير بورتات لينكس. لو ما أضفتش فحوصات بيستخدم Ping)</label></div>' +
      '<div data-checks="' + s.id + '">' + listHtml(s) + '</div>' + formHtml(s) + '</div>';
  }).join('') || '<div class="card">No servers yet.</div>';
}
function update(){
  servers.forEach(function(s){
    var el = document.querySelector('[data-checks="' + s.id + '"]');
    if(el) el.innerHTML = listHtml(s);
    var w = document.querySelector('[data-win="' + s.id + '"]');
    if(w) w.checked = !!s.windows_mode;
  });
}
function load(){
  api('api/server-checks').then(function(d){
    servers = d;
    var sig = d.map(function(s){ return s.id; }).join(',');
    if(sig !== builtFor){ builtFor = sig; build(); } else { update(); }
  }).catch(function(){});
}
function formBody(id){
  var f = document.querySelector('[data-form="' + id + '"]');
  var q = function(c){ return f.querySelector(c); };
  return {server_id: id, type: q('.f-type').value, name: q('.f-name').value, port: q('.f-portv').value, url: q('.f-urlv').value,
    expect: q('.f-expect').value, keyword: q('.f-kwv').value, mode: q('.f-mode').value, verify_ssl: q('.f-ssl').checked, case_sensitive: q('.f-case').checked};
}
function toggle(f){
  var t = f.querySelector('.f-type').value;
  var show = function(cls, on){ var n = f.querySelectorAll(cls); for(var i=0;i<n.length;i++) n[i].style.display = on ? '' : 'none'; };
  show('.f-port', t==='port'); show('.f-url', t==='http'||t==='keyword'); show('.f-http', t==='http'||t==='keyword'); show('.f-kw', t==='keyword');
}
function msg(id, text, ok){ var m = document.querySelector('[data-msg="' + id + '"]'); m.className = 'msg ' + (ok ? 'ok' : 'err'); m.textContent = text; }
document.addEventListener('change', function(e){
  var t = e.target;
  if(t.classList && t.classList.contains('f-type')) toggle(t.closest('[data-form]'));
  if(t.hasAttribute && t.hasAttribute('data-win')){
    post('api/server-windows', {server_id: parseInt(t.getAttribute('data-win'),10), windows: t.checked}).then(load).catch(function(er){ alert(em(er)); });
  }
});
document.addEventListener('click', function(e){
  var t = e.target, v;
  if((v = t.getAttribute && t.getAttribute('data-add'))){
    post('api/server-checks', formBody(parseInt(v,10))).then(function(){ msg(v, 'Added \u2714 (النتيجة تظهر خلال ثواني)', true); load(); }).catch(function(er){ msg(v, em(er), false); });
  } else if((v = t.getAttribute && t.getAttribute('data-test'))){
    msg(v, '...', true);
    post('api/server-checks/test', formBody(parseInt(v,10))).then(function(r){ msg(v, (r.ok ? '\u2714 UP' : '\u2716 DOWN') + ' \u2014 ' + r.detail, r.ok); }).catch(function(er){ msg(v, em(er), false); });
  } else if((v = t.getAttribute && t.getAttribute('data-del'))){
    if(confirm('Delete this check? / حذف الفحص؟')){
      var p = v.split(':');
      api('api/server-checks/' + p[0] + '/' + p[1], {method:'DELETE'}).then(load).catch(function(er){ alert(em(er)); });
    }
  }
});
load(); setInterval(load, 15000);
</script></body></html>
"""


def register(app, get_db):
    global _get_db
    _get_db = get_db
    try:
        ensure_schema(get_db)
    except Exception as e:
        print(f"[checks] schema: {e}")

    def _err(code, http=400):
        return JSONResponse({"detail": code}, status_code=http)

    @app.get("/checks", response_class=HTMLResponse)
    async def checks_page():
        return HTMLResponse(PAGE_HTML)

    @app.get("/api/server-checks")
    def list_checks():
        ensure_schema(get_db)
        conn = get_db()
        rows = conn.execute(
            """SELECT s.id, s.name, s.ip, COALESCE(s.windows_mode, 0) AS windows_mode,
                      COALESCE(s.extra_checks, '[]') AS extra_checks, st.services_json
               FROM servers s LEFT JOIN server_state st ON s.id = st.server_id
               ORDER BY s.name COLLATE NOCASE"""
        ).fetchall()
        conn.close()
        out = []
        for r in rows:
            try:
                checks = json.loads(r["extra_checks"] or "[]")
            except Exception:
                checks = []
            try:
                svcs = json.loads(r["services_json"] or "[]")
            except Exception:
                svcs = []
            res = {str(s.get("key")): s for s in svcs if str(s.get("key", "")).startswith("chk_")}
            items = []
            for c in checks:
                s = res.get("chk_" + str(c.get("id")))
                items.append(dict(c, result=({"status": s.get("status"), "latency_ms": s.get("latency_ms"), "detail": s.get("detail")} if s else None)))
            ap = res.get("chk_autoping")
            out.append({
                "id": r["id"], "name": r["name"], "ip": r["ip"], "windows_mode": bool(r["windows_mode"]),
                "checks": items,
                "auto_ping": ({"status": ap.get("status"), "detail": ap.get("detail")} if ap else None),
            })
        return out

    @app.post("/api/server-checks")
    async def add_check(request: Request):
        b = await request.json()
        ensure_schema(get_db)
        try:
            sid = int(b.get("server_id"))
        except (TypeError, ValueError):
            return _err("bad_server")
        try:
            chk = clean_check(b)
        except ValueError as e:
            return _err(str(e))
        conn = get_db()
        row = conn.execute("SELECT extra_checks FROM servers WHERE id = ?", (sid,)).fetchone()
        if not row:
            conn.close()
            return _err("not_found", 404)
        try:
            lst = json.loads(row["extra_checks"] or "[]")
        except Exception:
            lst = []
        if len(lst) >= 20:
            conn.close()
            return _err("too_many")
        chk["id"] = uuid.uuid4().hex[:8]
        lst.append(chk)
        conn.execute("UPDATE servers SET extra_checks = ? WHERE id = ?", (json.dumps(lst, ensure_ascii=False), sid))
        conn.commit()
        conn.close()
        return {"ok": True, "id": chk["id"]}

    @app.delete("/api/server-checks/{sid}/{cid}")
    def delete_check(sid: int, cid: str):
        ensure_schema(get_db)
        conn = get_db()
        row = conn.execute("SELECT extra_checks FROM servers WHERE id = ?", (sid,)).fetchone()
        if not row:
            conn.close()
            return _err("not_found", 404)
        try:
            lst = json.loads(row["extra_checks"] or "[]")
        except Exception:
            lst = []
        lst = [c for c in lst if str(c.get("id")) != cid]
        conn.execute("UPDATE servers SET extra_checks = ? WHERE id = ?", (json.dumps(lst, ensure_ascii=False), sid))
        conn.commit()
        conn.close()
        _state.pop((sid, cid), None)
        return {"ok": True}

    @app.post("/api/server-windows")
    async def set_windows(request: Request):
        b = await request.json()
        ensure_schema(get_db)
        try:
            sid = int(b.get("server_id"))
        except (TypeError, ValueError):
            return _err("bad_server")
        conn = get_db()
        conn.execute("UPDATE servers SET windows_mode = ? WHERE id = ?", (1 if b.get("windows") else 0, sid))
        conn.commit()
        conn.close()
        return {"ok": True}

    @app.post("/api/server-checks/test")
    async def test_check(request: Request):
        b = await request.json()
        ensure_schema(get_db)
        try:
            sid = int(b.get("server_id"))
        except (TypeError, ValueError):
            return _err("bad_server")
        try:
            chk = clean_check(b)
        except ValueError as e:
            return _err(str(e))
        conn = get_db()
        row = conn.execute("SELECT id, ip FROM servers WHERE id = ?", (sid,)).fetchone()
        conn.close()
        if not row:
            return _err("not_found", 404)
        try:
            ok, ms, detail = await _run_once({"id": row["id"], "ip": row["ip"]}, chk)
        except Exception as e:
            ok, ms, detail = False, 0.0, f"error: {e}"
        return {"ok": bool(ok), "latency_ms": ms, "detail": detail}
