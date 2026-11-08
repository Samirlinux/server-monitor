import asyncio
import json
import os
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

import extras
from monitor_engine import (
    DEFAULT_SERVICE_PORTS,
    execute_service_action,
    fetch_remote_logs,
    inspect_server,
    send_alerts,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "monitor.db")
TEMPLATE_DIRS = [
    d for d in [os.path.join(BASE_DIR, "templates"), BASE_DIR] if os.path.isdir(d)
]
templates = Jinja2Templates(directory=TEMPLATE_DIRS)


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = get_db()
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE IF NOT EXISTS servers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            ip TEXT NOT NULL,
            ssh_port INTEGER DEFAULT 22,
            ssh_user TEXT DEFAULT 'root',
            ssh_password TEXT DEFAULT '',
            ssh_key TEXT DEFAULT '',
            check_ports TEXT DEFAULT '',
            mail_queue_limit INTEGER DEFAULT 100,
            spam_threshold INTEGER DEFAULT 20,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS server_state (
            server_id INTEGER PRIMARY KEY,
            status TEXT DEFAULT 'UNKNOWN',
            ping_ms REAL DEFAULT 0,
            ssh_connected INTEGER DEFAULT 0,
            uptime_str TEXT DEFAULT 'جاري الفحص...',
            uptime_24h REAL DEFAULT 100.0,
            cpu_usage REAL DEFAULT 0,
            ram_usage REAL DEFAULT 0,
            ram_detail TEXT DEFAULT '',
            disk_usage REAL DEFAULT 0,
            disk_detail TEXT DEFAULT '',
            services_json TEXT DEFAULT '[]',
            mail_stats_json TEXT DEFAULT '{}',
            rbl_status_json TEXT DEFAULT '{}',
            last_checked TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS uptime_checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            server_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            ping_ms REAL DEFAULT 0,
            services_up INTEGER DEFAULT 0,
            services_total INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_uptime_server_time
        ON uptime_checks(server_id, created_at);

        CREATE TABLE IF NOT EXISTS incidents_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            server_id INTEGER NOT NULL,
            server_name TEXT NOT NULL,
            server_ip TEXT NOT NULL,
            severity TEXT NOT NULL,
            service_name TEXT NOT NULL,
            title TEXT NOT NULL,
            details TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """
    )

    default_settings = {
        "telegram_enabled": "0",
        "telegram_bot_token": "",
        "telegram_chat_id": "",
        "slack_enabled": "0",
        "slack_webhook_url": "",
        "check_interval_sec": "5",
        "alert_on_recovery": "1",
        "alert_on_spam": "1",
    }
    for k, v in default_settings.items():
        cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))

    conn.commit()
    conn.close()


def get_settings_dict() -> Dict[str, str]:
    conn = get_db()
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    conn.close()
    return {r["key"]: r["value"] for r in rows}


def record_incident(
    server_id: int,
    server_name: str,
    server_ip: str,
    severity: str,
    service_name: str,
    title: str,
    details: str,
) -> None:
    conn = get_db()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        """
        INSERT INTO incidents_logs
        (server_id, server_name, server_ip, severity, service_name, title, details, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (server_id, server_name, server_ip, severity, service_name, title, details, now_str),
    )
    conn.commit()
    conn.close()


# Track RBL scan cycles & spam alert cooldowns in memory
_cycle_counter = 0
_spam_alert_cooldown: Dict[int, float] = {}


async def check_and_update_single_server(
    server: Dict[str, Any], force_rbl: bool = False
) -> Dict[str, Any]:
    server_id = server["id"]
    server_name = server["name"]
    server_ip = server["ip"]

    conn = get_db()
    prev_row = conn.execute(
        "SELECT * FROM server_state WHERE server_id = ?", (server_id,)
    ).fetchone()
    conn.close()

    prev_status = prev_row["status"] if prev_row else "UNKNOWN"
    prev_services_list = (
        json.loads(prev_row["services_json"])
        if (prev_row and prev_row["services_json"])
        else []
    )
    prev_rbl = (
        json.loads(prev_row["rbl_status_json"])
        if (prev_row and prev_row["rbl_status_json"])
        else None
    )

    prev_service_map = {s["key"]: s["status"] for s in prev_services_list}

    # Run RBL check on first run, every 24 cycles (~2 mins), or when forced
    run_rbl = force_rbl or (prev_rbl is None) or (_cycle_counter % 24 == 0)

    inspection = await inspect_server(
        server, run_rbl_check=run_rbl, previous_rbl=prev_rbl
    )

    new_status = inspection["status"]
    new_services = inspection["services"]
    mail_stats = inspection["mail_stats"]
    rbl_status = inspection["rbl_status"]
    settings = get_settings_dict()

    ram_info = f"{inspection['ram_usage']}%" + (f" ({inspection['ram_detail']})" if inspection.get("ram_detail") else "")
    disk_info = f"{inspection['disk_usage']}%" + (f" ({inspection['disk_detail']})" if inspection.get("disk_detail") else "")
    load_info = inspection.get("load_str") or f"CPU {inspection['cpu_usage']}%"
    if inspection.get("cpu_cores"):
        load_info += f" ({inspection['cpu_cores']} Cores)"

    extra_stats = {
        "وقت التشغيل (Uptime)": inspection["uptime_str"],
        "لود السيرفر (Load Avg)": f"{load_info} | CPU: {inspection['cpu_usage']}%",
        "استهلاك الرام (RAM)": ram_info,
        "مساحة السيرفر (Disk)": disk_info,
        "الاستجابة (Ping)": f"{inspection['ping_ms']} ms",
        "الرسائل المرسلة": str(mail_stats.get("sent_count", 0)),
        "طابور الإرسال (Queue)": str(mail_stats.get("queue_count", 0)),
    }

    # 1. Detect full server UP <-> DOWN transitions
    if prev_status in ("UP", "UNKNOWN") and new_status == "DOWN":
        title = "توقف السيرفر بالكامل (SERVER DOWN)"
        details = f"السيرفر {server_name} ({server_ip}) لا يستجيب لأي اتصال أو بورت مفتوح!"
        record_incident(server_id, server_name, server_ip, "CRITICAL", "Server Reachability", title, details)
        await send_alerts(
            settings, title, server_name, server_ip, "CRITICAL", "Server Host", details, extra_stats
        )
    elif prev_status == "DOWN" and new_status == "UP":
        title = "عودة السيرفر للعمل (SERVER RECOVERED)"
        details = f"عاد السيرفر {server_name} ({server_ip}) للاتصال والعمل بشكل طبيعي."
        record_incident(server_id, server_name, server_ip, "RECOVERY", "Server Reachability", title, details)
        if settings.get("alert_on_recovery", "1") == "1":
            await send_alerts(
                settings, title, server_name, server_ip, "RECOVERY", "Server Host", details, extra_stats
            )

    # 2. Detect individual service transitions (Apache, MySQL, Mail, systemd services)
    if new_status == "UP":
        for svc in new_services:
            s_key = svc["key"]
            s_name = svc["name"]
            s_status = svc["status"]
            old_s_status = prev_service_map.get(s_key)

            # Alert when a previously UP service goes DOWN, or if a systemd service is dead
            if old_s_status == "UP" and s_status == "DOWN":
                title = f"توقف خدمة: {s_name}"
                details = f"فصلت خدمة {s_name} على السيرفر {server_name} — ({svc.get('detail', '')})"
                record_incident(server_id, server_name, server_ip, "CRITICAL", s_name, title, details)
                await send_alerts(
                    settings, title, server_name, server_ip, "CRITICAL", s_name, details, extra_stats
                )
            elif old_s_status == "DOWN" and s_status == "UP":
                title = f"عودة خدمة للعمل: {s_name}"
                details = f"عادت خدمة {s_name} للعمل بنجاح ({svc.get('detail', '')})"
                record_incident(server_id, server_name, server_ip, "RECOVERY", s_name, title, details)
                if settings.get("alert_on_recovery", "1") == "1":
                    await send_alerts(
                        settings, title, server_name, server_ip, "RECOVERY", s_name, details, extra_stats
                    )

    # 3. Detect Spam / Mail Queue / Blacklist issues (with 10-minute alert cooldown per server)
    if mail_stats.get("has_spam_issue") and settings.get("alert_on_spam", "1") == "1":
        now_ts = datetime.now().timestamp()
        last_spam_ts = _spam_alert_cooldown.get(server_id, 0)
        if now_ts - last_spam_ts > 600:
            _spam_alert_cooldown[server_id] = now_ts
            warnings_joined = " | ".join(mail_stats.get("spam_warnings", []))
            title = "تحذير سبام أو مشكلة في إرسال الإيميلات (SPAM / MAIL ALERT)"
            record_incident(
                server_id, server_name, server_ip, "WARNING", "Mail & Anti-Spam", title, warnings_joined
            )
            await send_alerts(
                settings,
                title,
                server_name,
                server_ip,
                "WARNING",
                "Mail / Anti-Spam Monitor",
                warnings_joined,
                extra_stats,
            )

    # 4. Save heartbeat & compute 24h uptime %
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cutoff_24h = (datetime.now() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    services_up_count = sum(1 for s in new_services if s["status"] == "UP")
    services_total_count = len(new_services)

    conn = get_db()
    conn.execute(
        """
        INSERT INTO uptime_checks (server_id, status, ping_ms, services_up, services_total, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            server_id,
            new_status,
            inspection["ping_ms"],
            services_up_count,
            services_total_count,
            now_str,
        ),
    )

    # Keep uptime_checks table lean (delete records older than 7 days occasionally)
    if _cycle_counter % 200 == 0:
        cutoff_7d = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("DELETE FROM uptime_checks WHERE created_at < ?", (cutoff_7d,))

    stats_24h = conn.execute(
        """
        SELECT COUNT(*) as total,
               SUM(CASE WHEN status = 'UP' THEN 1 ELSE 0 END) as up_count
        FROM uptime_checks
        WHERE server_id = ? AND created_at >= ?
        """,
        (server_id, cutoff_24h),
    ).fetchone()

    total_checks = stats_24h["total"] or 1
    up_checks = stats_24h["up_count"] or 0
    uptime_24h = round((up_checks / total_checks) * 100, 2)

    conn.execute(
        """
        INSERT OR REPLACE INTO server_state (
            server_id, status, ping_ms, ssh_connected, uptime_str, uptime_24h,
            cpu_usage, ram_usage, ram_detail, disk_usage, disk_detail,
            services_json, mail_stats_json, rbl_status_json, last_checked
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            server_id,
            new_status,
            inspection["ping_ms"],
            1 if inspection["ssh_connected"] else 0,
            inspection["uptime_str"],
            uptime_24h,
            inspection["cpu_usage"],
            inspection["ram_usage"],
            inspection["ram_detail"],
            inspection["disk_usage"],
            inspection["disk_detail"],
            json.dumps(new_services, ensure_ascii=False),
            json.dumps(mail_stats, ensure_ascii=False),
            json.dumps(rbl_status, ensure_ascii=False),
            inspection["last_checked"],
        ),
    )
    conn.commit()
    conn.close()
    return inspection


async def monitor_loop() -> None:
    """Background loop checking all servers every 5 seconds concurrently."""
    global _cycle_counter
    while True:
        loop_start = asyncio.get_running_loop().time()
        _cycle_counter += 1
        try:
            conn = get_db()
            servers = [dict(r) for r in conn.execute("SELECT * FROM servers").fetchall()]
            conn.close()

            if servers:
                await asyncio.gather(
                    *(check_and_update_single_server(srv) for srv in servers),
                    return_exceptions=True,
                )
        except Exception as e:
            print(f"[Monitor Loop Error] {e}")

        settings = get_settings_dict()
        try:
            interval = max(3, int(settings.get("check_interval_sec", "5")))
        except ValueError:
            interval = 5

        elapsed = asyncio.get_running_loop().time() - loop_start
        sleep_for = max(0.5, interval - elapsed)
        await asyncio.sleep(sleep_for)


extras_start = None  # بيتعبّى بعد إنشاء app (الأسطر تحت)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    task = asyncio.create_task(monitor_loop())
    # تقرير كل ساعة + تجميع الـ uptime اليومي لصفحة /status
    extra_tasks = [asyncio.create_task(c) for c in extras_start()]
    yield
    task.cancel()
    for t in extra_tasks:
        t.cancel()


app = FastAPI(
    title="Linux Server & Mail Uptime Monitor — Developed by Eng. Samir Abo El-Khair",
    version="1.0.0",
    lifespan=lifespan,
)

# يسجّل: /status  و  /api/public-status  و  POST /api/reports/send-now
extras_start = extras.register(app, get_db, get_settings_dict, BASE_DIR)


# ─── Pydantic Payloads ───────────────────────────────────────────────────────

class ServerPayload(BaseModel):
    name: str
    ip: str
    ssh_port: int = 22
    ssh_user: str = "root"
    ssh_password: str = ""
    ssh_key: str = ""
    mail_queue_limit: int = 100
    spam_threshold: int = 20
    selected_ports: Optional[List[int]] = None


class ServiceActionPayload(BaseModel):
    service_name: str
    action: str = "restart"


class SettingsPayload(BaseModel):
    telegram_enabled: str = "0"
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    slack_enabled: str = "0"
    slack_webhook_url: str = ""
    check_interval_sec: str = "5"
    alert_on_recovery: str = "1"
    alert_on_spam: str = "1"


# ─── HTML Pages ──────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard_page(request: Request):
    return templates.TemplateResponse(request=request, name="index.html", context={})


@app.get("/server/{server_id}", response_class=HTMLResponse)
async def server_detail_page(request: Request, server_id: int):
    conn = get_db()
    srv = conn.execute("SELECT id, name, ip FROM servers WHERE id = ?", (server_id,)).fetchone()
    conn.close()
    if not srv:
        raise HTTPException(status_code=404, detail="السيرفر غير موجود")
    return templates.TemplateResponse(
        request=request,
        name="server_detail.html",
        context={"server_id": server_id, "server_name": srv["name"], "server_ip": srv["ip"]},
    )


# ─── API Endpoints ───────────────────────────────────────────────────────────

@app.get("/api/overview")
async def api_overview():
    conn = get_db()
    rows = conn.execute(
        """
        SELECT s.id, s.name, s.ip, s.ssh_port, s.ssh_user,
               s.mail_queue_limit, s.spam_threshold, s.created_at,
               CASE WHEN (s.ssh_password != '' OR s.ssh_key != '') THEN 1 ELSE 0 END AS has_ssh_creds,
               st.status, st.ping_ms, st.ssh_connected, st.uptime_str, st.uptime_24h,
               st.cpu_usage, st.ram_usage, st.ram_detail, st.disk_usage, st.disk_detail,
               st.services_json, st.mail_stats_json, st.rbl_status_json, st.last_checked
        FROM servers s
        LEFT JOIN server_state st ON s.id = st.server_id
        ORDER BY s.id DESC
        """
    ).fetchall()

    incidents = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM incidents_logs ORDER BY id DESC LIMIT 40"
        ).fetchall()
    ]
    conn.close()

    servers_out = []
    total_sent_emails = 0
    total_spam_alerts = 0
    up_servers = 0
    down_servers = 0

    for r in rows:
        d = dict(r)
        d["services"] = json.loads(d.pop("services_json") or "[]")
        d["mail_stats"] = json.loads(d.pop("mail_stats_json") or "{}")
        d["rbl_status"] = json.loads(d.pop("rbl_status_json") or "{}")

        if d["status"] == "UP":
            up_servers += 1
        elif d["status"] == "DOWN":
            down_servers += 1

        total_sent_emails += int(d["mail_stats"].get("sent_count", 0))
        if d["mail_stats"].get("has_spam_issue") or d["rbl_status"].get("is_blacklisted"):
            total_spam_alerts += 1

        servers_out.append(d)

    return {
        "summary": {
            "total_servers": len(servers_out),
            "up_servers": up_servers,
            "down_servers": down_servers,
            "total_sent_emails": total_sent_emails,
            "total_spam_alerts": total_spam_alerts,
            "server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        },
        "servers": servers_out,
        "incidents": incidents,
    }


@app.get("/api/servers/{server_id}")
async def api_server_detail(server_id: int):
    conn = get_db()
    row = conn.execute(
        """
        SELECT s.id, s.name, s.ip, s.ssh_port, s.ssh_user,
               s.mail_queue_limit, s.spam_threshold, s.check_ports, s.created_at,
               CASE WHEN (s.ssh_password != '' OR s.ssh_key != '') THEN 1 ELSE 0 END AS has_ssh_creds,
               st.status, st.ping_ms, st.ssh_connected, st.uptime_str, st.uptime_24h,
               st.cpu_usage, st.ram_usage, st.ram_detail, st.disk_usage, st.disk_detail,
               st.services_json, st.mail_stats_json, st.rbl_status_json, st.last_checked
        FROM servers s
        LEFT JOIN server_state st ON s.id = st.server_id
        WHERE s.id = ?
        """,
        (server_id,),
    ).fetchone()

    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="السيرفر غير موجود")

    # Last 60 heartbeat checks for live timeline chart
    heartbeats = [
        dict(r)
        for r in conn.execute(
            """
            SELECT status, ping_ms, services_up, services_total, created_at
            FROM uptime_checks
            WHERE server_id = ?
            ORDER BY id DESC
            LIMIT 60
            """,
            (server_id,),
        ).fetchall()
    ]
    heartbeats.reverse()

    # Calculate 7-day uptime %
    cutoff_7d = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    stats_7d = conn.execute(
        """
        SELECT COUNT(*) as total,
               SUM(CASE WHEN status = 'UP' THEN 1 ELSE 0 END) as up_count
        FROM uptime_checks
        WHERE server_id = ? AND created_at >= ?
        """,
        (server_id, cutoff_7d),
    ).fetchone()
    uptime_7d = (
        round(((stats_7d["up_count"] or 0) / stats_7d["total"]) * 100, 2)
        if stats_7d and stats_7d["total"]
        else 100.0
    )

    incidents = [
        dict(r)
        for r in conn.execute(
            """
            SELECT * FROM incidents_logs
            WHERE server_id = ?
            ORDER BY id DESC
            LIMIT 50
            """,
            (server_id,),
        ).fetchall()
    ]
    conn.close()

    data = dict(row)
    data["services"] = json.loads(data.pop("services_json") or "[]")
    data["mail_stats"] = json.loads(data.pop("mail_stats_json") or "{}")
    data["rbl_status"] = json.loads(data.pop("rbl_status_json") or "{}")
    data["uptime_7d"] = uptime_7d
    data["heartbeats"] = heartbeats
    data["incidents"] = incidents
    return data


