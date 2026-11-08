"""
extras.py  —  إضافات لمشروع server-monitor
1) تقرير كل ساعة (Load / RAM / Disk / CPU / Uptime + التاريخ والوقت) على Telegram و Slack
2) صفحة Status عامة /status تجمع كل السيرفرات (شبيهة بـ UptimeRobot)

لا يغيّر أي سلوك موجود. بيقرأ من نفس الـ DB والإعدادات (Telegram / Slack).
"""
import asyncio
import json
import os
import time
from datetime import datetime, timedelta

import httpx
from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse

REPORT_EVERY_SEC = int(os.getenv("REPORT_EVERY_SEC", "3600"))  # كل ساعة
REPORTS_ENABLED = os.getenv("HOURLY_REPORTS", "0") == "1"   # التقرير الدوري مقفول افتراضياً
DISK_ALERT_PCT = float(os.getenv("DISK_ALERT_PCT", "90"))    # تنبيه لو الهارد وصل الحد ده
RAM_ALERT_PCT = float(os.getenv("RAM_ALERT_PCT", "90"))      # تنبيه لو الرام وصلت الحد ده
ALERT_REPEAT_SEC = int(os.getenv("ALERT_REPEAT_SEC", "21600"))  # ما يكررش نفس التنبيه قبل 6 ساعات
STATUS_TITLE = os.getenv("STATUS_TITLE", "حالة السيرفرات")


