"""
tasks.py  —  صفحة التاسكات (مستقلة) لمشروع server-monitor

الصفحة:  /tasks
الـ API:
  GET    /api/tasks?status=all|active|done&employee=NAME
  POST   /api/tasks
  PUT    /api/tasks/{id}
  DELETE /api/tasks/{id}
  GET    /api/tasks/employees
"""
from datetime import datetime
from typing import Optional

from fastapi import HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

VALID_STATUS = ("pending", "in_progress", "done")


class TaskIn(BaseModel):
    title: str
    details: str = ""
    employee: str = ""
    start_date: str = ""
    end_date: str = ""
    status: str = "pending"


class TaskUpdate(BaseModel):
    title: Optional[str] = None
    details: Optional[str] = None
    employee: Optional[str] = None
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
               start_date TEXT DEFAULT '',
               end_date TEXT DEFAULT '',
               status TEXT DEFAULT 'pending',
               created_at TEXT NOT NULL,
               completed_at TEXT DEFAULT ''
           )"""
    )
    conn.commit()
    conn.close()

    @app.get("/tasks", response_class=HTMLResponse)
    async def tasks_page():
        return HTMLResponse(TASKS_HTML)

    @app.get("/api/tasks/employees")
    def tasks_employees():
        conn = get_db()
        rows = conn.execute(
            "SELECT DISTINCT employee FROM tasks WHERE employee != '' ORDER BY employee COLLATE NOCASE"
        ).fetchall()
        conn.close()
        return [r["employee"] for r in rows]

    @app.get("/api/tasks")
    def tasks_list(status: str = "all", employee: str = ""):
        sql = "SELECT * FROM tasks WHERE 1=1"
        params = []
        if status == "active":
            sql += " AND status != 'done'"
        elif status == "done":
            sql += " AND status = 'done'"
        if employee:
            sql += " AND employee = ?"
            params.append(employee)
        # الشغالة: الأقرب انتهاءً الأول | المنتهية: الأحدث إنجازاً الأول
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
            """INSERT INTO tasks (title, details, employee, start_date, end_date, status, created_at, completed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (title, p.details.strip(), p.employee.strip(), p.start_date, p.end_date, p.status, now,
             now if p.status == "done" else ""),
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
        data = p.model_dump(exclude_none=True)
        for k, v in data.items():
            cur[k] = v.strip() if isinstance(v, str) else v
        if not cur["title"]:
            conn.close()
            raise HTTPException(status_code=400, detail="العنوان مطلوب")
        if cur["status"] not in VALID_STATUS:
            conn.close()
            raise HTTPException(status_code=400, detail="حالة غير صحيحة")
        try:
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
            """UPDATE tasks SET title=?, details=?, employee=?, start_date=?, end_date=?,
               status=?, completed_at=? WHERE id=?""",
            (cur["title"], cur["details"], cur["employee"], cur["start_date"], cur["end_date"],
             cur["status"], cur["completed_at"], task_id),
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
  :root{--bg:#f5f7fa;--card:#fff;--text:#1f2933;--muted:#6b7785;--border:#e3e8ee;--green:#16a34a;--amber:#f59e0b;--red:#dc2626;--blue:#2563eb;--gray:#94a3b8}
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
  button.sec{background:var(--gray)} button.ok{background:var(--green)} button.warn{background:var(--amber)} button.del{background:var(--red)}
  button.sm{padding:5px 10px;font-size:13px}
  .tabs{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:12px}
  .tab{background:var(--card);color:var(--text);border:1px solid var(--border)}
  .tab.on{background:var(--blue);color:#fff;border-color:var(--blue)}
  .tabs select{width:auto;min-width:150px;margin-inline-start:auto}
  .task{border-inline-start:5px solid var(--gray)}
  .task.pending{border-inline-start-color:var(--gray)}
  .task.in_progress{border-inline-start-color:var(--amber)}
  .task.done{border-inline-start-color:var(--green)}
  .task.overdue{border-inline-start-color:var(--red)}
  .row{display:flex;justify-content:space-between;align-items:flex-start;gap:10px;flex-wrap:wrap}
  .title{font-weight:600;font-size:16px}
  .badge{font-size:12px;padding:2px 10px;border-radius:999px;color:#fff;white-space:nowrap}
  .b-pending{background:var(--gray)}.b-in_progress{background:var(--amber)}.b-done{background:var(--green)}.b-overdue{background:var(--red)}
  .meta{display:flex;gap:16px;flex-wrap:wrap;color:var(--muted);font-size:13px;margin:8px 0}
  .meta b{color:var(--text)}
  .details{white-space:pre-wrap;font-size:14px;margin:6px 0 10px}
  .acts{display:flex;gap:6px;flex-wrap:wrap}
  .empty{text-align:center;color:var(--muted);padding:30px}
  #formCard{display:none}
</style>
</head>
<body>
<div class="wrap">
  <div class="top">
    <h1>📋 التاسكات</h1>
    <div><a href="/">لوحة المراقبة</a> &nbsp;|&nbsp; <a href="/status">صفحة الحالة</a></div>
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
      <div><label>الموظف المسؤول</label><input id="fEmployee" list="empList" maxlength="100"><datalist id="empList"></datalist></div>
      <div><label>الحالة</label>
        <select id="fStatus">
          <option value="pending">لم يبدأ</option>
          <option value="in_progress">شغال عليها</option>
          <option value="done">انتهت</option>
        </select></div>
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
    <select id="empFilter" onchange="load()"><option value="">كل الموظفين</option></select>
  </div>

  <div id="list"></div>
</div>

<script>
var tab = 'active';
var editingId = null;
var STATUS_AR = {pending:'لم يبدأ', in_progress:'شغال عليها', done:'انتهت'};

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
  document.getElementById('fEmployee').value = task ? task.employee : '';
  document.getElementById('fStatus').value = task ? task.status : 'pending';
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
    employee: document.getElementById('fEmployee').value,
    status: document.getElementById('fStatus').value,
    start_date: document.getElementById('fStart').value,
    end_date: document.getElementById('fEnd').value
  };
  var url = editingId ? '/api/tasks/' + editingId : '/api/tasks';
  api(url, {method: editingId ? 'PUT' : 'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)})
    .then(function(){ hideForm(); load(); loadEmployees(); })
    .catch(function(e){ document.getElementById('formErr').textContent = e.message; });
}

function setStatus(id, status){
  api('/api/tasks/' + id, {method:'PUT', headers:{'Content-Type':'application/json'}, body: JSON.stringify({status: status})})
    .then(load);
}
function delTask(id){
  if(!confirm('حذف التاسك نهائياً؟')) return;
  api('/api/tasks/' + id, {method:'DELETE'}).then(function(){ load(); loadEmployees(); });
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
    var badge = t.overdue
      ? '<span class="badge b-overdue">متأخرة</span> '
      : '';
    badge += '<span class="badge b-' + esc(t.status) + '">' + esc(STATUS_AR[t.status] || t.status) + '</span>';
    var acts = '';
    if(t.status === 'pending')     acts += '<button class="sm warn" onclick="setStatus('+t.id+',\\'in_progress\\')">بدء الشغل</button>';
    if(t.status !== 'done')        acts += '<button class="sm ok" onclick="setStatus('+t.id+',\\'done\\')">تم الانتهاء</button>';
    if(t.status === 'done')        acts += '<button class="sm sec" onclick="setStatus('+t.id+',\\'in_progress\\')">إعادة فتح</button>';
    acts += '<button class="sm" onclick="editTask('+t.id+')">تعديل</button>';
    acts += '<button class="sm del" onclick="delTask('+t.id+')">حذف</button>';
    return '<div class="card task ' + cls + '">' +
      '<div class="row"><span class="title">' + esc(t.title) + '</span><span>' + badge + '</span></div>' +
      '<div class="meta">' +
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
  api('/api/tasks?status=' + tab + '&employee=' + encodeURIComponent(emp)).then(render);
  api('/api/tasks?status=all').then(function(all){
    var active = all.filter(function(t){return t.status !== 'done';}).length;
    var over = all.filter(function(t){return t.overdue;}).length;
    document.getElementById('sActive').textContent = active;
    document.getElementById('sOver').textContent = over;
    document.getElementById('sDone').textContent = all.length - active;
    document.getElementById('sAll').textContent = all.length;
  });
}

function loadEmployees(){
  api('/api/tasks/employees').then(function(list){
    document.getElementById('empList').innerHTML = list.map(function(n){return '<option value="'+esc(n)+'">';}).join('');
    var sel = document.getElementById('empFilter');
    var cur = sel.value;
    sel.innerHTML = '<option value="">كل الموظفين</option>' + list.map(function(n){return '<option value="'+esc(n)+'">'+esc(n)+'</option>';}).join('');
    sel.value = cur;
  });
}

loadEmployees();
load();
</script>
</body>
</html>
"""
