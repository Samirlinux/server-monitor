import asyncio
import io
import json
import os
import re
import socket
import subprocess
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import httpx
import paramiko


# Default service ports monitored automatically as soon as an IP is added
DEFAULT_SERVICE_PORTS = [
    {"key": "apache_http", "name": "Apache / Web (HTTP)", "category": "web", "port": 80},
    {"key": "apache_https", "name": "Apache / Web (HTTPS)", "category": "web", "port": 443},
    {"key": "mysql", "name": "MySQL / MariaDB", "category": "db", "port": 3306},
    {"key": "postgres", "name": "PostgreSQL", "category": "db", "port": 5432},
    {"key": "smtp", "name": "Mail SMTP (25)", "category": "mail", "port": 25},
    {"key": "smtp_sub", "name": "Mail Submission (587)", "category": "mail", "port": 587},
    {"key": "smtps", "name": "Mail SMTPS (465)", "category": "mail", "port": 465},
    {"key": "ssh", "name": "SSH Server", "category": "system", "port": 22},
]

# Major DNSBL / Spam Blacklists for checking server IP reputation
DNSBL_PROVIDERS = [
    ("Spamhaus ZEN", "zen.spamhaus.org"),
    ("SpamCop", "bl.spamcop.net"),
    ("Barracuda RBL", "b.barracudacentral.org"),
    ("SORBS DNSBL", "dnsbl.sorbs.net"),
    ("PSBL Surriel", "psbl.surriel.com"),
]


async def check_tcp_port(ip: str, port: int, timeout: float = 2.2) -> Tuple[bool, float, str]:
    """
    Fast async TCP connect check with banner / HTTP status probe.
    Returns (is_up, latency_ms, banner_or_detail).
    """
    start = time.perf_counter()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, port), timeout=timeout
        )
        latency_ms = round((time.perf_counter() - start) * 1000, 1)
        detail = f"Port {port} Open ({latency_ms}ms)"

        # Optional quick protocol probe for richer detail
        try:
            if port == 80:
                req = f"HEAD / HTTP/1.1\r\nHost: {ip}\r\nConnection: close\r\n\r\n"
                writer.write(req.encode("utf-8"))
                await writer.drain()
                first_line = await asyncio.wait_for(reader.readline(), timeout=1.0)
                if first_line:
                    status_line = first_line.decode("utf-8", errors="ignore").strip()
                    detail = status_line or detail
            elif port in (22, 25, 587, 3306):
                banner = await asyncio.wait_for(reader.read(128), timeout=0.8)
                if banner:
                    clean_banner = re.sub(r"[^\x20-\x7E]", " ", banner.decode("utf-8", errors="ignore")).strip()
                    if clean_banner:
                        detail = clean_banner[:70]
        except Exception:
            pass

        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True, latency_ms, detail
    except asyncio.TimeoutError:
        return False, 0.0, f"Timeout on port {port} (>{timeout}s)"
    except Exception as e:
        return False, 0.0, f"Connection refused / unreachable ({type(e).__name__})"


def _check_single_dnsbl_sync(ip: str, provider_name: str, zone: str) -> Dict[str, Any]:
    """Reverse-IP lookup against a DNSBL zone."""
    try:
        # Validate IPv4 format
        parts = ip.strip().split(".")
        if len(parts) != 4 or not all(p.isdigit() for p in parts):
            return {"provider": provider_name, "zone": zone, "listed": False, "detail": "Skipped (non-IPv4)"}
        reversed_ip = ".".join(reversed(parts))
        query = f"{reversed_ip}.{zone}"
        socket.setdefaulttimeout(2.0)
        res = socket.gethostbyname(query)
        # Spamhaus may return 127.255.255.x if open resolver is blocked; only 127.0.0.x is an actual listing
        if res.startswith("127.255."):
            return {"provider": provider_name, "zone": zone, "listed": False, "detail": "Clean (Resolver limit)"}
        return {"provider": provider_name, "zone": zone, "listed": True, "detail": f"LISTED ({res})"}
    except socket.gaierror:
        return {"provider": provider_name, "zone": zone, "listed": False, "detail": "Clean"}
    except Exception as e:
        return {"provider": provider_name, "zone": zone, "listed": False, "detail": f"Check skipped ({type(e).__name__})"}