def register(app, get_db, get_settings_dict, base_dir):
    """يسجّل صفحة /status و API ويرجع دالة start() تنتج الـ background tasks."""

    # ───────────────────────── Uptime اليومي (للـ 30/90 يوم) ─────────────────────────
    def init_tables():
        conn = get_db()
        conn.execute(
            """CREATE TABLE IF NOT EXISTS uptime_daily (
                   server_id INTEGER NOT NULL,
                   day TEXT NOT NULL,
                   total INTEGER DEFAULT 0,
                   up INTEGER DEFAULT 0,
                   PRIMARY KEY (server_id, day)
               )"""
        )
        cols = [r[1] for r in conn.execute("PRAGMA table_info(servers)").fetchall()]
        if "public_visible" not in cols:
            conn.execute("ALTER TABLE servers ADD COLUMN public_visible INTEGER DEFAULT 1")
        conn.commit()
        conn.close()

    def aggregate(days_back: int = 1):
        since = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d 00:00:00")
        conn = get_db()
        conn.execute(
            """INSERT OR REPLACE INTO uptime_daily (server_id, day, total, up)
               SELECT server_id, substr(created_at, 1, 10),
                      COUNT(*), SUM(CASE WHEN status = 'UP' THEN 1 ELSE 0 END)
               FROM uptime_checks
               WHERE created_at >= ?
               GROUP BY server_id, substr(created_at, 1, 10)""",
            (since,),
        )
        conn.commit()
        conn.close()

    async def aggregate_loop():
        await asyncio.to_thread(aggregate, 7)  # backfill أول مرة
        while True:
            await asyncio.sleep(60)
            try:
                await asyncio.to_thread(aggregate, 1)
            except Exception as e:
                print(f"[aggregate_loop] {e}")

    # ───────────────────────── صفحة Status العامة ─────────────────────────
    @app.get("/status", response_class=HTMLResponse)
    async def status_page():
        path = os.path.join(base_dir, "status.html")
        with open(path, "r", encoding="utf-8") as f:
            html = f.read().replace("__STATUS_TITLE__", STATUS_TITLE)
        return HTMLResponse(html)

    def _pct(up, total):
        return None if not total else round(up * 100.0 / total, 2)

    @app.get("/api/public-status")
    def public_status():
        conn = get_db()
        servers = conn.execute(
            """SELECT s.id, s.name, st.status, st.uptime_24h, st.ping_ms, st.last_checked
               FROM servers s LEFT JOIN server_state st ON s.id = st.server_id
               WHERE COALESCE(s.public_visible, 1) = 1
               ORDER BY s.name COLLATE NOCASE"""
        ).fetchall()
        since = (datetime.now() - timedelta(days=89)).strftime("%Y-%m-%d")
        daily = conn.execute(
            "SELECT server_id, day, total, up FROM uptime_daily WHERE day >= ?", (since,)
        ).fetchall()
        conn.close()

        by_server = {}
        for r in daily:
            by_server.setdefault(r["server_id"], {})[r["day"]] = (r["total"] or 0, r["up"] or 0)

        today = datetime.now().date()
        day_list = [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(89, -1, -1)]

        out, up_count, down_count = [], 0, 0
        for s in servers:
            rows = by_server.get(s["id"], {})
            bars = [{"day": d, "pct": _pct(rows.get(d, (0, 0))[1], rows.get(d, (0, 0))[0])} for d in day_list]

            def window(n):
                t = u = 0
                for d in day_list[-n:]:
                    tt, uu = rows.get(d, (0, 0))
                    t += tt
                    u += uu
                return _pct(u, t)

            status = s["status"] or "UNKNOWN"
            if status == "UP":
                up_count += 1
            elif status == "DOWN":
                down_count += 1
            out.append(
                {
                    "name": s["name"],
                    "status": status,
                    "ping_ms": s["ping_ms"],
                    "last_checked": s["last_checked"],
                    "uptime_24h": s["uptime_24h"],
                    "uptime_7d": window(7),
                    "uptime_30d": window(30),
                    "uptime_90d": window(90),
                    "bars": bars,
                }
            )

        overall = "up" if down_count == 0 else ("down" if up_count == 0 else "partial")
        return JSONResponse(
            {
                "overall": overall,
                "up": up_count,
                "down": down_count,
                "total": len(out),
                "servers": out,
                "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
        )

    # ───────────────────────── التحكم في السيرفرات الظاهرة ─────────────────────────
    ADMIN_HTML = """<!DOCTYPE html><html lang="ar" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Status visibility</title>
<style>body{font-family:system-ui,Tahoma,sans-serif;background:#f5f7fa;color:#1f2933;margin:0}
.w{max-width:700px;margin:0 auto;padding:24px 16px}.c{background:#fff;border:1px solid #e3e8ee;border-radius:10px;padding:14px 16px;margin-bottom:10px;display:flex;justify-content:space-between;align-items:center;gap:10px}
small{color:#6b7785;display:block}label{display:flex;align-items:center;gap:8px;cursor:pointer;white-space:nowrap}
input{width:20px;height:20px}a{color:#16a34a}</style></head><body><div class="w">
<h2>إظهار / إخفاء السيرفرات في صفحة الحالة &middot; Show / hide on status page</h2>
<p><a href="/status">/status</a> &middot; <a href="/">Dashboard</a></p><div id="l">...</div></div>
<script>
async function load(){const r=await fetch('/api/status-visibility');const d=await r.json();
document.getElementById('l').innerHTML=d.map(s=>'<div class="c"><div><b>'+esc(s.name)+'</b><small>'+esc(s.ip)+'</small></div>'+
'<label><input type="checkbox" '+(s.public_visible?'checked':'')+' onchange="tog('+s.id+',this.checked)"> ظاهر / Visible</label></div>').join('')||'No servers';}
function esc(s){const d=document.createElement('div');d.textContent=s==null?'':String(s);return d.innerHTML}
async function tog(id,v){await fetch('/api/status-visibility',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:id,visible:v})});}
load();</script></body></html>"""

    @app.get("/status-settings", response_class=HTMLResponse)
    async def status_settings_page():
        return HTMLResponse(ADMIN_HTML)

    @app.get("/api/status-visibility")
    def status_visibility_list():
        conn = get_db()
        rows = conn.execute(
            "SELECT id, name, ip, COALESCE(public_visible, 1) AS public_visible FROM servers ORDER BY name COLLATE NOCASE"
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    @app.post("/api/status-visibility")
    async def status_visibility_set(request: Request):
        body = await request.json()
        conn = get_db()
        conn.execute(
            "UPDATE servers SET public_visible = ? WHERE id = ?",
            (1 if body.get("visible") else 0, int(body.get("id"))),
        )
        conn.commit()
        conn.close()
        return {"ok": True}

    # ───────────────────────── تنبيهات الهارد والرام (فقط عند تجاوز الحد) ─────────────────────────
    _last_alert = {}

    async def check_thresholds():
        conn = get_db()
        rows = conn.execute(
            """SELECT s.id, s.name, s.ip, st.*
               FROM servers s JOIN server_state st ON s.id = st.server_id"""
        ).fetchall()
        conn.close()
        settings = get_settings_dict()
        now = time.time()
        for row in rows:
            if not row["ssh_connected"] or row["status"] != "UP":
                continue
            for key, label, val, limit, detail in (
                ("disk", "💽 مساحة الهارد / Disk", row["disk_usage"], DISK_ALERT_PCT, row["disk_detail"]),
                ("ram", "🧠 الرام / RAM", row["ram_usage"], RAM_ALERT_PCT, row["ram_detail"]),
            ):
                k = (row["id"], key)
                if (val or 0) >= limit:
                    if now - _last_alert.get(k, 0) >= ALERT_REPEAT_SEC:
                        _last_alert[k] = now
                        text = (
                            f"⚠️ تحذير: {label} وصل {val}%\n"
                            f"🖥 السيرفر: {row['name']} ({row['ip']})\n"
                            f"📊 التفاصيل: {detail or '-'}\n"
                            f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                        )
                        await send_text(settings, text)
                else:
                    _last_alert.pop(k, None)  # رجع طبيعي، لو عدّى الحد تاني يبعت من جديد

    async def threshold_loop():
        await asyncio.sleep(90)
        while True:
            try:
                await check_thresholds()
            except Exception as e:
                print(f"[threshold_loop] {e}")
            await asyncio.sleep(60)

    # ───────────────────────── التقرير كل ساعة ─────────────────────────
    async def send_text(settings, text):
        results = []
        async with httpx.AsyncClient(timeout=8.0) as client:
            token = (settings.get("telegram_bot_token") or "").strip()
            if settings.get("telegram_enabled", "0") != "1":
                token = ""
            if token.lower().startswith("bot") and ":" in token:
                token = token[3:].strip()
            chat_id = (settings.get("telegram_chat_id") or "").strip()
            if token and chat_id:
                try:
                    r = await client.post(
                        f"https://api.telegram.org/bot{token}/sendMessage",
                        json={"chat_id": chat_id, "text": text},
                    )
                    results.append(f"telegram: {r.status_code} {r.text[:150]}")
                except Exception as e:
                    print(f"[report] telegram error: {e}")
                    results.append(f"telegram error: {e}")
            hook = (settings.get("slack_webhook_url") or "").strip()
            if settings.get("slack_enabled", "0") != "1":
                hook = ""
            if hook:
                try:
                    r = await client.post(hook, json={"text": text})
                    results.append(f"slack: {r.status_code}")
                except Exception as e:
                    print(f"[report] slack error: {e}")
                    results.append(f"slack error: {e}")
        return results

    def build_report(row):
        mail = json.loads(row["mail_stats_json"] or "{}")
        status = row["status"] or "UNKNOWN"
        icon = "🟢" if status == "UP" else ("🔴" if status == "DOWN" else "⚪")
        has_metrics = bool(row["ssh_connected"])
        if has_metrics:
            cores = mail.get("cpu_cores") or "?"
            load = f"{mail.get('load_str') or 'N/A'}  ({cores} cores)"
            cpu = f"{row['cpu_usage']}%"
            ram = f"{row['ram_usage']}%  ({row['ram_detail']})"
            disk = f"{row['disk_usage']}%  ({row['disk_detail']})"
        else:
            na = "غير متاح (أضف بيانات SSH للسيرفر)"
            load = cpu = ram = disk = na
        now = datetime.now()
        return (
            f"📊 تقرير الساعة — {row['name']}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🌐 IP: {row['ip']}\n"
            f"{icon} الحالة: {status}\n"
            f"⏱ Uptime: {row['uptime_str']}  (24h: {row['uptime_24h']}%)\n"
            f"⚙️ Load Avg: {load}\n"
            f"🧮 CPU: {cpu}\n"
            f"🧠 RAM: {ram}\n"
            f"💽 Disk: {disk}\n"
            f"📅 التاريخ: {now.strftime('%Y-%m-%d')}\n"
            f"🕒 الوقت: {now.strftime('%H:%M:%S')}"
        )

    async def send_reports():
        conn = get_db()
        rows = conn.execute(
            """SELECT s.name, s.ip, st.*
               FROM servers s JOIN server_state st ON s.id = st.server_id
               ORDER BY s.name COLLATE NOCASE"""
        ).fetchall()
        conn.close()
        settings = get_settings_dict()
        log = []
        for row in rows:
            log.append({"server": row["name"], "results": await send_text(settings, build_report(row))})
            await asyncio.sleep(1)  # علشان Telegram ما يعملش rate-limit
        return log

    async def report_loop():
        await asyncio.sleep(60)  # أول تقرير بعد دقيقة من التشغيل
        while True:
            try:
                await send_reports()
            except Exception as e:
                print(f"[report_loop] {e}")
            await asyncio.sleep(REPORT_EVERY_SEC)

    # اختبار يدوي: افتح الرابط في المتصفح /api/reports/send-now
    @app.api_route("/api/reports/send-now", methods=["GET", "POST"])
    async def reports_send_now():
        log = await send_reports()
        if not log:
            return {"ok": False, "message": "مفيش سيرفرات مضافة"}
        sent = any(r["results"] for r in log)
        msg = "اتبعتت" if sent else "ما اتبعتش: فعّل Telegram أو Slack من الإعدادات واكتب Token و Chat ID"
        return {"ok": sent, "message": msg, "details": log}

    def start():
        init_tables()
        coros = [aggregate_loop(), threshold_loop()]
        if REPORTS_ENABLED:
            coros.append(report_loop())
        return coros

    return start