@app.post("/api/servers")
async def api_add_server(payload: ServerPayload):
    ports_cfg = DEFAULT_SERVICE_PORTS
    if payload.selected_ports:
        ports_cfg = [
            p for p in DEFAULT_SERVICE_PORTS if p["port"] in payload.selected_ports
        ] or DEFAULT_SERVICE_PORTS

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO servers (
            name, ip, ssh_port, ssh_user, ssh_password, ssh_key,
            check_ports, mail_queue_limit, spam_threshold, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            payload.name.strip(),
            payload.ip.strip(),
            payload.ssh_port,
            payload.ssh_user.strip() or "root",
            payload.ssh_password,
            payload.ssh_key,
            json.dumps(ports_cfg, ensure_ascii=False),
            payload.mail_queue_limit,
            payload.spam_threshold,
            now_str,
        ),
    )
    server_id = cur.lastrowid
    conn.commit()
    srv_row = dict(conn.execute("SELECT * FROM servers WHERE id = ?", (server_id,)).fetchone())
    conn.close()

    # Trigger immediate check in background so UI sees data right away
    asyncio.create_task(check_and_update_single_server(srv_row, force_rbl=True))
    return {"ok": True, "id": server_id}


@app.put("/api/servers/{server_id}")
async def api_update_server(server_id: int, payload: ServerPayload):
    conn = get_db()
    existing = conn.execute("SELECT * FROM servers WHERE id = ?", (server_id,)).fetchone()
    if not existing:
        conn.close()
        raise HTTPException(status_code=404, detail="السيرفر غير موجود")

    new_pass = payload.ssh_password if payload.ssh_password != "__KEEP__" else existing["ssh_password"]
    new_key = payload.ssh_key if payload.ssh_key != "__KEEP__" else existing["ssh_key"]

    ports_cfg = DEFAULT_SERVICE_PORTS
    if payload.selected_ports:
        ports_cfg = [
            p for p in DEFAULT_SERVICE_PORTS if p["port"] in payload.selected_ports
        ] or DEFAULT_SERVICE_PORTS

    conn.execute(
        """
        UPDATE servers
        SET name = ?, ip = ?, ssh_port = ?, ssh_user = ?,
            ssh_password = ?, ssh_key = ?, check_ports = ?,
            mail_queue_limit = ?, spam_threshold = ?
        WHERE id = ?
        """,
        (
            payload.name.strip(),
            payload.ip.strip(),
            payload.ssh_port,
            payload.ssh_user.strip() or "root",
            new_pass,
            new_key,
            json.dumps(ports_cfg, ensure_ascii=False),
            payload.mail_queue_limit,
            payload.spam_threshold,
            server_id,
        ),
    )
    conn.commit()
    srv_row = dict(conn.execute("SELECT * FROM servers WHERE id = ?", (server_id,)).fetchone())
    conn.close()

    asyncio.create_task(check_and_update_single_server(srv_row, force_rbl=True))
    return {"ok": True}