async def check_ip_blacklists(ip: str) -> Dict[str, Any]:
    """Check if the server IP is listed on global email spam blacklists (RBLs)."""
    loop = asyncio.get_running_loop()
    tasks = [
        loop.run_in_executor(None, _check_single_dnsbl_sync, ip, name, zone)
        for name, zone in DNSBL_PROVIDERS
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    clean_results = []
    listed_on = []
    for r in results:
        if isinstance(r, dict):
            clean_results.append(r)
            if r.get("listed"):
                listed_on.append(r["provider"])
    return {
        "is_blacklisted": len(listed_on) > 0,
        "listed_count": len(listed_on),
        "listed_providers": listed_on,
        "checks": clean_results,
        "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def _create_ssh_client(
    ip: str,
    ssh_port: int = 22,
    ssh_user: str = "root",
    ssh_password: Optional[str] = None,
    ssh_key: Optional[str] = None,
    timeout: float = 4.0,
) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    connect_kwargs: Dict[str, Any] = {
        "hostname": ip,
        "port": int(ssh_port or 22),
        "username": (ssh_user or "root").strip(),
        "timeout": timeout,
        "banner_timeout": timeout,
        "auth_timeout": timeout,
        "allow_agent": False,
        "look_for_keys": False,
    }
    if ssh_key and ssh_key.strip():
        key_stream = io.StringIO(ssh_key.strip())
        pkey = None
        for key_cls in (paramiko.RSAKey, paramiko.Ed25519Key, paramiko.ECDSAKey):
            try:
                key_stream.seek(0)
                pkey = key_cls.from_private_key(key_stream)
                break
            except Exception:
                continue
        if pkey:
            connect_kwargs["pkey"] = pkey
    if ssh_password and ssh_password.strip():
        connect_kwargs["password"] = ssh_password
    client.connect(**connect_kwargs)
    return client


INSPECTION_BASH_SCRIPT = r"""
export LC_ALL=C
echo "===UPTIME==="
uptime -p 2>/dev/null || uptime
cat /proc/uptime 2>/dev/null | awk '{print $1}'

echo "===LOADAVG==="
cat /proc/loadavg 2>/dev/null | awk '{print $1, $2, $3}'
nproc 2>/dev/null || grep -c ^processor /proc/cpuinfo 2>/dev/null || echo 1

echo "===RESOURCES==="
top -bn1 2>/dev/null | grep -E "Cpu\(s\)|%Cpu" | head -n1
free -m 2>/dev/null | awk '/Mem:/ {printf "%d %d %.1f\n", $3, $2, ($3/$2)*100}'
df -h / 2>/dev/null | awk 'NR==2 {print $3, $2, $5}'

echo "===SERVICES==="
for svc in apache2 httpd lshttpd nginx mysql mariadb postgresql redis redis-server postfix exim4 exim dovecot named bind9 fail2ban cpanel crond cron; do
    if systemctl list-unit-files "${svc}.service" >/dev/null 2>&1 || [ -f "/etc/init.d/${svc}" ] || pgrep -x "${svc}" >/dev/null 2>&1; then
        st=$(systemctl is-active "${svc}" 2>/dev/null || service "${svc}" status 2>/dev/null | grep -Eo "running|active|stopped|dead" | head -n1)
        echo "${svc}:${st}"
    fi
done

echo "===MAIL_QUEUE==="
if command -v exim >/dev/null 2>&1; then
    echo "MTA:exim"
    q_count=$(exim -bpc 2>/dev/null || echo 0)
    echo "QUEUE:${q_count}"
elif command -v postqueue >/dev/null 2>&1; then
    echo "MTA:postfix"
    q_count=$(postqueue -p 2>/dev/null | tail -n1 | grep -Eo '[0-9]+ Request' | awk '{print $1}')
    echo "QUEUE:${q_count:-0}"
elif command -v mailq >/dev/null 2>&1; then
    echo "MTA:sendmail"
    q_count=$(mailq 2>/dev/null | grep -c "^[A-F0-9]" || echo 0)
    echo "QUEUE:${q_count:-0}"
else
    echo "MTA:unknown"
    echo "QUEUE:0"
fi

echo "===MAIL_STATS==="
MAIL_LOG=""
for f in /var/log/mail.log /var/log/maillog /var/log/exim4/mainlog /var/log/exim_mainlog; do
    if [ -r "$f" ]; then
        MAIL_LOG="$f"
        break
    fi
done

if [ -n "$MAIL_LOG" ]; then
    echo "LOGFILE:$MAIL_LOG"
    tail -n 5000 "$MAIL_LOG" > /tmp/.mon_mail_sample 2>/dev/null
    sent_pf=$(grep -c "status=sent" /tmp/.mon_mail_sample 2>/dev/null || echo 0)
    sent_ex=$(grep -c "=>" /tmp/.mon_mail_sample 2>/dev/null || echo 0)
    SENT=$((sent_pf + sent_ex))
    DEFERRED=$(grep -ciE "status=deferred|defer \(" /tmp/.mon_mail_sample 2>/dev/null || echo 0)
    BOUNCED=$(grep -ciE "status=bounced|\*\* " /tmp/.mon_mail_sample 2>/dev/null || echo 0)
    REJECTED=$(grep -ciE "reject:|rejected|550 |554 |spam|blocked|blacklisted" /tmp/.mon_mail_sample 2>/dev/null || echo 0)
    AUTH_FAIL=$(grep -ciE "authentication failed|sasl_method.*failed|auth failed" /tmp/.mon_mail_sample 2>/dev/null || echo 0)
    echo "SENT:${SENT}"
    echo "DEFERRED:${DEFERRED}"
    echo "BOUNCED:${BOUNCED}"
    echo "REJECTED:${REJECTED}"
    echo "AUTH_FAIL:${AUTH_FAIL}"
    echo "===TOP_SENDERS==="
    grep -Eo "from=<[^>]+>|A=[^ ]+" /tmp/.mon_mail_sample 2>/dev/null | sort | uniq -c | sort -nr | head -n 5
    rm -f /tmp/.mon_mail_sample
elif command -v journalctl >/dev/null 2>&1; then
    echo "LOGFILE:journalctl"
    journalctl -u postfix -u exim4 --no-pager -n 2000 > /tmp/.mon_mail_sample 2>/dev/null
    SENT=$(grep -cE "status=sent|=>" /tmp/.mon_mail_sample 2>/dev/null || echo 0)
    DEFERRED=$(grep -ciE "status=deferred|defer" /tmp/.mon_mail_sample 2>/dev/null || echo 0)
    BOUNCED=$(grep -ciE "status=bounced" /tmp/.mon_mail_sample 2>/dev/null || echo 0)
    REJECTED=$(grep -ciE "reject:|550 |554 |spam|blocked" /tmp/.mon_mail_sample 2>/dev/null || echo 0)
    AUTH_FAIL=$(grep -ciE "authentication failed" /tmp/.mon_mail_sample 2>/dev/null || echo 0)
    echo "SENT:${SENT}"
    echo "DEFERRED:${DEFERRED}"
    echo "BOUNCED:${BOUNCED}"
    echo "REJECTED:${REJECTED}"
    echo "AUTH_FAIL:${AUTH_FAIL}"
    echo "===TOP_SENDERS==="
    grep -Eo "from=<[^>]+>" /tmp/.mon_mail_sample 2>/dev/null | sort | uniq -c | sort -nr | head -n 5
    rm -f /tmp/.mon_mail_sample
else
    echo "SENT:0"
    echo "DEFERRED:0"
    echo "BOUNCED:0"
    echo "REJECTED:0"
    echo "AUTH_FAIL:0"
fi
"""


def _get_local_ips() -> set:
    ips = {"127.0.0.1", "localhost", "::1"}
    try:
        hostname = socket.gethostname()
        ips.add(hostname)
        for info in socket.getaddrinfo(hostname, None):
            ips.add(info[4][0])
    except Exception:
        pass
    try:
        out = subprocess.check_output(["hostname", "-I"], timeout=1.5).decode("utf-8", errors="ignore")
        for part in out.split():
            ips.add(part.strip())
    except Exception:
        pass
    try:
        pub = subprocess.check_output(["curl", "-s", "--max-time", "2", "https://api.ipify.org"], timeout=2.5).decode("utf-8", errors="ignore").strip()
        if pub:
            ips.add(pub)
    except Exception:
        pass
    return ips


LOCAL_HOST_IPS = _get_local_ips()


def is_local_server_ip(ip: str) -> bool:
    return os.name != "nt" and ip.strip() in LOCAL_HOST_IPS


def _local_deep_inspect_sync() -> Dict[str, Any]:
    """Run inspection directly on the local Linux server without needing SSH."""
    res = subprocess.run(
        ["bash", "-c", INSPECTION_BASH_SCRIPT],
        capture_output=True,
        text=True,
        timeout=5,
    )
    return _parse_ssh_inspection_output(res.stdout)


def _ssh_deep_inspect_sync(server: Dict[str, Any]) -> Dict[str, Any]:
    """
    Runs a single combined bash inspection command over SSH so it executes in <1 second.
    """
    client = _create_ssh_client(
        ip=server["ip"],
        ssh_port=server.get("ssh_port", 22),
        ssh_user=server.get("ssh_user", "root"),
        ssh_password=server.get("ssh_password"),
        ssh_key=server.get("ssh_key"),
        timeout=4.0,
    )
    try:
        stdin, stdout, stderr = client.exec_command(INSPECTION_BASH_SCRIPT, timeout=5)
        raw_out = stdout.read().decode("utf-8", errors="ignore")
        return _parse_ssh_inspection_output(raw_out)
    finally:
        client.close()


def _parse_ssh_inspection_output(raw: str) -> Dict[str, Any]:
    sections: Dict[str, List[str]] = {}
    current = None
    for line in raw.splitlines():
        line_str = line.strip()
        if line_str.startswith("===") and line_str.endswith("==="):
            current = line_str.strip("=")
            sections[current] = []
        elif current is not None and line_str:
            sections[current].append(line_str)

    # 1. Uptime
    uptime_lines = sections.get("UPTIME", [])
    uptime_str = uptime_lines[0] if uptime_lines else "Online"
    uptime_seconds = 0.0
    if len(uptime_lines) > 1:
        try:
            uptime_seconds = float(uptime_lines[1])
            days = int(uptime_seconds // 86400)
            hours = int((uptime_seconds % 86400) // 3600)
            mins = int((uptime_seconds % 3600) // 60)
            if days > 0:
                uptime_str = f"{days}d {hours}h {mins}m"
            elif hours > 0:
                uptime_str = f"{hours}h {mins}m"
            else:
                uptime_str = f"{mins}m"
        except ValueError:
            pass

    # 1b. Load Average (1m, 5m, 15m)
    load_lines = sections.get("LOADAVG", [])
    load_str = "0.00 / 0.00 / 0.00"
    cpu_cores = 1
    if load_lines:
        parts = load_lines[0].split()
        if len(parts) >= 3:
            load_str = f"{parts[0]} / {parts[1]} / {parts[2]}"
        if len(load_lines) > 1:
            try:
                cpu_cores = max(1, int(load_lines[1].strip()))
            except ValueError:
                cpu_cores = 1

    # 2. Resources
    res_lines = sections.get("RESOURCES", [])
    cpu_pct = 0.0
    ram_pct = 0.0
    ram_detail = ""
    disk_pct = 0.0
    disk_detail = ""
    for line in res_lines:
        if "Cpu" in line or "id" in line:
            m_idle = re.search(r"([0-9.]+)\s*%?\s*id", line)
            if m_idle:
                cpu_pct = round(max(0.0, 100.0 - float(m_idle.group(1))), 1)
        elif re.match(r"^\d+\s+\d+\s+[0-9.]+$", line):
            parts = line.split()
            ram_detail = f"{parts[0]}MB / {parts[1]}MB"
            ram_pct = float(parts[2])
        elif "%" in line:
            parts = line.split()
            if len(parts) >= 3:
                disk_detail = f"{parts[0]} / {parts[1]}"
                try:
                    disk_pct = float(parts[2].replace("%", ""))
                except ValueError:
                    pass

    # 3. Services via systemd
    ssh_services: Dict[str, str] = {}
    for line in sections.get("SERVICES", []):
        if ":" in line:
            k, v = line.split(":", 1)
            ssh_services[k.strip()] = v.strip()

    # 4. Mail Queue & Stats
    mta = "unknown"
    queue_count = 0
    for line in sections.get("MAIL_QUEUE", []):
        if line.startswith("MTA:"):
            mta = line.split(":", 1)[1].strip()
        elif line.startswith("QUEUE:"):
            try:
                queue_count = int(re.sub(r"[^0-9]", "", line.split(":", 1)[1]) or "0")
            except ValueError:
                queue_count = 0

    mail_stats = {
        "mta": mta,
        "queue_count": queue_count,
        "sent_count": 0,
        "deferred_count": 0,
        "bounced_count": 0,
        "rejected_spam_count": 0,
        "auth_failures": 0,
        "log_file": "",
        "top_senders": [],
    }
    for line in sections.get("MAIL_STATS", []):
        if ":" in line:
            k, v = line.split(":", 1)
            v = v.strip()
            if k == "LOGFILE":
                mail_stats["log_file"] = v
            elif k == "SENT":
                mail_stats["sent_count"] = int(re.sub(r"[^0-9]", "", v) or "0")
            elif k == "DEFERRED":
                mail_stats["deferred_count"] = int(re.sub(r"[^0-9]", "", v) or "0")
            elif k == "BOUNCED":
                mail_stats["bounced_count"] = int(re.sub(r"[^0-9]", "", v) or "0")
            elif k == "REJECTED":
                mail_stats["rejected_spam_count"] = int(re.sub(r"[^0-9]", "", v) or "0")
            elif k == "AUTH_FAIL":
                mail_stats["auth_failures"] = int(re.sub(r"[^0-9]", "", v) or "0")

    for line in sections.get("TOP_SENDERS", []):
        mail_stats["top_senders"].append(line)

    return {
        "ssh_connected": True,
        "uptime_str": uptime_str,
        "uptime_seconds": uptime_seconds,
        "load_str": load_str,
        "cpu_cores": cpu_cores,
        "cpu_usage": cpu_pct,
        "ram_usage": ram_pct,
        "ram_detail": ram_detail,
        "disk_usage": disk_pct,
        "disk_detail": disk_detail,
        "ssh_services": ssh_services,
        "mail_stats": mail_stats,
    }


async def inspect_server(
    server: Dict[str, Any],
    run_rbl_check: bool = False,
    previous_rbl: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Full inspection of a single server within the 5-second monitoring cycle.
    Combines fast TCP port checks (Apache, DB, Mail, SSH) + optional SSH/Local deep metrics + RBL spam check.
    """
    ip = server["ip"].strip()
    configured_ports = server.get("check_ports")
    if isinstance(configured_ports, str):
        try:
            configured_ports = json.loads(configured_ports)
        except Exception:
            configured_ports = DEFAULT_SERVICE_PORTS
    if not configured_ports:
        configured_ports = DEFAULT_SERVICE_PORTS

    # 1. Run all TCP port checks concurrently
    port_tasks = [
        check_tcp_port(ip, int(item["port"]), timeout=2.2)
        for item in configured_ports
    ]

    # 2. If SSH credentials are provided (or if this is the local host), run deep inspection in parallel
    has_ssh_creds = bool(
        (server.get("ssh_password") and server.get("ssh_password").strip())
        or (server.get("ssh_key") and server.get("ssh_key").strip())
    )
    loop = asyncio.get_running_loop()
    if has_ssh_creds:
        ssh_future = loop.run_in_executor(None, _ssh_deep_inspect_sync, server)
    elif is_local_server_ip(ip):
        ssh_future = loop.run_in_executor(None, _local_deep_inspect_sync)
    else:
        ssh_future = None

    port_results = await asyncio.gather(*port_tasks, return_exceptions=True)

    ssh_data: Dict[str, Any] = {"ssh_connected": False}
    if ssh_future is not None:
        try:
            ssh_data = await asyncio.wait_for(ssh_future, timeout=4.5)
        except Exception as e:
            if is_local_server_ip(ip):
                try:
                    ssh_data = await loop.run_in_executor(None, _local_deep_inspect_sync)
                except Exception:
                    ssh_data = {"ssh_connected": False, "ssh_error": str(e)}
            else:
                ssh_data = {"ssh_connected": False, "ssh_error": str(e)}

    # 3. Build unified services status list
    services_list: List[Dict[str, Any]] = []
    latencies: List[float] = []
    ssh_svc_map: Dict[str, str] = ssh_data.get("ssh_services", {})

    for idx, item in enumerate(configured_ports):
        res = port_results[idx]
        if isinstance(res, tuple):
            is_up, lat_ms, detail = res
        else:
            is_up, lat_ms, detail = False, 0.0, str(res)

        if is_up and lat_ms > 0:
            latencies.append(lat_ms)

        services_list.append(
            {
                "key": item["key"],
                "name": item["name"],
                "category": item.get("category", "other"),
                "port": int(item["port"]),
                "status": "UP" if is_up else "DOWN",
                "latency_ms": lat_ms,
                "detail": detail,
                "source": "TCP Port",
            }
        )

    # Enrich or append systemd services discovered via SSH
    systemd_labels = {
        "apache2": ("Apache Web Server (systemd)", "web"),
        "httpd": ("Apache httpd (systemd)", "web"),
        "lshttpd": ("LiteSpeed / OpenLiteSpeed (systemd)", "web"),
        "nginx": ("Nginx Web Server (systemd)", "web"),
        "mysql": ("MySQL Database (systemd)", "db"),
        "mariadb": ("MariaDB Database (systemd)", "db"),
        "postgresql": ("PostgreSQL Database (systemd)", "db"),
        "redis": ("Redis Cache (systemd)", "db"),
        "redis-server": ("Redis Server (systemd)", "db"),
        "postfix": ("Postfix Mail MTA (systemd)", "mail"),
        "exim4": ("Exim4 Mail MTA (systemd)", "mail"),
        "exim": ("Exim Mail MTA (systemd)", "mail"),
        "dovecot": ("Dovecot IMAP/POP3 (systemd)", "mail"),
        "named": ("BIND DNS Server (systemd)", "system"),
        "bind9": ("BIND9 DNS Server (systemd)", "system"),
        "cpanel": ("cPanel / WHM Service (systemd)", "system"),
        "fail2ban": ("Fail2ban Security (systemd)", "system"),
        "cron": ("Cron Scheduler (systemd)", "system"),
        "crond": ("Crond Scheduler (systemd)", "system"),
    }
    for svc_key, svc_state in ssh_svc_map.items():
        is_active = svc_state in ("active", "running")
        label, cat = systemd_labels.get(svc_key, (f"{svc_key} (systemd)", "system"))
        services_list.append(
            {
                "key": f"systemd_{svc_key}",
                "systemd_unit": svc_key,
                "name": label,
                "category": cat,
                "port": 0,
                "status": "UP" if is_active else "DOWN",
                "latency_ms": 0.0,
                "detail": f"systemctl: {svc_state}",
                "source": "SSH / systemd",
            }
        )

    # 4. Determine overall server reachability & status
    any_port_up = any(s["status"] == "UP" for s in services_list) or ssh_data.get("ssh_connected", False)
    avg_ping_ms = round(sum(latencies) / len(latencies), 1) if latencies else 0.0

    # 5. RBL / Blacklist check (runs periodically or on first check)
    if run_rbl_check or not previous_rbl:
        rbl_status = await check_ip_blacklists(ip)
    else:
        rbl_status = previous_rbl

    # 6. Mail & Spam assessment
    mail_port_up = any(
        s["status"] == "UP" and s.get("category") == "mail" for s in services_list
    )
    mail_stats = ssh_data.get("mail_stats") or {
        "mta": "SMTP Port Active" if mail_port_up else "Not Detected",
        "queue_count": 0,
        "sent_count": 0,
        "deferred_count": 0,
        "bounced_count": 0,
        "rejected_spam_count": 0,
        "auth_failures": 0,
        "log_file": "Requires SSH for log parsing",
        "top_senders": [],
    }
    mail_stats["smtp_reachable"] = mail_port_up

    # Evaluate spam / sending warning flags
    spam_warnings: List[str] = []
    queue_limit = int(server.get("mail_queue_limit") or 100)
    spam_threshold = int(server.get("spam_threshold") or 20)

    if rbl_status.get("is_blacklisted"):
        providers_str = ", ".join(rbl_status.get("listed_providers", []))
        spam_warnings.append(f"الـ IP مدرج في قوائم الحظر (Blacklist): {providers_str}")

    if mail_stats.get("queue_count", 0) >= queue_limit:
        spam_warnings.append(
            f"طابور الرسائل (Mail Queue) مرتفع جداً: {mail_stats['queue_count']} رسالة بالانتظار"
        )

    if mail_stats.get("bounced_count", 0) >= spam_threshold:
        spam_warnings.append(
            f"معدل ارتداد الرسائل (Bounced) مرتفع: {mail_stats['bounced_count']} رسالة مرتدة"
        )

    if mail_stats.get("rejected_spam_count", 0) >= spam_threshold:
        spam_warnings.append(
            f"تم رصد رسائل مرفوضة أو سبام (Rejected/Spam): {mail_stats['rejected_spam_count']} عملية"
        )

    if mail_stats.get("auth_failures", 0) >= 25:
        spam_warnings.append(
            f"محاولات اختراق SMTP (SASL Auth Failures): {mail_stats['auth_failures']} محاولة فاشلة"
        )

    mail_stats["has_spam_issue"] = len(spam_warnings) > 0
    mail_stats["spam_warnings"] = spam_warnings
    mail_stats["load_str"] = ssh_data.get("load_str", "")
    mail_stats["cpu_cores"] = ssh_data.get("cpu_cores", 0)

    overall_status = "UP" if any_port_up else "DOWN"

    return {
        "status": overall_status,
        "ping_ms": avg_ping_ms,
        "ssh_connected": ssh_data.get("ssh_connected", False),
        "ssh_error": ssh_data.get("ssh_error", ""),
        "uptime_str": ssh_data.get("uptime_str") or ("Online (TCP Active)" if overall_status == "UP" else "Offline (DOWN)"),
        "load_str": ssh_data.get("load_str", ""),
        "cpu_cores": ssh_data.get("cpu_cores", 0),
        "cpu_usage": ssh_data.get("cpu_usage", 0.0),
        "ram_usage": ssh_data.get("ram_usage", 0.0),
        "ram_detail": ssh_data.get("ram_detail", ""),
        "disk_usage": ssh_data.get("disk_usage", 0.0),
        "disk_detail": ssh_data.get("disk_detail", ""),
        "services": services_list,
        "mail_stats": mail_stats,
        "rbl_status": rbl_status,
        "last_checked": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def _fetch_remote_logs_sync(
    server: Dict[str, Any], log_type: str = "mail", lines: int = 120
) -> Dict[str, Any]:
    """Fetch real-time logs over SSH (or locally if local host) for Mail, Apache/Web, Database, or System."""
    has_ssh = bool(
        (server.get("ssh_password") and server.get("ssh_password").strip())
        or (server.get("ssh_key") and server.get("ssh_key").strip())
    )
    is_local = is_local_server_ip(server["ip"])

    if not has_ssh and not is_local:
        return {
            "ok": False,
            "source": "local_only",
            "content": (
                "⚠️ لعرض ملفات اللوج الداخلية مباشرة من السيرفر (/var/log/mail.log, apache2/error.log, mysql/error.log)، "
                "يرجى إضافة بيانات الـ SSH (اسم المستخدم وكلمة المرور أو المفتاح) من زر تعديل السيرفر.\n\n"
                "💡 بدون SSH، يمكنك متابعة 'سجل انقطاعات السيرفر والتنبيهات' في التبويب المخصص."
            ),
        }

    lines = max(20, min(int(lines or 120), 500))
    cmd_map = {
        "mail": (
            f"for f in /var/log/mail.log /var/log/maillog /var/log/exim4/mainlog /var/log/exim_mainlog; do "
            f"if [ -r \"$f\" ]; then echo \"==> $f <==\"; tail -n {lines} \"$f\"; exit 0; fi; done; "
            f"journalctl -u postfix -u exim4 -u dovecot --no-pager -n {lines} 2>/dev/null || echo 'No mail logs found'"
        ),
        "apache": (
            f"for f in /var/log/apache2/error.log /var/log/httpd/error_log /var/log/nginx/error.log /var/log/apache2/access.log; do "
            f"if [ -r \"$f\" ]; then echo \"==> $f <==\"; tail -n {lines} \"$f\"; fi; done | tail -n {lines}; "
            f"journalctl -u apache2 -u httpd -u nginx --no-pager -n 40 2>/dev/null"
        ),
        "db": (
            f"for f in /var/log/mysql/error.log /var/log/mysqld.log /var/log/mariadb/mariadb.log /var/log/postgresql/postgresql-*.log; do "
            f"if [ -r \"$f\" ]; then echo \"==> $f <==\"; tail -n {lines} \"$f\"; exit 0; fi; done; "
            f"journalctl -u mysql -u mariadb -u postgresql --no-pager -n {lines} 2>/dev/null"
        ),
        "system": (
            f"for f in /var/log/syslog /var/log/messages /var/log/auth.log /var/log/secure; do "
            f"if [ -r \"$f\" ]; then echo \"==> $f <==\"; tail -n {lines // 2} \"$f\"; fi; done; "
            f"journalctl --no-pager -n 50 2>/dev/null"
        ),
        "queue": (
            "echo '=== Mail Queue Details ==='; "
            "if command -v postqueue >/dev/null 2>&1; then postqueue -p | head -n 150; "
            "elif command -v exim >/dev/null 2>&1; then exim -bp | head -n 150; "
            "else mailq | head -n 150; fi"
        ),
    }
    cmd = cmd_map.get(log_type, cmd_map["system"])

    if not has_ssh and is_local:
        res = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=6)
        out = (res.stdout + res.stderr).strip()
        return {
            "ok": True,
            "source": f"Local Server ({server['ip']})",
            "content": out or "No log entries found.",
        }

    client = _create_ssh_client(
        ip=server["ip"],
        ssh_port=server.get("ssh_port", 22),
        ssh_user=server.get("ssh_user", "root"),
        ssh_password=server.get("ssh_password"),
        ssh_key=server.get("ssh_key"),
        timeout=5.0,
    )
    try:
        _, stdout, stderr = client.exec_command(cmd, timeout=6)
        out = stdout.read().decode("utf-8", errors="ignore").strip()
        err = stderr.read().decode("utf-8", errors="ignore").strip()
        return {
            "ok": True,
            "source": f"SSH ({server['ip']})",
            "content": out or err or "لا توجد سجلات حالياً.",
        }
    finally:
        client.close()


async def fetch_remote_logs(
    server: Dict[str, Any], log_type: str = "mail", lines: int = 120
) -> Dict[str, Any]:
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(None, _fetch_remote_logs_sync, server, log_type, lines)
    except Exception as e:
        return {
            "ok": False,
            "source": "ssh_error",
            "content": f"❌ تعذر الاتصال بالسيرفر عبر SSH لجلب اللوجات:\n{e}",
        }


def _service_action_sync(server: Dict[str, Any], service_name: str, action: str) -> Dict[str, Any]:
    allowed_actions = {"restart", "start", "stop", "flush_queue", "clear_deferred"}
    if action not in allowed_actions:
        return {"ok": False, "message": "عملية غير مسموحة"}

    client = _create_ssh_client(
        ip=server["ip"],
        ssh_port=server.get("ssh_port", 22),
        ssh_user=server.get("ssh_user", "root"),
        ssh_password=server.get("ssh_password"),
        ssh_key=server.get("ssh_key"),
        timeout=5.0,
    )
    try:
        if action == "flush_queue":
            cmd = "postqueue -f 2>&1 || exim -qff 2>&1 || sendmail -q 2>&1"
        elif action == "clear_deferred":
            cmd = "postsuper -d ALL deferred 2>&1 || exiqgrep -z -i | xargs -r exim -Mrm 2>&1"
        else:
            clean_svc = re.sub(r"[^a-zA-Z0-9_.-]", "", service_name.replace("systemd_", ""))
            cmd = f"systemctl {action} {clean_svc} 2>&1 || service {clean_svc} {action} 2>&1"

        _, stdout, stderr = client.exec_command(cmd, timeout=8)
        out = (stdout.read() + stderr.read()).decode("utf-8", errors="ignore").strip()
        return {"ok": True, "message": out or f"تم تنفيذ العملية ({action}) بنجاح ✅"}
    finally:
        client.close()


async def execute_service_action(
    server: Dict[str, Any], service_name: str, action: str = "restart"
) -> Dict[str, Any]:
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(None, _service_action_sync, server, service_name, action)
    except Exception as e:
        return {"ok": False, "message": f"خطأ أثناء تنفيذ الأمر عبر SSH: {e}"}


async def send_alerts(
    settings: Dict[str, Any],
    title: str,
    server_name: str,
    server_ip: str,
    severity: str,
    service_name: str,
    details: str,
    extra_stats: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Sends instant (<1s) alerts to Telegram and Slack when a server or service changes state
    or when Spam / Blacklist is detected.
    """
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    icon = "🚨" if severity == "CRITICAL" else ("⚠️" if severity == "WARNING" else "✅")

    stats_block = ""
    if extra_stats:
        stats_lines = [f"• {k}: {v}" for k, v in extra_stats.items()]
        stats_block = "\n📊 *إحصائيات السيرفر الحالية:*\n" + "\n".join(stats_lines)

    message_text = (
        f"{icon} *{title}*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🖥️ *السيرفر:* `{server_name}`\n"
        f"🌐 *الـ IP:* `{server_ip}`\n"
        f"⚙️ *الخدمة / الفحص:* `{service_name}`\n"
        f"📝 *التفاصيل:* {details}"
        f"{stats_block}\n"
        f"🕒 *الوقت:* `{now_str}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"⚡ *Developed by Eng. Samir Abo El-Khair | Version 1.0*"
    )

    results = {"telegram": None, "slack": None}

    async with httpx.AsyncClient(timeout=6.0) as client:
        # 1. Telegram Notification
        bot_token = (settings.get("telegram_bot_token") or "").strip()
        if bot_token.lower().startswith("bot") and ":" in bot_token:
            bot_token = bot_token[3:].strip()
        chat_id = (settings.get("telegram_chat_id") or "").strip()
        tg_enabled = str(settings.get("telegram_enabled", "0")) == "1" or bool(bot_token)

        if tg_enabled and bot_token:
            if not chat_id:
                try:
                    upd = await client.get(f"https://api.telegram.org/bot{bot_token}/getUpdates")
                    for item in reversed(upd.json().get("result") or []):
                        msg_obj = item.get("message") or item.get("channel_post") or {}
                        cid = (msg_obj.get("chat") or {}).get("id")
                        if cid:
                            chat_id = str(cid)
                            break
                except Exception:
                    pass

            if not chat_id:
                results["telegram"] = "Missing Chat ID (أرسل /start للبوت أو أدخل الـ Chat ID)"
            else:
                tg_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
                try:
                    resp = await client.post(
                        tg_url,
                        json={
                            "chat_id": chat_id,
                            "text": message_text,
                            "parse_mode": "Markdown",
                        },
                    )
                    if resp.status_code == 200:
                        results["telegram"] = True
                    else:
                        err_desc = resp.json().get("description", resp.text)
                        results["telegram"] = f"Telegram Error ({resp.status_code}): {err_desc}"
                except Exception as e:
                    results["telegram"] = f"Connection Error: {e}"

        # 2. Slack Notification
        webhook_url = (settings.get("slack_webhook_url") or "").strip()
        slack_enabled = str(settings.get("slack_enabled", "0")) == "1" or bool(webhook_url)
        if slack_enabled and webhook_url:
            try:
                resp = await client.post(
                    webhook_url,
                    json={"text": message_text},
                )
                if resp.status_code in (200, 204):
                    results["slack"] = True
                else:
                    results["slack"] = f"Slack Error ({resp.status_code}): {resp.text[:80]}"
            except Exception as e:
                results["slack"] = f"Connection Error: {e}"

    return results
