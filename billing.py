"""
billing.py — نظام الحسابات اليومية، مستحقات العملاء، وإصدار الفواتير
Developed by Eng. Samir Abo El-Khair | Version 3.5

الصفحات:
  /billing               لوحة الحسابات اليومية + مستحقات العملاء (الفلوس اللي بره) + الفواتير
  /billing/invoice/{id}  عرض وطباعة فاتورة احترافية (PDF / Print)
"""
import csv
import io
import json
from datetime import datetime
from typing import List, Optional

from fastapi import HTTPException
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel

NO_CACHE = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}


class PaymentIn(BaseModel):
    client_name: str
    service_title: str = ""
    amount: float
    currency: str = "EGP"
    payment_method: str = "Cash / Vodafone Cash"
    payment_date: str = ""
    notes: str = ""
    debt_id: Optional[int] = None


class DebtIn(BaseModel):
    client_name: str
    phone: str = ""
    service_title: str
    total_amount: float
    paid_amount: float = 0.0
    currency: str = "EGP"
    due_date: str = ""
    notes: str = ""


class DebtCollectIn(BaseModel):
    amount: float
    payment_method: str = "Cash / Instapay"
    notes: str = ""


class InvoiceItem(BaseModel):
    description: str
    qty: float = 1.0
    price: float = 0.0


class InvoiceIn(BaseModel):
    client_name: str
    client_phone: str = ""
    currency: str = "EGP"
    status: str = "unpaid"  # paid | partial | unpaid
    paid_amount: float = 0.0
    issue_date: str = ""
    due_date: str = ""
    notes: str = ""
    items: List[InvoiceItem] = []


