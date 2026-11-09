"""
tasks.py  —  صفحة التاسكات (مستقلة) لمشروع server-monitor

الصفحات:
  /tasks          إدارة التاسكات
  /tasks/report   تقرير لكل شركة (طباعة / PDF / CSV)
الـ API:
  GET    /api/tasks?status=all|active|done&employee=&company=&date_from=&date_to=
  POST   /api/tasks
  PUT    /api/tasks/{id}
  DELETE /api/tasks/{id}
  GET    /api/tasks/employees
  GET    /api/tasks/companies
  GET    /api/tasks/export.csv?company=&date_from=&date_to=
"""
import csv
import io
from datetime import datetime
from typing import Optional

from fastapi import HTTPException
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel

VALID_STATUS = ("pending", "new", "in_progress", "solving", "done")
STATUS_AR = {
    "pending": "لم يبدأ",
    "new": "جديدة",
    "in_progress": "يتم العمل عليها",
    "solving": "جاري حل المشكلة",
    "done": "تم الانتهاء",
}


class TaskIn(BaseModel):
    title: str
    details: str = ""
    employee: str = ""
    company: str = ""
    start_date: str = ""
    end_date: str = ""
    status: str = "new"


class TaskUpdate(BaseModel):
    title: Optional[str] = None
    details: Optional[str] = None
    employee: Optional[str] = None
    company: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    status: Optional[str] = None


def _check_dates(start: str, end: str) -> None:
    for label, v in (("start_date", start), ("end_date", end)):
        if v:
            try:
                datetime.strptime(v, "%Y-%m-%d")
            except ValueError:
                raise HTTPException(status_code=400, detail=f"{label} غير صحيح (YYYY-MM-DD)")
    if start and end and end < start:
        raise HTTPException(status_code=400, detail="تاريخ الانتهاء لازم يكون بعد تاريخ البداية")