@app.delete("/api/servers/{server_id}")
async def api_delete_server(server_id: int):
    conn = get_db()
    conn.execute("DELETE FROM servers WHERE id = ?", (server_id,))
    conn.execute("DELETE FROM server_state WHERE server_id = ?", (server_id,))
    conn.execute("DELETE FROM uptime_checks WHERE server_id = ?", (server_id,))
    conn.execute("DELETE FROM incidents_logs WHERE server_id = ?", (server_id,))
    try:
        conn.execute("DELETE FROM uptime_daily WHERE server_id = ?", (server_id,))
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()
    return {"ok": True}


@app.post("/api/servers/{server_id}/check-now")
async def api_check_server_now(server_id: int):
    conn = get_db()
    row = conn.execute("SELECT * FROM servers WHERE id = ?", (server_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="السيرفر غير موجود")
    result = await check_and_update_single_server(dict(row), force_rbl=True)
    return {"ok": True, "result": result}


@app.get("/api/logs")
async def api_get_global_or_server_logs(
    server_id: str = "all", log_type: str = "mail", lines: int = 100
):
    """
    Fetch live logs either across ALL servers (server_id='all')
    or for a specific server_id.
    """
    conn = get_db()
    if server_id == "all" or not server_id:
        servers = [dict(r) for r in conn.execute("SELECT * FROM servers ORDER BY id ASC").fetchall()]
    else:
        row = conn.execute("SELECT * FROM servers WHERE id = ?", (int(server_id),)).fetchone()
        servers = [dict(row)] if row else []

    if not servers:
        conn.close()
        return {
            "ok": True,
            "source": "No Servers",
            "content": "No servers added yet / لم يتم إضافة سيرفرات بعد.",
        }

    if log_type == "incidents":
        if server_id == "all" or not server_id:
            inc_rows = conn.execute(
                "SELECT * FROM incidents_logs ORDER BY id DESC LIMIT ?", (int(lines),)
            ).fetchall()
        else:
            inc_rows = conn.execute(
                "SELECT * FROM incidents_logs WHERE server_id = ? ORDER BY id DESC LIMIT ?",
                (int(server_id), int(lines)),
            ).fetchall()
        conn.close()
        if not inc_rows:
            return {
                "ok": True,
                "source": "Monitor Database (All Servers)" if server_id == "all" else "Monitor Database",
                "content": "No incidents recorded yet / لا توجد انقطاعات أو تحذيرات مسجلة حتى الآن.",
            }
        formatted = "\n".join(
            f"[{r['created_at']}] [{r['server_name']} - {r['server_ip']}] [{r['severity']}] ({r['service_name']}) {r['title']} -> {r['details']}"
            for r in inc_rows
        )
        return {
            "ok": True,
            "source": "Monitor Database (All Servers)" if server_id == "all" else f"Server #{server_id}",
            "content": formatted,
        }

    conn.close()

    # Fetch remote SSH logs concurrently for all selected servers
    per_server_lines = max(20, int(lines) // max(1, len(servers))) if server_id == "all" else int(lines)
    tasks = [
        fetch_remote_logs(srv, log_type=log_type, lines=per_server_lines)
        for srv in servers
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    blocks = []
    for srv, res in zip(servers, results):
        header = f"======================================================================\n[SERVER: {srv['name']} | IP: {srv['ip']} | LOG: {log_type.upper()}]\n======================================================================"
        if isinstance(res, dict):
            blocks.append(f"{header}\n{res.get('content', '')}")
        else:
            blocks.append(f"{header}\nError: {res}")

    return {
        "ok": True,
        "source": "All Servers (SSH)" if server_id == "all" else f"{servers[0]['name']} ({servers[0]['ip']})",
        "content": "\n\n".join(blocks),
    }


@app.get("/api/servers/{server_id}/logs")
async def api_get_server_logs(server_id: int, log_type: str = "mail", lines: int = 120):
    return await api_get_global_or_server_logs(
        server_id=str(server_id), log_type=log_type, lines=lines
    )


@app.post("/api/servers/{server_id}/service-action")
async def api_service_action(server_id: int, payload: ServiceActionPayload):
    conn = get_db()
    row = conn.execute("SELECT * FROM servers WHERE id = ?", (server_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="السيرفر غير موجود")
    res = await execute_service_action(dict(row), payload.service_name, payload.action)
    if res.get("ok"):
        asyncio.create_task(check_and_update_single_server(dict(row)))
    return res


@app.get("/api/settings")
async def api_get_settings():
    return get_settings_dict()


@app.post("/api/settings")
async def api_save_settings(payload: SettingsPayload):
    conn = get_db()
    for k, v in payload.model_dump().items():
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (k, str(v)),
        )
    conn.commit()
    conn.close()
    return {"ok": True}


@app.post("/api/settings/detect-telegram-chat-id")
async def api_detect_telegram_chat_id(request: Request):
    import httpx

    body = await request.json()
    bot_token = (body.get("bot_token") or "").strip()
    if not bot_token:
        return {"ok": False, "message": "يرجى إدخال Bot Token أولاً"}

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"https://api.telegram.org/bot{bot_token}/getUpdates")
            data = resp.json()
            if not data.get("ok"):
                return {"ok": False, "message": "التوكن غير صحيح (Invalid Bot Token)"}
            results = data.get("result") or []
            for item in reversed(results):
                msg = item.get("message") or item.get("channel_post") or item.get("my_chat_member") or {}
                chat = msg.get("chat") or {}
                if chat.get("id"):
                    chat_id = str(chat["id"])
                    chat_title = chat.get("title") or chat.get("username") or chat.get("first_name") or ""
                    return {"ok": True, "chat_id": chat_id, "chat_name": chat_title}
            return {
                "ok": False,
                "message": "افتح البوت الجديد الذي أنشأته في تليجرام واضغط Start أو أرسل له أي كلمة ثم اضغط هنا مرة أخرى.",
            }
    except Exception as e:
        return {"ok": False, "message": f"خطأ في الاتصال بتليجرام: {e}"}


@app.post("/api/settings/test-alert")
async def api_test_alert():
    settings = get_settings_dict()
    res = await send_alerts(
        settings=settings,
        title="رسالة اختبار من نظام مراقبة السيرفرات (Test Alert)",
        server_name="Main Monitoring Server",
        server_ip="127.0.0.1",
        severity="RECOVERY",
        service_name="Apache / MySQL / Mail / Uptime Monitor",
        details="تم ربط التنبيهات بنجاح! سيصلك تنبيه فوري خلال 5 ثوانٍ عند توقف أي سيرفر أو خدمة أو وجود سبام، بالإضافة إلى تقرير شامل كل ساعة.",
        extra_stats={
            "فترة الفحص التلقائي": f"كل {settings.get('check_interval_sec', '5')} ثوانٍ",
            "التقرير الدوري": "كل ساعة (Load / RAM / Disk / Uptime)",
            "حالة النظام": "يعمل بكفاءة 100%",
        },
    )
    return {"ok": True, "results": res}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="0.0.0.0", port=8080, reload=False)