def register(app, get_db):
    conn = get_db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS daily_income (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_name TEXT NOT NULL,
            service_title TEXT DEFAULT '',
            amount REAL NOT NULL DEFAULT 0,
            currency TEXT DEFAULT 'EGP',
            payment_method TEXT DEFAULT '',
            payment_date TEXT NOT NULL,
            notes TEXT DEFAULT '',
            debt_id INTEGER DEFAULT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS client_debts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_name TEXT NOT NULL,
            phone TEXT DEFAULT '',
            service_title TEXT NOT NULL,
            total_amount REAL NOT NULL DEFAULT 0,
            paid_amount REAL NOT NULL DEFAULT 0,
            currency TEXT DEFAULT 'EGP',
            due_date TEXT DEFAULT '',
            status TEXT DEFAULT 'open',
            notes TEXT DEFAULT '',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS invoices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_no TEXT NOT NULL,
            client_name TEXT NOT NULL,
            client_phone TEXT DEFAULT '',
            currency TEXT DEFAULT 'EGP',
            status TEXT DEFAULT 'unpaid',
            total_amount REAL NOT NULL DEFAULT 0,
            paid_amount REAL NOT NULL DEFAULT 0,
            issue_date TEXT NOT NULL,
            due_date TEXT DEFAULT '',
            notes TEXT DEFAULT '',
            items_json TEXT DEFAULT '[]',
            created_at TEXT NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()

    # ───────────────────────── HTML Pages ─────────────────────────
    @app.get("/billing", response_class=HTMLResponse)
    async def billing_page():
        return HTMLResponse(BILLING_HTML, headers=NO_CACHE)

    @app.get("/billing/invoice/{inv_id}", response_class=HTMLResponse)
    async def invoice_print_page(inv_id: int):
        conn = get_db()
        row = conn.execute("SELECT * FROM invoices WHERE id = ?", (inv_id,)).fetchone()
        conn.close()
        if not row:
            raise HTTPException(status_code=404, detail="الفاتورة غير موجودة")
        inv = dict(row)
        try:
            inv["items"] = json.loads(inv.get("items_json") or "[]")
        except Exception:
            inv["items"] = []
        inv["remaining"] = max(0.0, round((inv["total_amount"] or 0) - (inv["paid_amount"] or 0), 2))
        html = INVOICE_PRINT_HTML.replace("__INVOICE_JSON__", json.dumps(inv, ensure_ascii=False))
        return HTMLResponse(html, headers=NO_CACHE)

    # ───────────────────────── Overview & Summary API ─────────────────────────
    @app.get("/api/billing/overview")
    def billing_overview(date_from: str = "", date_to: str = "", client: str = ""):
        conn = get_db()
        today = datetime.now().strftime("%Y-%m-%d")
        month_prefix = today[:7]

        # 1. Daily Income
        inc_sql = "SELECT * FROM daily_income WHERE 1=1"
        inc_params = []
        if date_from:
            inc_sql += " AND payment_date >= ?"
            inc_params.append(date_from)
        if date_to:
            inc_sql += " AND payment_date <= ?"
            inc_params.append(date_to)
        if client:
            inc_sql += " AND client_name LIKE ?"
            inc_params.append(f"%{client}%")
        inc_sql += " ORDER BY payment_date DESC, id DESC"
        income_rows = [dict(r) for r in conn.execute(inc_sql, inc_params).fetchall()]

        all_income = [dict(r) for r in conn.execute("SELECT amount, payment_date, currency FROM daily_income").fetchall()]
        today_income = sum(r["amount"] for r in all_income if r["payment_date"] == today)
        month_income = sum(r["amount"] for r in all_income if (r["payment_date"] or "").startswith(month_prefix))
        filtered_income = sum(r["amount"] for r in income_rows)

        # 2. Client Debts (الفلوس اللي ليك عند الناس)
        debt_sql = "SELECT * FROM client_debts WHERE 1=1"
        debt_params = []
        if client:
            debt_sql += " AND client_name LIKE ?"
            debt_params.append(f"%{client}%")
        debt_sql += " ORDER BY CASE WHEN status='paid' THEN 1 ELSE 0 END, due_date ASC, id DESC"
        debts = []
        total_remaining_debt = 0.0
        overdue_debts_count = 0
        for r in conn.execute(debt_sql, debt_params).fetchall():
            d = dict(r)
            rem = max(0.0, round((d["total_amount"] or 0) - (d["paid_amount"] or 0), 2))
            d["remaining"] = rem
            d["is_paid"] = rem <= 0.001
            d["overdue"] = bool(not d["is_paid"] and d["due_date"] and d["due_date"] < today)
            if not d["is_paid"]:
                total_remaining_debt += rem
                if d["overdue"]:
                    overdue_debts_count += 1
            debts.append(d)

        # 3. Invoices
        inv_sql = "SELECT * FROM invoices WHERE 1=1"
        inv_params = []
        if client:
            inv_sql += " AND client_name LIKE ?"
            inv_params.append(f"%{client}%")
        inv_sql += " ORDER BY id DESC"
        invoices = []
        for r in conn.execute(inv_sql, inv_params).fetchall():
            inv = dict(r)
            inv["remaining"] = max(0.0, round((inv["total_amount"] or 0) - (inv["paid_amount"] or 0), 2))
            invoices.append(inv)

        # Client autocomplete list
        clients_set = set()
        for tbl in ("daily_income", "client_debts", "invoices"):
            for r in conn.execute(f"SELECT DISTINCT client_name FROM {tbl} WHERE client_name != ''").fetchall():
                clients_set.add(r[0])

        conn.close()
        return {
            "today": today,
            "summary": {
                "today_income": round(today_income, 2),
                "month_income": round(month_income, 2),
                "filtered_income": round(filtered_income, 2),
                "total_remaining_debt": round(total_remaining_debt, 2),
                "overdue_debts_count": overdue_debts_count,
                "open_debts_count": sum(1 for d in debts if not d["is_paid"]),
                "invoices_count": len(invoices),
            },
            "income": income_rows,
            "debts": debts,
            "invoices": invoices,
            "clients": sorted(clients_set),
        }

    # ───────────────────────── Daily Income CRUD ─────────────────────────
    @app.post("/api/billing/income")
    def add_income(p: PaymentIn):
        name = p.client_name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="اسم العميل أو الجهة مطلوب")
        if p.amount <= 0:
            raise HTTPException(status_code=400, detail="المبلغ يجب أن يكون أكبر من صفر")
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        pay_date = p.payment_date.strip() or datetime.now().strftime("%Y-%m-%d")
        conn = get_db()
        conn.execute(
            """INSERT INTO daily_income (client_name, service_title, amount, currency, payment_method, payment_date, notes, debt_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (name, p.service_title.strip(), round(p.amount, 2), p.currency.strip() or "EGP",
             p.payment_method.strip(), pay_date, p.notes.strip(), p.debt_id, now),
        )
        conn.commit()
        conn.close()
        return {"ok": True}

    @app.delete("/api/billing/income/{inc_id}")
    def delete_income(inc_id: int):
        conn = get_db()
        conn.execute("DELETE FROM daily_income WHERE id = ?", (inc_id,))
        conn.commit()
        conn.close()
        return {"ok": True}

    # ───────────────────────── Client Debts (المستحقات والديون) ─────────────────────────
    @app.post("/api/billing/debts")
    def add_debt(p: DebtIn):
        name = p.client_name.strip()
        srv = p.service_title.strip()
        if not name or not srv:
            raise HTTPException(status_code=400, detail="اسم العميل وسبب المديونية مطلوبان")
        if p.total_amount <= 0:
            raise HTTPException(status_code=400, detail="إجمالي المبلغ يجب أن يكون أكبر من صفر")
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        today = datetime.now().strftime("%Y-%m-%d")
        paid = max(0.0, round(p.paid_amount, 2))
        total = round(p.total_amount, 2)
        status = "paid" if paid >= total else "open"
        conn = get_db()
        cur = conn.execute(
            """INSERT INTO client_debts (client_name, phone, service_title, total_amount, paid_amount, currency, due_date, status, notes, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (name, p.phone.strip(), srv, total, paid, p.currency.strip() or "EGP",
             p.due_date.strip(), status, p.notes.strip(), now),
        )
        debt_id = cur.lastrowid
        # لو دفع مقدم، يتسجل تلقائياً في الوارد اليومي
        if paid > 0:
            conn.execute(
                """INSERT INTO daily_income (client_name, service_title, amount, currency, payment_method, payment_date, notes, debt_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (name, f"دفعة من: {srv}", paid, p.currency.strip() or "EGP", "دفعة مقدم", today, p.notes.strip(), debt_id, now),
            )
        conn.commit()
        conn.close()
        return {"ok": True, "id": debt_id}

    @app.post("/api/billing/debts/{debt_id}/collect")
    def collect_debt(debt_id: int, p: DebtCollectIn):
        if p.amount <= 0:
            raise HTTPException(status_code=400, detail="مبلغ التحصيل يجب أن يكون أكبر من صفر")
        conn = get_db()
        row = conn.execute("SELECT * FROM client_debts WHERE id = ?", (debt_id,)).fetchone()
        if not row:
            conn.close()
            raise HTTPException(status_code=404, detail="السجل غير موجود")
        new_paid = round((row["paid_amount"] or 0) + p.amount, 2)
        status = "paid" if new_paid >= (row["total_amount"] or 0) else "open"
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        today = datetime.now().strftime("%Y-%m-%d")
        conn.execute(
            "UPDATE client_debts SET paid_amount = ?, status = ? WHERE id = ?",
            (new_paid, status, debt_id),
        )
        # تسجيل الفلوس اللي دخلت تلقائياً في سجل الوارد اليومي
        conn.execute(
            """INSERT INTO daily_income (client_name, service_title, amount, currency, payment_method, payment_date, notes, debt_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (row["client_name"], f"تحصيل مستحقات: {row['service_title']}", round(p.amount, 2),
             row["currency"] or "EGP", p.payment_method.strip() or "تحصيل", today, p.notes.strip(), debt_id, now),
        )
        conn.commit()
        conn.close()
        return {"ok": True}

    @app.delete("/api/billing/debts/{debt_id}")
    def delete_debt(debt_id: int):
        conn = get_db()
        conn.execute("DELETE FROM client_debts WHERE id = ?", (debt_id,))
        conn.commit()
        conn.close()
        return {"ok": True}

    # ───────────────────────── Invoices (الفواتير) ─────────────────────────
    @app.post("/api/billing/invoices")
    def create_invoice(p: InvoiceIn):
        name = p.client_name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="اسم العميل مطلوب")
        items_clean = []
        total = 0.0
        for it in p.items:
            desc = it.description.strip()
            if desc:
                line_total = round((it.qty or 1) * (it.price or 0), 2)
                total += line_total
                items_clean.append({"description": desc, "qty": it.qty or 1, "price": it.price or 0, "total": line_total})
        if not items_clean:
            raise HTTPException(status_code=400, detail="أضف بنداً واحداً على الأقل في الفاتورة")

        total = round(total, 2)
        paid = round(p.paid_amount or 0.0, 2)
        if p.status == "paid":
            paid = total
        elif paid >= total and total > 0:
            p.status = "paid"
        elif paid > 0:
            p.status = "partial"
        else:
            p.status = "unpaid"

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        issue_date = p.issue_date.strip() or datetime.now().strftime("%Y-%m-%d")
        conn = get_db()
        last_id = (conn.execute("SELECT MAX(id) FROM invoices").fetchone()[0] or 0) + 1
        inv_no = f"INV-{datetime.now().strftime('%Y%m')}-{last_id:04d}"

        cur = conn.execute(
            """INSERT INTO invoices (invoice_no, client_name, client_phone, currency, status, total_amount, paid_amount, issue_date, due_date, notes, items_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (inv_no, name, p.client_phone.strip(), p.currency.strip() or "EGP", p.status,
             total, paid, issue_date, p.due_date.strip(), p.notes.strip(),
             json.dumps(items_clean, ensure_ascii=False), now),
        )
        inv_id = cur.lastrowid

        # لو الفاتورة مدفوعة أو مدفوع جزء منها، يسجل في الوارد اليومي
        if paid > 0:
            conn.execute(
                """INSERT INTO daily_income (client_name, service_title, amount, currency, payment_method, payment_date, notes, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (name, f"فاتورة {inv_no}", paid, p.currency.strip() or "EGP", "فاتورة", issue_date, p.notes.strip(), now),
            )
        # لو متبقي فلوس على العميل في الفاتورة، يسجلها تلقائياً في مستحقات العملاء
        remaining = round(total - paid, 2)
        if remaining > 0:
            summary_srv = f"فاتورة {inv_no} ({items_clean[0]['description']})"
            conn.execute(
                """INSERT INTO client_debts (client_name, phone, service_title, total_amount, paid_amount, currency, due_date, status, notes, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)""",
                (name, p.client_phone.strip(), summary_srv, total, paid, p.currency.strip() or "EGP",
                 p.due_date.strip(), p.notes.strip(), now),
            )

        conn.commit()
        conn.close()
        return {"ok": True, "id": inv_id, "invoice_no": inv_no}

    @app.delete("/api/billing/invoices/{inv_id}")
    def delete_invoice(inv_id: int):
        conn = get_db()
        conn.execute("DELETE FROM invoices WHERE id = ?", (inv_id,))
        conn.commit()
        conn.close()
        return {"ok": True}

    # ───────────────────────── Export CSV ─────────────────────────
    @app.get("/api/billing/export-income.csv")
    def export_income_csv():
        conn = get_db()
        rows = conn.execute("SELECT * FROM daily_income ORDER BY payment_date DESC, id DESC").fetchall()
        conn.close()
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["التاريخ", "العميل", "البيان / الخدمة", "المبلغ", "العملة", "طريقة الدفع", "ملاحظات"])
        for r in rows:
            w.writerow([r["payment_date"], r["client_name"], r["service_title"], r["amount"], r["currency"], r["payment_method"], r["notes"]])
        data = ("\ufeff" + buf.getvalue()).encode("utf-8")
        return Response(content=data, media_type="text/csv; charset=utf-8", headers={"Content-Disposition": "attachment; filename=daily-income.csv"})


BILLING_HTML = r"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>الحسابات والفواتير والفلوس | Eng. Samir Abo El-Khair — Version 3.5</title>
<style>
  :root{--bg:#f4faf6;--card:#fff;--text:#112a1a;--muted:#52735e;--border:#cce3d4;--green:#15803d;--green-light:#dcfce7;--amber:#d97706;--red:#dc2626;--blue:#1d4ed8}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);font-family:system-ui,-apple-system,"Segoe UI",Tahoma,sans-serif}
  .header{background:#fff;border-bottom:2px solid var(--green);padding:14px 20px;position:sticky;top:0;z-index:20}
  .header-in{max-width:1200px;margin:0 auto;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px}
  .title-box h1{margin:0;font-size:20px;color:#064e3b;display:flex;align-items:center;gap:8px}
  .ver{font-size:12px;background:var(--green-light);color:#065f46;border:1px solid #86efac;padding:2px 8px;border-radius:6px;font-family:monospace}
  .nav-links{display:flex;gap:8px;flex-wrap:wrap}
  .nav-links a{text-decoration:none;padding:7px 12px;border-radius:6px;border:1px solid var(--green);color:var(--green);font-weight:700;font-size:13px;background:#fff}
  .nav-links a:hover{background:var(--green-light)}
  .wrap{max-width:1200px;margin:0 auto;padding:20px 16px}
  .stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px;margin-bottom:20px}
  .stat{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:14px 16px;box-shadow:0 1px 2px rgba(0,0,0,.03)}
  .stat small{color:var(--muted);font-weight:700;font-size:12px;display:block}
  .stat b{font-size:24px;display:block;margin-top:4px}
  .tabs{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px}
  .tab{padding:10px 18px;border-radius:8px;border:1px solid var(--border);background:#fff;color:var(--text);font-weight:700;cursor:pointer;font-size:14px}
  .tab.active{background:var(--green);color:#fff;border-color:var(--green)}
  .card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:16px;margin-bottom:16px}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px}
  label{display:block;font-size:12px;font-weight:700;color:var(--muted);margin-bottom:4px}
  input,select,textarea{width:100%;padding:8px 10px;border:1px solid var(--border);border-radius:6px;font:inherit;background:#fff}
  button,.btn{border:0;border-radius:6px;padding:9px 15px;font:inherit;font-weight:700;font-size:13px;cursor:pointer;color:#fff;background:var(--green);text-decoration:none;display:inline-flex;align-items:center;gap:5px}
  button.red{background:var(--red)} button.blue{background:var(--blue)} button.amber{background:var(--amber)} button.gray{background:#64748b}
  button.sm,.btn.sm{padding:5px 10px;font-size:12px}
  table{width:100%;border-collapse:collapse;font-size:13.5px}
  th,td{padding:10px 8px;border-bottom:1px solid var(--border);text-align:right;vertical-align:middle}
  th{background:#f0fdf4;color:#065f46;font-weight:800;font-size:12.5px}
  .badge{padding:3px 9px;border-radius:999px;font-size:11.5px;font-weight:700;display:inline-block}
  .b-green{background:#dcfce7;color:#166534}
  .b-red{background:#fee2e2;color:#991b1b}
  .b-amber{background:#fef3c7;color:#92400e}
  .sec-pane{display:none} .sec-pane.active{display:block}
  .inv-item-row{display:grid;grid-template-columns:2fr 90px 120px 40px;gap:8px;margin-bottom:8px;align-items:center}
  .foot{text-align:center;color:var(--muted);font-size:12px;margin-top:30px;padding-top:14px;border-top:1px solid var(--border)}
</style>
</head>
<body>

<header class="header">
  <div class="header-in">
    <div class="title-box">
      <h1>💰 نظام الحسابات اليومية، المستحقات، والفواتير <span class="ver">Version 3.5</span></h1>
      <div style="font-size:12px;color:var(--muted);font-weight:600;margin-top:2px">Developed by <strong>Eng. Samir Abo El-Khair</strong> — سجل الفلوس اللي دخلت، الفلوس اللي ليك عند العملاء، واطبع فواتيرك فوراً</div>
    </div>
    <div class="nav-links">
      <a href="/">🖥️ لوحة السيرفرات</a>
      <a href="/tasks">📋 التاسكات</a>
      <a href="/status">🌐 صفحة الـ Uptime</a>
      <a href="/api/billing/export-income.csv">⬇️ تصدير الحسابات Excel</a>
    </div>
  </div>
</header>

<div class="wrap">
  <datalist id="clientsList"></datalist>

  <!-- Summary Cards -->
  <div class="stats">
    <div class="stat">
      <small>💵 الفلوس اللي دخلت النهاردة</small>
      <b id="sumToday" style="color:var(--green)">0 EGP</b>
    </div>
    <div class="stat">
      <small>📅 إجمالي اللي دخل الشهر ده</small>
      <b id="sumMonth" style="color:#065f46">0 EGP</b>
    </div>
    <div class="stat" style="border-color:#fca5a5;background:#fffafa">
      <small>📌 الفلوس اللي ليك بره عند الناس (مستحقات)</small>
      <b id="sumDebt" style="color:var(--red)">0 EGP</b>
    </div>
    <div class="stat">
      <small>🧾 عدد الفواتير المصدرة</small>
      <b id="sumInv" style="color:var(--blue)">0</b>
    </div>
  </div>

  <!-- Filter by Client -->
  <div class="card" style="padding:12px 16px;display:flex;flex-wrap:wrap;gap:10px;align-items:flex-end">
    <div style="flex:1;min-width:200px">
      <label>🔍 بحث باسم العميل / الشركة</label>
      <input id="fltClient" list="clientsList" placeholder="اكتب اسم العميل لفلترة حساباته..." oninput="loadAll()">
    </div>
    <div>
      <label>من تاريخ</label>
      <input type="date" id="fltFrom" onchange="loadAll()">
    </div>
    <div>
      <label>إلى تاريخ</label>
      <input type="date" id="fltTo" onchange="loadAll()">
    </div>
    <button class="gray" onclick="clearFilters()">مسح الفلتر</button>
  </div>

  <!-- Navigation Tabs -->
  <div class="tabs">
    <button class="tab active" id="btnTab-debts" onclick="switchTab('debts')">📌 الفلوس اللي ليا عند الناس (المستحقات)</button>
    <button class="tab" id="btnTab-income" onclick="switchTab('income')">💵 تسجيل الفلوس اللي دخلت يومياً (الوارد)</button>
    <button class="tab" id="btnTab-invoices" onclick="switchTab('invoices')">🧾 نظام الفواتير (إنشاء وطباعة فاتورة)</button>
  </div>

  <!-- TAB 1: DEBTS (الفلوس اللي بره عند الناس) -->
  <div class="sec-pane active" id="pane-debts">
    <div class="card">
      <h3 style="margin-top:0;color:#991b1b">➕ تسجيل فلوس ليك عند عميل (شغل أو سيرفر أو اشتراك لسه مدفعش أو دفع جزء)</h3>
      <div class="grid">
        <div><label>اسم العميل / الشركة *</label><input id="dClient" list="clientsList" placeholder="مثال: شركة ميجا / أ. أحمد"></div>
        <div><label>رقم الموبايل / واتساب</label><input id="dPhone" placeholder="010xxxxxxxx"></div>
        <div><label>مقابل إيه (الخدمة / السيرفر) *</label><input id="dService" placeholder="تجديد سيرفر / إعداد ميل / دعم فني"></div>
        <div><label>إجمالي المبلغ المطلوب *</label><input type="number" step="0.01" id="dTotal" placeholder="1000"></div>
        <div><label>دفع منه كام دلوقتي؟ (لو مدفعش خليه 0)</label><input type="number" step="0.01" id="dPaid" value="0"></div>
        <div><label>العملة</label>
          <select id="dCurr"><option value="EGP">جنيه (EGP)</option><option value="USD">دولار (USD)</option><option value="SAR">ريال (SAR)</option><option value="EUR">يورو (EUR)</option></select>
        </div>
        <div><label>ميعاد السداد المتفق عليه</label><input type="date" id="dDue"></div>
        <div><label>ملاحظات</label><input id="dNotes" placeholder="أي تفاصيل إضافية"></div>
      </div>
      <div style="margin-top:12px">
        <button onclick="addDebt()">💾 حفظ في سجل المستحقات</button>
      </div>
    </div>

    <div class="card" style="overflow-x:auto">
      <h3 style="margin-top:0">📋 قايمة الفلوس اللي ليك عند الناس</h3>
      <table>
        <thead>
          <tr>
            <th>العميل</th>
            <th>الخدمة / البيان</th>
            <th>الإجمالي</th>
            <th>المدفوع</th>
            <th>المتبقي ليك</th>
            <th>ميعاد السداد</th>
            <th>الحالة</th>
            <th>إجراء (تحصيل / حذف)</th>
          </tr>
        </thead>
        <tbody id="debtsBody"></tbody>
      </table>
    </div>
  </div>

  <!-- TAB 2: DAILY INCOME (الفلوس اللي دخلت يومياً) -->
  <div class="sec-pane" id="pane-income">
    <div class="card">
      <h3 style="margin-top:0;color:#065f46">➕ تسجيل فلوس دخلت النهاردة (وارد يومي)</h3>
      <div class="grid">
        <div><label>اسم العميل / الجهة *</label><input id="incClient" list="clientsList" placeholder="اسم العميل"></div>
        <div><label>مقابل إيه (الخدمة)</label><input id="incService" placeholder="إدارة سيرفر / فحص سبام / دفعة"></div>
        <div><label>المبلغ اللي دخل *</label><input type="number" step="0.01" id="incAmount" placeholder="500"></div>
        <div><label>العملة</label>
          <select id="incCurr"><option value="EGP">جنيه (EGP)</option><option value="USD">دولار (USD)</option><option value="SAR">ريال (SAR)</option><option value="EUR">يورو (EUR)</option></select>
        </div>
        <div><label>طريقة الدفع</label>
          <select id="incMethod">
            <option value="Instapay / إنستا باي">Instapay / إنستا باي</option>
            <option value="Vodafone Cash / محفظة">Vodafone Cash / محفظة</option>
            <option value="تحويل بنكي">تحويل بنكي</option>
            <option value="كاش">كاش</option>
            <option value="PayPal / Stripe / USDT">PayPal / Stripe / USDT</option>
          </select>
        </div>
        <div><label>التاريخ</label><input type="date" id="incDate"></div>
        <div><label>ملاحظات</label><input id="incNotes" placeholder="رقم التحويل أو ملاحظة"></div>
      </div>
      <div style="margin-top:12px">
        <button onclick="addIncome()">💵 تسجيل المبلغ في وارد اليوم</button>
      </div>
    </div>

    <div class="card" style="overflow-x:auto">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;flex-wrap:wrap">
        <h3 style="margin:0">📊 سجل الفلوس اللي دخلت</h3>
        <div style="font-weight:800;color:var(--green)">إجمالي المعروض: <span id="lblFilteredIncome">0</span></div>
      </div>
      <table>
        <thead>
          <tr>
            <th>التاريخ</th>
            <th>العميل</th>
            <th>الخدمة / البيان</th>
            <th>المبلغ</th>
            <th>طريقة الدفع</th>
            <th>ملاحظات</th>
            <th>حذف</th>
          </tr>
        </thead>
        <tbody id="incomeBody"></tbody>
      </table>
    </div>
  </div>

  <!-- TAB 3: INVOICES (نظام الفواتير) -->
  <div class="sec-pane" id="pane-invoices">
    <div class="card">
      <h3 style="margin-top:0;color:var(--blue)">🧾 إنشاء فاتورة جديدة للعميل (تطبع PDF أو تبعتهاله فوراً)</h3>
      <div class="grid" style="margin-bottom:12px">
        <div><label>اسم العميل / الشركة *</label><input id="invClient" list="clientsList" placeholder="اسم العميل أو الشركة"></div>
        <div><label>رقم التليفون / الإيميل</label><input id="invPhone" placeholder="010xxxxxxx"></div>
        <div><label>العملة</label>
          <select id="invCurr"><option value="EGP">EGP (جنيه)</option><option value="USD">USD (دولار)</option><option value="SAR">SAR (ريال)</option><option value="EUR">EUR (يورو)</option></select>
        </div>
        <div><label>حالة الفاتورة</label>
          <select id="invStatus" onchange="toggleInvPaid()">
            <option value="unpaid">غير مدفوعة (تتسجل في المستحقات تلقائياً)</option>
            <option value="paid">مدفوعة بالكامل (تتسجل في الوارد اليومي تلقائياً)</option>
            <option value="partial">مدفوع جزء والباقي آجل</option>
          </select>
        </div>
        <div id="invPaidBox" style="display:none"><label>المبلغ المدفوع حالياً</label><input type="number" step="0.01" id="invPaid" value="0"></div>
        <div><label>تاريخ الفاتورة</label><input type="date" id="invIssue"></div>
        <div><label>تاريخ الاستحقاق</label><input type="date" id="invDue"></div>
        <div><label>ملاحظات تظهر أسفل الفاتورة</label><input id="invNotes" placeholder="مثال: الدفع عبر إنستا باي أو فودافون كاش"></div>
      </div>

      <div style="background:#f8fafc;border:1px solid var(--border);border-radius:8px;padding:12px;margin-bottom:12px">
        <div style="font-weight:800;margin-bottom:8px;font-size:13px">بنود الفاتورة (الخدمات / السيرفرات):</div>
        <div id="invItemsWrap"></div>
        <button type="button" class="blue sm" onclick="addInvItemRow()">+ إضافة بند آخر</button>
      </div>

      <button onclick="createInvoice()">🧾 حفظ وإصدار الفاتورة</button>
    </div>

    <div class="card" style="overflow-x:auto">
      <h3 style="margin-top:0">🗂️ سجل الفواتير</h3>
      <table>
        <thead>
          <tr>
            <th>رقم الفاتورة</th>
            <th>التاريخ</th>
            <th>العميل</th>
            <th>الإجمالي</th>
            <th>المدفوع</th>
            <th>المتبقي</th>
            <th>الحالة</th>
            <th>عرض وطباعة / حذف</th>
          </tr>
        </thead>
        <tbody id="invoicesBody"></tbody>
      </table>
    </div>
  </div>

  <div class="foot">
    Developed by <strong>Eng. Samir Abo El-Khair</strong> — <span class="ver">Version 3.5</span>
  </div>
</div>

<script>
function esc(s){var d=document.createElement('div');d.textContent=s==null?'':String(s);return d.innerHTML;}

function switchTab(t){
  ['debts','income','invoices'].forEach(function(k){
    document.getElementById('btnTab-'+k).className = 'tab' + (k===t ? ' active' : '');
    document.getElementById('pane-'+k).className = 'sec-pane' + (k===t ? ' active' : '');
  });
}

function toggleInvPaid(){
  var st = document.getElementById('invStatus').value;
  document.getElementById('invPaidBox').style.display = (st === 'partial') ? 'block' : 'none';
}

function addInvItemRow(desc, qty, price){
  var wrap = document.getElementById('invItemsWrap');
  var row = document.createElement('div');
  row.className = 'inv-item-row';
  row.innerHTML =
    '<input class="it-desc" placeholder="وصف الخدمة / السيرفر (مثال: إدارة سيرفر لينكس شهري)" value="'+esc(desc||'')+'">' +
    '<input type="number" step="1" class="it-qty" placeholder="العدد" value="'+(qty||1)+'">' +
    '<input type="number" step="0.01" class="it-price" placeholder="السعر" value="'+(price||'')+'">' +
    '<button type="button" class="red sm" onclick="this.parentElement.remove()">✕</button>';
  wrap.appendChild(row);
}

function clearFilters(){
  document.getElementById('fltClient').value = '';
  document.getElementById('fltFrom').value = '';
  document.getElementById('fltTo').value = '';
  loadAll();
}

function api(url, opts){
  return fetch(url, opts).then(function(r){
    return r.json().then(function(j){ if(!r.ok) throw new Error(j.detail || 'خطأ'); return j; });
  });
}

function loadAll(){
  var c = encodeURIComponent(document.getElementById('fltClient').value || '');
  var f = encodeURIComponent(document.getElementById('fltFrom').value || '');
  var t = encodeURIComponent(document.getElementById('fltTo').value || '');
  api('/api/billing/overview?client='+c+'&date_from='+f+'&date_to='+t).then(function(d){
    document.getElementById('sumToday').textContent = d.summary.today_income.toLocaleString() + ' EGP';
    document.getElementById('sumMonth').textContent = d.summary.month_income.toLocaleString() + ' EGP';
    document.getElementById('sumDebt').textContent = d.summary.total_remaining_debt.toLocaleString() + ' EGP (' + d.summary.open_debts_count + ' عميل)';
    document.getElementById('sumInv').textContent = d.summary.invoices_count;
    document.getElementById('lblFilteredIncome').textContent = d.summary.filtered_income.toLocaleString() + ' EGP';

    if(!document.getElementById('incDate').value) document.getElementById('incDate').value = d.today;
    if(!document.getElementById('invIssue').value) document.getElementById('invIssue').value = d.today;

    document.getElementById('clientsList').innerHTML = (d.clients||[]).map(function(n){return '<option value="'+esc(n)+'">';}).join('');

    // Render Debts
    document.getElementById('debtsBody').innerHTML = (d.debts||[]).map(function(x){
      var stBadge = x.is_paid
        ? '<span class="badge b-green">✅ خالص</span>'
        : (x.overdue ? '<span class="badge b-red">⚠️ متأخر</span>' : '<span class="badge b-amber">⏳ عليه باقي</span>');
      var collectBtn = x.is_paid ? '' : '<button class="sm" onclick="collectDebt('+x.id+','+x.remaining+',\''+esc(x.client_name)+'\')">💵 تحصيل دفعة</button> ';
      return '<tr>' +
        '<td><b>'+esc(x.client_name)+'</b>'+(x.phone?'<br><small style="color:var(--muted)">'+esc(x.phone)+'</small>':'')+'</td>' +
        '<td>'+esc(x.service_title)+(x.notes?'<br><small style="color:var(--muted)">'+esc(x.notes)+'</small>':'')+'</td>' +
        '<td>'+x.total_amount.toLocaleString()+' '+esc(x.currency)+'</td>' +
        '<td style="color:var(--green);font-weight:700">'+x.paid_amount.toLocaleString()+' '+esc(x.currency)+'</td>' +
        '<td style="color:'+(x.is_paid?'var(--green)':'var(--red)')+';font-weight:800">'+x.remaining.toLocaleString()+' '+esc(x.currency)+'</td>' +
        '<td>'+esc(x.due_date || '—')+'</td>' +
        '<td>'+stBadge+'</td>' +
        '<td>'+collectBtn+'<button class="red sm" onclick="delDebt('+x.id+')">حذف</button></td>' +
      '</tr>';
    }).join('') || '<tr><td colspan="8" style="text-align:center;padding:24px;color:var(--muted)">لا توجد مستحقات مسجلة حالياً</td></tr>';

    // Render Income
    document.getElementById('incomeBody').innerHTML = (d.income||[]).map(function(x){
      return '<tr>' +
        '<td>'+esc(x.payment_date)+'</td>' +
        '<td><b>'+esc(x.client_name)+'</b></td>' +
        '<td>'+esc(x.service_title || '—')+'</td>' +
        '<td style="color:var(--green);font-weight:800">+'+x.amount.toLocaleString()+' '+esc(x.currency)+'</td>' +
        '<td>'+esc(x.payment_method || '—')+'</td>' +
        '<td>'+esc(x.notes || '—')+'</td>' +
        '<td><button class="red sm" onclick="delIncome('+x.id+')">حذف</button></td>' +
      '</tr>';
    }).join('') || '<tr><td colspan="7" style="text-align:center;padding:24px;color:var(--muted)">لم يتم تسجيل وارد بعد</td></tr>';

    // Render Invoices
    document.getElementById('invoicesBody').innerHTML = (d.invoices||[]).map(function(x){
      var b = x.status==='paid' ? '<span class="badge b-green">مدفوعة</span>' : (x.status==='partial' ? '<span class="badge b-amber">مدفوع جزء</span>' : '<span class="badge b-red">غير مدفوعة</span>');
      return '<tr>' +
        '<td style="font-family:monospace;font-weight:700">'+esc(x.invoice_no)+'</td>' +
        '<td>'+esc(x.issue_date)+'</td>' +
        '<td><b>'+esc(x.client_name)+'</b></td>' +
        '<td><b>'+x.total_amount.toLocaleString()+' '+esc(x.currency)+'</b></td>' +
        '<td style="color:var(--green)">'+x.paid_amount.toLocaleString()+'</td>' +
        '<td style="color:var(--red);font-weight:700">'+x.remaining.toLocaleString()+'</td>' +
        '<td>'+b+'</td>' +
        '<td><a class="btn blue sm" href="/billing/invoice/'+x.id+'" target="_blank">🖨️ فتح وطباعة الفاتورة</a> <button class="red sm" onclick="delInvoice('+x.id+')">حذف</button></td>' +
      '</tr>';
    }).join('') || '<tr><td colspan="8" style="text-align:center;padding:24px;color:var(--muted)">لا توجد فواتير مصدرة بعد</td></tr>';
  });
}

function addDebt(){
  var body = {
    client_name: document.getElementById('dClient').value,
    phone: document.getElementById('dPhone').value,
    service_title: document.getElementById('dService').value,
    total_amount: parseFloat(document.getElementById('dTotal').value || '0'),
    paid_amount: parseFloat(document.getElementById('dPaid').value || '0'),
    currency: document.getElementById('dCurr').value,
    due_date: document.getElementById('dDue').value,
    notes: document.getElementById('dNotes').value
  };
  api('/api/billing/debts', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)})
    .then(function(){
      document.getElementById('dService').value='';
      document.getElementById('dTotal').value='';
      document.getElementById('dPaid').value='0';
      document.getElementById('dNotes').value='';
      loadAll();
    }).catch(function(e){ alert(e.message); });
}

function collectDebt(id, rem, client){
  var val = prompt('تحصيل مبلغ من ('+client+') — المتبقي عليه '+rem+':\nاكتب المبلغ اللي دفعه دلوقتي:', rem);
  if(val === null) return;
  var amt = parseFloat(val);
  if(!amt || amt <= 0) return alert('اكتب مبلغ صحيح');
  var method = prompt('طريقة الدفع (إنستا باي / فودافون كاش / كاش / تحويل):', 'Instapay / فودافون كاش') || 'تحصيل';
  api('/api/billing/debts/'+id+'/collect', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({amount: amt, payment_method: method})
  }).then(loadAll).catch(function(e){ alert(e.message); });
}

function delDebt(id){
  if(confirm('حذف هذا السجل؟')) api('/api/billing/debts/'+id, {method:'DELETE'}).then(loadAll);
}

function addIncome(){
  var body = {
    client_name: document.getElementById('incClient').value,
    service_title: document.getElementById('incService').value,
    amount: parseFloat(document.getElementById('incAmount').value || '0'),
    currency: document.getElementById('incCurr').value,
    payment_method: document.getElementById('incMethod').value,
    payment_date: document.getElementById('incDate').value,
    notes: document.getElementById('incNotes').value
  };
  api('/api/billing/income', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)})
    .then(function(){
      document.getElementById('incService').value='';
      document.getElementById('incAmount').value='';
      document.getElementById('incNotes').value='';
      loadAll();
    }).catch(function(e){ alert(e.message); });
}

function delIncome(id){
  if(confirm('حذف هذه العملية من الوارد؟')) api('/api/billing/income/'+id, {method:'DELETE'}).then(loadAll);
}

function createInvoice(){
  var items = [];
  document.querySelectorAll('#invItemsWrap .inv-item-row').forEach(function(r){
    var desc = r.querySelector('.it-desc').value.trim();
    var qty = parseFloat(r.querySelector('.it-qty').value || '1');
    var price = parseFloat(r.querySelector('.it-price').value || '0');
    if(desc) items.push({description: desc, qty: qty, price: price});
  });
  var body = {
    client_name: document.getElementById('invClient').value,
    client_phone: document.getElementById('invPhone').value,
    currency: document.getElementById('invCurr').value,
    status: document.getElementById('invStatus').value,
    paid_amount: parseFloat(document.getElementById('invPaid').value || '0'),
    issue_date: document.getElementById('invIssue').value,
    due_date: document.getElementById('invDue').value,
    notes: document.getElementById('invNotes').value,
    items: items
  };
  api('/api/billing/invoices', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)})
    .then(function(res){
      loadAll();
      window.open('/billing/invoice/' + res.id, '_blank');
    }).catch(function(e){ alert(e.message); });
}

function delInvoice(id){
  if(confirm('حذف الفاتورة؟')) api('/api/billing/invoices/'+id, {method:'DELETE'}).then(loadAll);
}

addInvItemRow('', 1, '');
loadAll();
</script>
</body>
</html>
"""


INVOICE_PRINT_HTML = r"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>فاتورة | Eng. Samir Abo El-Khair</title>
<style>
  body{margin:0;background:#f1f5f9;font-family:system-ui,-apple-system,"Segoe UI",Tahoma,sans-serif;color:#0f172a}
  .bar{max-width:820px;margin:20px auto 0;display:flex;justify-content:space-between;padding:0 16px}
  .btn{border:0;border-radius:8px;padding:10px 18px;background:#15803d;color:#fff;font-weight:700;cursor:pointer;text-decoration:none;font-size:14px}
  .sheet{max-width:820px;margin:14px auto 40px;background:#fff;border:1px solid #cbd5e1;border-radius:12px;padding:36px;box-shadow:0 4px 20px rgba(0,0,0,.05)}
  .top{display:flex;justify-content:space-between;align-items:flex-start;border-bottom:3px solid #15803d;padding-bottom:18px;margin-bottom:24px;flex-wrap:wrap;gap:16px}
  .brand h1{margin:0;color:#064e3b;font-size:24px}
  .brand p{margin:4px 0 0;color:#475569;font-size:13px;font-weight:600}
  .inv-meta{text-align:left;direction:ltr}
  .inv-no{font-size:22px;font-weight:800;color:#15803d;font-family:monospace}
  .meta-grid{display:grid;grid-template-columns:1fr 1fr;gap:16px;background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:16px;margin-bottom:24px}
  table{width:100%;border-collapse:collapse;margin-bottom:24px}
  th,td{padding:12px 10px;border-bottom:1px solid #e2e8f0;text-align:right}
  th{background:#064e3b;color:#fff;font-size:13px}
  .totals{max-width:320px;margin-right:auto;border:1px solid #cbd5e1;border-radius:8px;overflow:hidden}
  .tot-row{display:flex;justify-content:space-between;padding:10px 14px;border-bottom:1px solid #e2e8f0;font-size:14px}
  .tot-row.final{background:#f0fdf4;font-weight:800;font-size:16px;color:#064e3b;border-bottom:0}
  .foot{margin-top:32px;padding-top:16px;border-top:1px dashed #cbd5e1;display:flex;justify-content:space-between;font-size:12px;color:#64748b;flex-wrap:wrap}
  @media print{
    body{background:#fff}
    .no-print{display:none !important}
    .sheet{box-shadow:none;border:0;margin:0;padding:10px}
  }
</style>
</head>
<body>
<div class="bar no-print">
  <button class="btn" onclick="window.print()">🖨️ طباعة الفاتورة / حفظ PDF</button>
  <a class="btn" style="background:#475569" href="/billing">← العودة للحسابات</a>
</div>

<div class="sheet" id="invSheet"></div>

<script>
var inv = __INVOICE_JSON__;
function esc(s){var d=document.createElement('div');d.textContent=s==null?'':String(s);return d.innerHTML;}
var stMap = {paid:'✅ مدفوعة بالكامل (PAID)', partial:'⏳ مدفوع جزء (PARTIAL)', unpaid:'📌 غير مدفوعة (UNPAID)'};
var rows = (inv.items||[]).map(function(it, i){
  return '<tr>' +
    '<td>'+(i+1)+'</td>' +
    '<td><b>'+esc(it.description)+'</b></td>' +
    '<td>'+it.qty+'</td>' +
    '<td>'+Number(it.price).toLocaleString()+' '+esc(inv.currency)+'</td>' +
    '<td><b>'+Number(it.total).toLocaleString()+' '+esc(inv.currency)+'</b></td>' +
  '</tr>';
}).join('');

document.getElementById('invSheet').innerHTML =
  '<div class="top">' +
    '<div class="brand">' +
      '<h1>Eng. Samir Abo El-Khair</h1>' +
      '<p>Linux Servers Administration, Cloud, Uptime & Mail Infrastructure</p>' +
    '</div>' +
    '<div class="inv-meta">' +
      '<div class="inv-no">' + esc(inv.invoice_no) + '</div>' +
      '<div style="font-size:13px;font-weight:700;margin-top:4px">' + esc(stMap[inv.status] || inv.status) + '</div>' +
    '</div>' +
  '</div>' +
  '<div class="meta-grid">' +
    '<div><div style="color:#64748b;font-size:12px">مطلوب من العميل / الشركة:</div><div style="font-size:17px;font-weight:800;margin-top:4px">' + esc(inv.client_name) + '</div>' +
      (inv.client_phone ? '<div style="font-size:13px;color:#475569;margin-top:2px">' + esc(inv.client_phone) + '</div>' : '') +
    '</div>' +
    '<div>' +
      '<div style="font-size:13px"><b>تاريخ الإصدار:</b> ' + esc(inv.issue_date) + '</div>' +
      (inv.due_date ? '<div style="font-size:13px;margin-top:4px"><b>تاريخ الاستحقاق:</b> ' + esc(inv.due_date) + '</div>' : '') +
      '<div style="font-size:13px;margin-top:4px"><b>العملة:</b> ' + esc(inv.currency) + '</div>' +
    '</div>' +
  '</div>' +
  '<table><thead><tr><th>#</th><th>البيان / الخدمة</th><th>الكمية</th><th>سعر الوحدة</th><th>الإجمالي</th></tr></thead><tbody>' + rows + '</tbody></table>' +
  '<div class="totals">' +
    '<div class="tot-row"><span>إجمالي الفاتورة:</span><b>' + Number(inv.total_amount).toLocaleString() + ' ' + esc(inv.currency) + '</b></div>' +
    '<div class="tot-row" style="color:#15803d"><span>المدفوع:</span><b>' + Number(inv.paid_amount).toLocaleString() + ' ' + esc(inv.currency) + '</b></div>' +
    '<div class="tot-row final" style="color:'+(inv.remaining>0?'#dc2626':'#15803d')+'"><span>المتبقي للسداد:</span><b>' + Number(inv.remaining).toLocaleString() + ' ' + esc(inv.currency) + '</b></div>' +
  '</div>' +
  (inv.notes ? '<div style="margin-top:20px;padding:12px;background:#f8fafc;border-right:4px solid #15803d;font-size:13.5px"><b>ملاحظات / بيانات الدفع:</b><br>' + esc(inv.notes) + '</div>' : '') +
  '<div class="foot"><span>Developed & Issued by Eng. Samir Abo El-Khair</span><span>Version 3.5</span></div>';
</script>
</body>
</html>
"""