def register(app, get_db):
    conn = get_db()
    conn.execute(
        """CREATE TABLE IF NOT EXISTS tasks (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               title TEXT NOT NULL,
               details TEXT DEFAULT '',
               employee TEXT DEFAULT '',
               company TEXT DEFAULT '',
               start_date TEXT DEFAULT '',
               end_date TEXT DEFAULT '',
               status TEXT DEFAULT 'new',
               created_at TEXT NOT NULL,
               completed_at TEXT DEFAULT ''
           )"""
    )
    cols = [r[1] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()]
    if "company" not in cols:  # ترقية الجدول القديم
        conn.execute("ALTER TABLE tasks ADD COLUMN company TEXT DEFAULT ''")
    conn.commit()
    conn.close()

    def _query(status="all", employee="", company="", date_from="", date_to=""):
        sql = "SELECT * FROM tasks WHERE 1=1"
        params = []
        if status == "active":
            sql += " AND status != 'done'"
        elif status == "done":
            sql += " AND status = 'done'"
        if employee:
            sql += " AND employee = ?"
            params.append(employee)
        if company:
            if company == "__none__":
                sql += " AND company = ''"
            else:
                sql += " AND company = ?"
                params.append(company)
        day = "COALESCE(NULLIF(start_date, ''), substr(created_at, 1, 10))"
        if date_from:
            sql += f" AND {day} >= ?"
            params.append(date_from)
        if date_to:
            sql += f" AND {day} <= ?"
            params.append(date_to)
        sql += """ ORDER BY CASE WHEN status = 'done' THEN 1 ELSE 0 END,
                   CASE WHEN status = 'done' THEN '' WHEN end_date = '' THEN '9999' ELSE end_date END ASC,
                   completed_at DESC, id DESC"""
        conn = get_db()
        rows = conn.execute(sql, params).fetchall()
        conn.close()
        today = datetime.now().strftime("%Y-%m-%d")
        out = []
        for r in rows:
            d = dict(r)
            d["overdue"] = bool(d["status"] != "done" and d["end_date"] and d["end_date"] < today)
            out.append(d)
        return out

    @app.get("/tasks", response_class=HTMLResponse)
    async def tasks_page():
        return HTMLResponse(TASKS_HTML)

    @app.get("/tasks/report", response_class=HTMLResponse)
    async def tasks_report_page():
        return HTMLResponse(REPORT_HTML)

    @app.get("/api/tasks/employees")
    def tasks_employees():
        conn = get_db()
        rows = conn.execute(
            "SELECT DISTINCT employee FROM tasks WHERE employee != '' ORDER BY employee COLLATE NOCASE"
        ).fetchall()
        conn.close()
        return [r["employee"] for r in rows]

    @app.get("/api/tasks/companies")
    def tasks_companies():
        conn = get_db()
        rows = conn.execute(
            "SELECT DISTINCT company FROM tasks WHERE company != '' ORDER BY company COLLATE NOCASE"
        ).fetchall()
        conn.close()
        return [r["company"] for r in rows]

    @app.get("/api/tasks/export.csv")
    def tasks_export(company: str = "", date_from: str = "", date_to: str = ""):
        rows = _query("all", "", company, date_from, date_to)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["الشركة", "العنوان", "التفاصيل", "الموظف", "الحالة", "البداية", "الانتهاء", "تاريخ الإنجاز", "متأخرة"])
        for r in sorted(rows, key=lambda x: (x["company"] or "~", x["id"])):
            w.writerow([
                r["company"], r["title"], r["details"], r["employee"],
                STATUS_AR.get(r["status"], r["status"]),
                r["start_date"], r["end_date"], r["completed_at"],
                "نعم" if r["overdue"] else "",
            ])
        data = ("\ufeff" + buf.getvalue()).encode("utf-8")  # BOM علشان Excel يقرا العربي صح
        return Response(
            content=data,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": "attachment; filename=tasks-report.csv"},
        )

    @app.get("/api/tasks")
    def tasks_list(status: str = "all", employee: str = "", company: str = "",
                   date_from: str = "", date_to: str = ""):
        return _query(status, employee, company, date_from, date_to)

    @app.post("/api/tasks")
    def tasks_create(p: TaskIn):
        title = p.title.strip()
        if not title:
            raise HTTPException(status_code=400, detail="العنوان مطلوب")
        if p.status not in VALID_STATUS:
            raise HTTPException(status_code=400, detail="حالة غير صحيحة")
        _check_dates(p.start_date, p.end_date)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn = get_db()
        cur = conn.execute(
            """INSERT INTO tasks (title, details, employee, company, start_date, end_date, status, created_at, completed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (title, p.details.strip(), p.employee.strip(), p.company.strip(), p.start_date, p.end_date,
             p.status, now, now if p.status == "done" else ""),
        )
        conn.commit()
        task_id = cur.lastrowid
        conn.close()
        return {"ok": True, "id": task_id}

    @app.put("/api/tasks/{task_id}")
    def tasks_update(task_id: int, p: TaskUpdate):
        conn = get_db()
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not row:
            conn.close()
            raise HTTPException(status_code=404, detail="التاسك غير موجود")
        cur = dict(row)
        for k, v in p.model_dump(exclude_none=True).items():
            cur[k] = v.strip() if isinstance(v, str) else v
        try:
            if not cur["title"]:
                raise HTTPException(status_code=400, detail="العنوان مطلوب")
            if cur["status"] not in VALID_STATUS:
                raise HTTPException(status_code=400, detail="حالة غير صحيحة")
            _check_dates(cur["start_date"], cur["end_date"])
        except HTTPException:
            conn.close()
            raise
        if cur["status"] == "done":
            if row["status"] != "done":
                cur["completed_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        else:
            cur["completed_at"] = ""
        conn.execute(
            """UPDATE tasks SET title=?, details=?, employee=?, company=?, start_date=?, end_date=?,
               status=?, completed_at=? WHERE id=?""",
            (cur["title"], cur["details"], cur["employee"], cur["company"], cur["start_date"],
             cur["end_date"], cur["status"], cur["completed_at"], task_id),
        )
        conn.commit()
        conn.close()
        return {"ok": True}

    @app.delete("/api/tasks/{task_id}")
    def tasks_delete(task_id: int):
        conn = get_db()
        conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        conn.commit()
        conn.close()
        return {"ok": True}


TASKS_HTML = """<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>التاسكات | Tasks</title>
<style>
  :root{--bg:#f5f7fa;--card:#fff;--text:#1f2933;--muted:#6b7785;--border:#e3e8ee;--green:#16a34a;--amber:#f59e0b;--red:#dc2626;--blue:#2563eb;--purple:#7c3aed;--gray:#94a3b8}
  @media (prefers-color-scheme: dark){:root{--bg:#0f141a;--card:#171e26;--text:#e6edf3;--muted:#8b98a5;--border:#26303b}}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);font-family:system-ui,-apple-system,"Segoe UI",Tahoma,sans-serif}
  .wrap{max-width:960px;margin:0 auto;padding:20px 16px}
  .top{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px;margin-bottom:16px}
  h1{margin:0;font-size:22px}
  a{color:var(--blue);text-decoration:none;font-size:14px}
  .stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin-bottom:16px}
  .stat{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:12px 14px}
  .stat b{display:block;font-size:22px}.stat span{color:var(--muted);font-size:13px}
  .card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:16px;margin-bottom:12px}
  .grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
  @media (max-width:600px){.grid{grid-template-columns:1fr}}
  label{display:block;font-size:13px;color:var(--muted);margin-bottom:4px}
  input,textarea,select{width:100%;padding:9px 10px;border:1px solid var(--border);border-radius:8px;background:var(--bg);color:var(--text);font:inherit}
  textarea{min-height:80px;resize:vertical}
  .full{grid-column:1/-1}
  button{border:0;border-radius:8px;padding:9px 16px;cursor:pointer;font:inherit;color:#fff;background:var(--blue)}
  button.sec{background:var(--gray)} button.ok{background:var(--green)} button.del{background:var(--red)}
  button.sm{padding:5px 10px;font-size:13px}
  .tabs{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:12px}
  .tab{background:var(--card);color:var(--text);border:1px solid var(--border)}
  .tab.on{background:var(--blue);color:#fff;border-color:var(--blue)}
  .tabs select{width:auto;min-width:140px}
  .task{border-inline-start:5px solid var(--gray)}
  .task.new{border-inline-start-color:var(--blue)}
  .task.in_progress{border-inline-start-color:var(--amber)}
  .task.solving{border-inline-start-color:var(--purple)}
  .task.done{border-inline-start-color:var(--green)}
  .task.overdue{border-inline-start-color:var(--red)}
  .row{display:flex;justify-content:space-between;align-items:flex-start;gap:10px;flex-wrap:wrap}
  .title{font-weight:600;font-size:16px}
  .badge{font-size:12px;padding:2px 10px;border-radius:999px;color:#fff;white-space:nowrap}
  .b-pending{background:var(--gray)}.b-new{background:var(--blue)}.b-in_progress{background:var(--amber)}
  .b-solving{background:var(--purple)}.b-done{background:var(--green)}.b-overdue{background:var(--red)}
  .meta{display:flex;gap:16px;flex-wrap:wrap;color:var(--muted);font-size:13px;margin:8px 0}
  .meta b{color:var(--text)}
  .details{white-space:pre-wrap;font-size:14px;margin:6px 0 10px}
  .acts{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
  .acts select{width:auto;padding:5px 8px;font-size:13px}
  .empty{text-align:center;color:var(--muted);padding:30px}
  #formCard{display:none}
</style>
</head>
<body>
<div class="wrap">
  <div class="top">
    <h1>📋 التاسكات</h1>
    <div><a href="/tasks/report">📊 تقرير الشركات</a> &nbsp;|&nbsp; <a href="/">لوحة المراقبة</a> &nbsp;|&nbsp; <a href="/status">صفحة الحالة</a></div>
  </div>

  <div class="stats">
    <div class="stat"><b id="sActive">0</b><span>شغالة</span></div>
    <div class="stat"><b id="sOver" style="color:var(--red)">0</b><span>متأخرة</span></div>
    <div class="stat"><b id="sDone" style="color:var(--green)">0</b><span>منتهية</span></div>
    <div class="stat"><b id="sAll">0</b><span>الإجمالي</span></div>
  </div>

  <div style="margin-bottom:12px"><button onclick="showForm()">+ تاسك جديد</button></div>

  <div class="card" id="formCard">
    <h3 id="formTitle" style="margin-top:0">تاسك جديد</h3>
    <div class="grid">
      <div class="full"><label>العنوان *</label><input id="fTitle" maxlength="200"></div>
      <div class="full"><label>التفاصيل</label><textarea id="fDetails"></textarea></div>
      <div><label>الشركة</label><input id="fCompany" list="coList" maxlength="150" placeholder="اكتب اسم الشركة"><datalist id="coList"></datalist></div>
      <div><label>الموظف المسؤول</label><input id="fEmployee" list="empList" maxlength="100"><datalist id="empList"></datalist></div>
      <div><label>الحالة</label>
        <select id="fStatus">
          <option value="pending">لم يبدأ</option>
          <option value="new">جديدة</option>
          <option value="in_progress">يتم العمل عليها</option>
          <option value="solving">جاري حل المشكلة</option>
          <option value="done">تم الانتهاء</option>
        </select></div>
      <div></div>
      <div><label>تاريخ البداية</label><input type="date" id="fStart"></div>
      <div><label>تاريخ الانتهاء</label><input type="date" id="fEnd"></div>
    </div>
    <div style="margin-top:12px;display:flex;gap:8px">
      <button class="ok" onclick="saveTask()">حفظ</button>
      <button class="sec" onclick="hideForm()">إلغاء</button>
    </div>
    <div id="formErr" style="color:var(--red);margin-top:8px;font-size:13px"></div>
  </div>

  <div class="tabs">
    <button class="tab on" id="tab-active" onclick="setTab('active')">الشغالة</button>
    <button class="tab" id="tab-done" onclick="setTab('done')">المنتهية</button>
    <button class="tab" id="tab-all" onclick="setTab('all')">الكل</button>
    <select id="coFilter" onchange="load()"><option value="">كل الشركات</option></select>
    <select id="empFilter" onchange="load()"><option value="">كل الموظفين</option></select>
  </div>

  <div id="list"></div>
</div>

<script>
var tab = 'active';
var editingId = null;
var STATUS_AR = {pending:'لم يبدأ', 'new':'جديدة', in_progress:'يتم العمل عليها', solving:'جاري حل المشكلة', done:'تم الانتهاء'};
var STATUS_ORDER = ['pending','new','in_progress','solving','done'];

function esc(s){var d=document.createElement('div');d.textContent=s==null?'':String(s);return d.innerHTML;}
function api(url, opts){
  return fetch(url, opts).then(function(r){
    return r.json().then(function(j){ if(!r.ok) throw new Error(j.detail || 'خطأ'); return j; });
  });
}

function setTab(t){
  tab = t;
  ['active','done','all'].forEach(function(x){
    document.getElementById('tab-'+x).className = 'tab' + (x===t ? ' on' : '');
  });
  load();
}

function showForm(task){
  editingId = task ? task.id : null;
  document.getElementById('formTitle').textContent = task ? 'تعديل التاسك' : 'تاسك جديد';
  document.getElementById('fTitle').value = task ? task.title : '';
  document.getElementById('fDetails').value = task ? task.details : '';
  document.getElementById('fCompany').value = task ? task.company : document.getElementById('coFilter').value;
  document.getElementById('fEmployee').value = task ? task.employee : '';
  document.getElementById('fStatus').value = task ? task.status : 'new';
  document.getElementById('fStart').value = task ? task.start_date : new Date().toISOString().slice(0,10);
  document.getElementById('fEnd').value = task ? task.end_date : '';
  document.getElementById('formErr').textContent = '';
  document.getElementById('formCard').style.display = 'block';
  window.scrollTo({top:0, behavior:'smooth'});
}
function hideForm(){ document.getElementById('formCard').style.display = 'none'; editingId = null; }

function saveTask(){
  var body = {
    title: document.getElementById('fTitle').value,
    details: document.getElementById('fDetails').value,
    company: document.getElementById('fCompany').value,
    employee: document.getElementById('fEmployee').value,
    status: document.getElementById('fStatus').value,
    start_date: document.getElementById('fStart').value,
    end_date: document.getElementById('fEnd').value
  };
  var url = editingId ? '/api/tasks/' + editingId : '/api/tasks';
  api(url, {method: editingId ? 'PUT' : 'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)})
    .then(function(){ hideForm(); load(); loadLists(); })
    .catch(function(e){ document.getElementById('formErr').textContent = e.message; });
}

function setStatus(id, status){
  api('/api/tasks/' + id, {method:'PUT', headers:{'Content-Type':'application/json'}, body: JSON.stringify({status: status})})
    .then(load);
}
function delTask(id){
  if(!confirm('حذف التاسك نهائياً؟')) return;
  api('/api/tasks/' + id, {method:'DELETE'}).then(function(){ load(); loadLists(); });
}

var cache = {};
function editTask(id){ showForm(cache[id]); }

function render(tasks){
  cache = {};
  if(!tasks.length){
    document.getElementById('list').innerHTML = '<div class="card empty">لا توجد تاسكات هنا.</div>';
    return;
  }
  document.getElementById('list').innerHTML = tasks.map(function(t){
    cache[t.id] = t;
    var cls = t.status + (t.overdue ? ' overdue' : '');
    var badge = t.overdue ? '<span class="badge b-overdue">متأخرة</span> ' : '';
    badge += '<span class="badge b-' + esc(t.status) + '">' + esc(STATUS_AR[t.status] || t.status) + '</span>';
    var opts = STATUS_ORDER.map(function(s){
      return '<option value="' + s + '"' + (s === t.status ? ' selected' : '') + '>' + STATUS_AR[s] + '</option>';
    }).join('');
    var acts = '<select onchange="setStatus(' + t.id + ', this.value)">' + opts + '</select>';
    if(t.status !== 'done') acts += '<button class="sm ok" onclick="setStatus(' + t.id + ',\\'done\\')">تم الانتهاء</button>';
    acts += '<button class="sm" onclick="editTask(' + t.id + ')">تعديل</button>';
    acts += '<button class="sm del" onclick="delTask(' + t.id + ')">حذف</button>';
    return '<div class="card task ' + cls + '">' +
      '<div class="row"><span class="title">' + esc(t.title) + '</span><span>' + badge + '</span></div>' +
      '<div class="meta">' +
        '<span>🏢 الشركة: <b>' + esc(t.company || '—') + '</b></span>' +
        '<span>👤 الموظف: <b>' + esc(t.employee || '—') + '</b></span>' +
        '<span>🟢 البداية: <b>' + esc(t.start_date || '—') + '</b></span>' +
        '<span>🔴 الانتهاء: <b>' + esc(t.end_date || '—') + '</b></span>' +
        (t.status === 'done' ? '<span>✅ اتنفذت في: <b>' + esc(t.completed_at) + '</b></span>' : '') +
        '<span>🕒 أُضيفت: ' + esc(t.created_at) + '</span>' +
      '</div>' +
      (t.details ? '<div class="details">' + esc(t.details) + '</div>' : '') +
      '<div class="acts">' + acts + '</div></div>';
  }).join('');
}

function load(){
  var emp = document.getElementById('empFilter').value;
  var co = document.getElementById('coFilter').value;
  api('/api/tasks?status=' + tab + '&employee=' + encodeURIComponent(emp) + '&company=' + encodeURIComponent(co)).then(render);
  api('/api/tasks?status=all&company=' + encodeURIComponent(co)).then(function(all){
    var active = all.filter(function(t){return t.status !== 'done';}).length;
    var over = all.filter(function(t){return t.overdue;}).length;
    document.getElementById('sActive').textContent = active;
    document.getElementById('sOver').textContent = over;
    document.getElementById('sDone').textContent = all.length - active;
    document.getElementById('sAll').textContent = all.length;
  });
}

function fillSelect(id, firstLabel, list){
  var sel = document.getElementById(id);
  var cur = sel.value;
  sel.innerHTML = '<option value="">' + firstLabel + '</option>' +
    list.map(function(n){return '<option value="'+esc(n)+'">'+esc(n)+'</option>';}).join('');
  sel.value = cur;
}

function loadLists(){
  api('/api/tasks/employees').then(function(list){
    document.getElementById('empList').innerHTML = list.map(function(n){return '<option value="'+esc(n)+'">';}).join('');
    fillSelect('empFilter', 'كل الموظفين', list);
  });
  api('/api/tasks/companies').then(function(list){
    document.getElementById('coList').innerHTML = list.map(function(n){return '<option value="'+esc(n)+'">';}).join('');
    fillSelect('coFilter', 'كل الشركات', list);
  });
}

loadLists();
load();
</script>
</body>
</html>
"""


REPORT_HTML = """<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>تقرير التاسكات حسب الشركة</title>
<style>
  :root{--bg:#f5f7fa;--card:#fff;--text:#1f2933;--muted:#6b7785;--border:#e3e8ee;--green:#16a34a;--amber:#f59e0b;--red:#dc2626;--blue:#2563eb;--purple:#7c3aed;--gray:#94a3b8}
  @media (prefers-color-scheme: dark){:root{--bg:#0f141a;--card:#171e26;--text:#e6edf3;--muted:#8b98a5;--border:#26303b}}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);font-family:system-ui,-apple-system,"Segoe UI",Tahoma,sans-serif}
  .wrap{max-width:1000px;margin:0 auto;padding:20px 16px}
  .top{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px;margin-bottom:14px}
  h1{margin:0;font-size:22px}
  a{color:var(--blue);text-decoration:none;font-size:14px}
  .filters{display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end;background:var(--card);border:1px solid var(--border);border-radius:10px;padding:12px 14px;margin-bottom:16px}
  .filters div{min-width:150px}
  label{display:block;font-size:12px;color:var(--muted);margin-bottom:3px}
  input,select{width:100%;padding:8px 10px;border:1px solid var(--border);border-radius:8px;background:var(--bg);color:var(--text);font:inherit}
  button,.btn{border:0;border-radius:8px;padding:9px 14px;cursor:pointer;font:inherit;font-size:14px;color:#fff;background:var(--blue);text-decoration:none;display:inline-block}
  .btn.g{background:var(--green)} .btn.s{background:var(--gray)}
  .company{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:16px;margin-bottom:18px;break-inside:avoid-page}
  .company h2{margin:0 0 8px;font-size:18px}
  .sum{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}
  .pill{font-size:12px;padding:3px 10px;border-radius:999px;color:#fff}
  .p-total{background:var(--text);color:var(--bg)}.p-pending{background:var(--gray)}.p-new{background:var(--blue)}
  .p-in_progress{background:var(--amber)}.p-solving{background:var(--purple)}.p-done{background:var(--green)}.p-overdue{background:var(--red)}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th,td{padding:7px 8px;border-bottom:1px solid var(--border);text-align:start;vertical-align:top}
  th{color:var(--muted);font-weight:600;font-size:12px}
  .od{color:var(--red);font-weight:600}
  .empty{text-align:center;color:var(--muted);padding:30px}
  .head-print{display:none}
  @media print{
    body{background:#fff;color:#000}
    .no-print{display:none !important}
    .head-print{display:block;margin-bottom:12px}
    .company{border:1px solid #bbb;box-shadow:none}
    .company{page-break-inside:avoid}
    :root{--card:#fff;--text:#000;--muted:#444;--border:#ccc;--bg:#fff}
  }
</style>
</head>
<body>
<div class="wrap">
  <div class="top no-print">
    <h1>📊 تقرير التاسكات حسب الشركة</h1>
    <div><a href="/tasks">← التاسكات</a> &nbsp;|&nbsp; <a href="/">لوحة المراقبة</a></div>
  </div>

  <div class="filters no-print">
    <div><label>الشركة</label><select id="company" onchange="load()"><option value="">كل الشركات (كل شركة لوحدها)</option></select></div>
    <div><label>من تاريخ (بداية التاسك)</label><input type="date" id="from" onchange="load()"></div>
    <div><label>إلى تاريخ</label><input type="date" id="to" onchange="load()"></div>
    <button onclick="window.print()">🖨 طباعة / PDF</button>
    <a class="btn g" id="csv" href="#">⬇ تصدير Excel (CSV)</a>
  </div>

  <div class="head-print"><h2 id="printTitle">تقرير التاسكات</h2><div id="printSub"></div></div>
  <div id="out"></div>
</div>

<script>
var STATUS_AR = {pending:'لم يبدأ', 'new':'جديدة', in_progress:'يتم العمل عليها', solving:'جاري حل المشكلة', done:'تم الانتهاء'};
var STATUS_ORDER = ['pending','new','in_progress','solving','done'];

function esc(s){var d=document.createElement('div');d.textContent=s==null?'':String(s);return d.innerHTML;}
function api(url){ return fetch(url).then(function(r){ return r.json(); }); }

function qs(){
  var c = document.getElementById('company').value;
  var f = document.getElementById('from').value;
  var t = document.getElementById('to').value;
  return 'company=' + encodeURIComponent(c) + '&date_from=' + encodeURIComponent(f) + '&date_to=' + encodeURIComponent(t);
}

function group(tasks){
  var g = {}, order = [];
  tasks.forEach(function(t){
    var k = t.company || '';
    if(!(k in g)){ g[k] = []; order.push(k); }
    g[k].push(t);
  });
  order.sort(function(a,b){
    if(a === '') return 1; if(b === '') return -1;
    return a.localeCompare(b, 'ar');
  });
  return order.map(function(k){ return {name: k, tasks: g[k]}; });
}

function render(tasks){
  var out = document.getElementById('out');
  if(!tasks.length){ out.innerHTML = '<div class="company empty">لا توجد تاسكات بهذه الفلاتر.</div>'; return; }
  out.innerHTML = group(tasks).map(function(g){
    var counts = {}; STATUS_ORDER.forEach(function(s){ counts[s] = 0; });
    var over = 0;
    g.tasks.forEach(function(t){ counts[t.status] = (counts[t.status] || 0) + 1; if(t.overdue) over++; });
    var pills = '<span class="pill p-total">الإجمالي: ' + g.tasks.length + '</span>';
    STATUS_ORDER.forEach(function(s){ if(counts[s]) pills += '<span class="pill p-' + s + '">' + STATUS_AR[s] + ': ' + counts[s] + '</span>'; });
    if(over) pills += '<span class="pill p-overdue">متأخرة: ' + over + '</span>';
    var rows = g.tasks.map(function(t){
      return '<tr><td>' + esc(t.title) + (t.details ? '<div style="color:var(--muted)">' + esc(t.details) + '</div>' : '') + '</td>' +
        '<td>' + esc(t.employee || '—') + '</td>' +
        '<td>' + esc(STATUS_AR[t.status] || t.status) + (t.overdue ? ' <span class="od">(متأخرة)</span>' : '') + '</td>' +
        '<td>' + esc(t.start_date || '—') + '</td>' +
        '<td>' + esc(t.end_date || '—') + '</td>' +
        '<td>' + esc(t.completed_at ? t.completed_at.slice(0,10) : '—') + '</td></tr>';
    }).join('');
    return '<div class="company"><h2>🏢 ' + esc(g.name || 'بدون شركة') + '</h2>' +
      '<div class="sum">' + pills + '</div>' +
      '<table><thead><tr><th>التاسك</th><th>الموظف</th><th>الحالة</th><th>البداية</th><th>الانتهاء</th><th>تاريخ الإنجاز</th></tr></thead><tbody>' +
      rows + '</tbody></table></div>';
  }).join('');
}

function load(){
  document.getElementById('csv').href = '/api/tasks/export.csv?' + qs();
  var c = document.getElementById('company').value;
  var f = document.getElementById('from').value, t = document.getElementById('to').value;
  document.getElementById('printTitle').textContent = 'تقرير التاسكات' + (c ? ' — ' + c : ' — كل الشركات');
  document.getElementById('printSub').textContent = 'تاريخ التقرير: ' + new Date().toISOString().slice(0,10) +
    (f ? ' | من: ' + f : '') + (t ? ' | إلى: ' + t : '');
  api('/api/tasks?status=all&' + qs()).then(render);
}

api('/api/tasks/companies').then(function(list){
  var sel = document.getElementById('company');
  sel.innerHTML = '<option value="">كل الشركات (كل شركة لوحدها)</option>' +
    list.map(function(n){ return '<option value="' + esc(n) + '">' + esc(n) + '</option>'; }).join('') +
    '<option value="__none__">بدون شركة</option>';
  var q = new URLSearchParams(location.search).get('company');
  if(q) sel.value = q;
  load();
});
</script>
</body>
</html>
"""
