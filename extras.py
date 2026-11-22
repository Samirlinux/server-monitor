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


# ───────────────────────── تفضيلات الخدمات (إظهار في اللوحة / تنبيه) ─────────────────────────
DEFAULT_ALERT_KEYWORDS = [
    k.strip().lower()
    for k in os.getenv("ALERT_SERVICES", "apache,httpd,mysql,maria").split(",")
    if k.strip()
]


def parse_prefs(settings):
    """بيرجّع {service_key: {"show": bool, "alert": bool}} من جدول الإعدادات."""
    try:
        p = json.loads(settings.get("service_prefs") or "{}")
        return p if isinstance(p, dict) else {}
    except Exception:
        return {}


def svc_visible(svc, prefs):
    p = prefs.get(str(svc.get("key", "")))
    if isinstance(p, dict) and "show" in p:
        return bool(p["show"])
    return True


def svc_alert_enabled(svc, prefs):
    """الخدمة المخفية ما بتبعتش تنبيه. لو ما اتحددش اختيار: Apache وقواعد البيانات بس."""
    if not svc_visible(svc, prefs):
        return False
    p = prefs.get(str(svc.get("key", "")))
    if isinstance(p, dict) and "alert" in p:
        return bool(p["alert"])
    if str(svc.get("key", "")).startswith("chk_"):
        return True   # الفحوصات الإضافية بتبعت تنبيه تلقائياً
    text = f"{svc.get('key', '')} {svc.get('name', '')}".lower()
    return any(k in text for k in DEFAULT_ALERT_KEYWORDS)


# ───────────────────────── لوحة الإعدادات (ودجت داخل اللوحة + صفحة /status-settings) ─────────────────────────
PANEL_INNER = r"""
<div id="sx-body" style="font-size:14px;color:#1f2933">
  <div style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:12px">
    <a href="/status" target="_blank" style="color:#2563eb">🌐 صفحة الاب تايم</a>
    <a href="/tasks" target="_blank" style="color:#2563eb">📋 التاسكات</a>
    <a href="/tasks/report" target="_blank" style="color:#2563eb">📊 تقرير الشركات</a>
    <a href="/checks" target="_blank" style="color:#2563eb">🔎 الفحوصات الإضافية</a>
  </div>

  <div style="font-weight:700;margin-bottom:4px">1) السيرفرات الظاهرة في صفحة الاب تايم</div>
  <div style="color:#6b7785;font-size:12px;margin-bottom:6px">شيل العلامة لإخفاء السيرفر من الصفحة العامة (بيفضل متراقب). زر إعادة العد بيمسح سجل الاب تايم للسيرفر ويبدأ العد من الأول.</div>
  <div id="sx-list">...</div>
  <button id="sx-reset-all" style="margin-top:8px;background:#dc2626;color:#fff;border:0;border-radius:6px;padding:6px 12px;cursor:pointer">🔄 إعادة العد لكل السيرفرات</button>

  <hr style="margin:16px 0;border:0;border-top:1px solid #e3e8ee">

  <div style="font-weight:700;margin-bottom:4px">2) الخدمات</div>
  <div style="color:#6b7785;font-size:12px;margin-bottom:6px">إظهار: الخدمة تظهر في لوحة المراقبة. تنبيه: يوصلك تنبيه لما تقع. الخدمة المخفية ما بتبعتش تنبيهات.</div>
  <div id="sx-svc">...</div>

  <hr style="margin:16px 0;border:0;border-top:1px solid #e3e8ee">

  <div style="font-weight:700;margin-bottom:4px">3) تنبيهات الميل</div>
  <label style="display:flex;align-items:center;gap:8px;cursor:pointer">
    <input type="checkbox" id="sx-mail" style="width:18px;height:18px">
    <span>تنبيهات طابور الإرسال والسبام (Mail queue / Spam)</span>
  </label>
  <div style="color:#6b7785;font-size:12px;margin-top:4px">لو مقفولة: مش هيوصلك أي تنبيه عن طابور الإرسال أو السبام.</div>
</div>
"""

PANEL_JS = r"""
function sxEsc(s){var d=document.createElement('div');d.textContent=s==null?'':String(s);return d.innerHTML;}
function sxPost(url, body){
  return fetch(url, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)})
    .then(function(r){ return r.json(); });
}
function sxLoad(){
  fetch('/api/status-visibility').then(function(r){return r.json();}).then(function(d){
    document.getElementById('sx-list').innerHTML = d.map(function(s){
      return '<div style="display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid #e3e8ee">' +
        '<label style="display:flex;align-items:center;gap:8px;flex:1;cursor:pointer">' +
        '<input type="checkbox" data-vis="' + s.id + '"' + (s.public_visible ? ' checked' : '') + ' style="width:18px;height:18px">' +
        '<span><b>' + sxEsc(s.name) + '</b><br><small style="color:#6b7785">' + sxEsc(s.ip) + '</small></span></label>' +
        '<button data-reset="' + s.id + '" style="background:#dc2626;color:#fff;border:0;border-radius:6px;padding:4px 8px;cursor:pointer;font-size:12px">🔄 إعادة العد</button></div>';
    }).join('') || 'لا توجد سيرفرات';
  });
  fetch('/api/service-prefs').then(function(r){return r.json();}).then(function(d){
    document.getElementById('sx-mail').checked = !!d.mail_alerts;
    document.getElementById('sx-svc').innerHTML = d.services.map(function(s){
      var k = encodeURIComponent(s.key);
      return '<div style="display:flex;align-items:center;gap:10px;padding:6px 0;border-bottom:1px solid #e3e8ee">' +
        '<span style="flex:1"><b>' + sxEsc(s.name) + '</b><br><small style="color:#6b7785">' + sxEsc(s.key) + '</small></span>' +
        '<label style="display:flex;align-items:center;gap:4px;cursor:pointer">إظهار <input type="checkbox" data-sshow="' + k + '"' + (s.show ? ' checked' : '') + ' style="width:18px;height:18px"></label>' +
        '<label style="display:flex;align-items:center;gap:4px;cursor:pointer">تنبيه <input type="checkbox" data-salert="' + k + '"' + (s.alert ? ' checked' : '') + (s.show ? '' : ' disabled') + ' style="width:18px;height:18px"></label>' +
        '</div>';
    }).join('') || 'مفيش خدمات متراقبة لسه';
  });
}
(function(){
  var body = document.getElementById('sx-body');
  body.addEventListener('change', function(e){
    var t = e.target;
    if(t.hasAttribute('data-vis')){
      sxPost('/api/status-visibility', {id: parseInt(t.getAttribute('data-vis'), 10), visible: t.checked});
    } else if(t.hasAttribute('data-sshow')){
      sxPost('/api/service-prefs', {key: decodeURIComponent(t.getAttribute('data-sshow')), show: t.checked}).then(sxLoad);
    } else if(t.hasAttribute('data-salert')){
      sxPost('/api/service-prefs', {key: decodeURIComponent(t.getAttribute('data-salert')), alert: t.checked});
    } else if(t.id === 'sx-mail'){
      sxPost('/api/service-prefs', {mail_alerts: t.checked});
    }
  });
  body.addEventListener('click', function(e){
    var t = e.target;
    if(t.hasAttribute('data-reset')){
      if(confirm('هيتمسح سجل الاب تايم للسيرفر ده ويبدأ العد من الأول. متأكد؟')){
        sxPost('/api/status-reset', {id: parseInt(t.getAttribute('data-reset'), 10)}).then(function(){ alert('تمت إعادة العد'); });
      }
    } else if(t.id === 'sx-reset-all'){
      if(confirm('هيتمسح سجل الاب تايم لكل السيرفرات ويبدأ العد من الأول. متأكد؟')){
        sxPost('/api/status-reset', {id: 'all'}).then(function(){ alert('تمت إعادة العد لكل السيرفرات'); });
      }
    }
  });
})();
"""

_WIDGET_HEAD = r"""
<div id="sx-root" dir="rtl" style="position:fixed;bottom:16px;left:16px;z-index:99999;font-family:system-ui,Tahoma,sans-serif;font-size:14px">
  <button id="sx-btn" style="background:#2563eb;color:#fff;border:0;border-radius:999px;padding:10px 16px;cursor:pointer;box-shadow:0 2px 10px rgba(0,0,0,.3);font:inherit">⚙ الحالة / الخدمات / التاسكات</button>
  <div id="sx-panel" style="display:none;position:absolute;bottom:48px;left:0;width:400px;max-width:92vw;max-height:75vh;overflow:auto;background:#fff;color:#1f2933;border:1px solid #cbd5e1;border-radius:12px;padding:14px;box-shadow:0 6px 24px rgba(0,0,0,.3)">
"""
_WIDGET_TOGGLE = r"""
(function(){
  var b = document.getElementById('sx-btn'), p = document.getElementById('sx-panel');
  b.addEventListener('click', function(){
    var open = p.style.display === 'none';
    p.style.display = open ? 'block' : 'none';
    if(open) sxLoad();
  });
})();
"""
DASHBOARD_WIDGET = _WIDGET_HEAD + PANEL_INNER + "</div></div><script>" + PANEL_JS + _WIDGET_TOGGLE + "</script>"

ADMIN_PAGE = (
    '<!DOCTYPE html><html lang="ar" dir="rtl"><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width, initial-scale=1">'
    "<title>إعدادات صفحة الحالة والتنبيهات</title></head>"
    '<body style="font-family:system-ui,Tahoma,sans-serif;background:#f5f7fa;margin:0">'
    '<div style="max-width:760px;margin:0 auto;padding:20px 16px"><h2>⚙ إعدادات صفحة الحالة والتنبيهات</h2>'
    '<div style="background:#fff;border:1px solid #e3e8ee;border-radius:12px;padding:16px">'
    + PANEL_INNER
    + "</div></div><script>"
    + PANEL_JS
    + "sxLoad();</script></body></html>"
)


# جرس التنبيهات: بيتحقن في أعلى اللوحة (بيتنادى من app.py)
BELL_WIDGET = "<script>" + r"""
(function () {
  if (window.__bellInit) return;
  window.__bellInit = true;
  var POS = 'top:10px;left:16px';   // مكان الجرس. لو بيغطي حاجة غيّره (مثلاً: top:10px;right:16px)
  var S = { open: false, data: null, sound: false, prev: -1, ctx: null, dismissed: '', basePad: '' };
  try { S.basePad = document.body.style.paddingTop || ''; } catch (e) {}
  var baseTitle = document.title;
  var seen = 0;
  try { S.sound = localStorage.getItem('bell_sound') === '1'; } catch (e) {}
  try { seen = parseInt(localStorage.getItem('bell_seen') || '0', 10) || 0; } catch (e) {}

  function esc(s) { var d = document.createElement('div'); d.textContent = (s == null ? '' : String(s)); return d.innerHTML; }

  // ---------- نص كل مشكلة ----------
  function ptext(p) {
    if (p.kind === 'server') return { icon: '\uD83D\uDD34', title: 'السيرفر واقع: ' + p.server, detail: p.detail || '' };
    if (p.kind === 'service') return { icon: '\uD83D\uDD34', title: 'خدمة واقعة: ' + p.name, detail: p.server + (p.detail ? ' \u2014 ' + p.detail : '') };
    if (p.kind === 'disk') return { icon: '\uD83D\uDFE0', title: 'مساحة الهارد ' + p.value + '%', detail: p.server + (p.detail ? ' \u2014 ' + p.detail : '') };
    if (p.kind === 'ram') return { icon: '\uD83D\uDFE0', title: 'استهلاك الرام ' + p.value + '%', detail: p.server + (p.detail ? ' \u2014 ' + p.detail : '') };
    if (p.kind === 'tasks') return { icon: '\uD83D\uDFE0', title: p.value + ' مهام متأخرة', detail: 'افتح صفحة التاسكات', href: '/tasks' };
    return { icon: '\u26A0\uFE0F', title: String(p.kind || ''), detail: p.server || '' };
  }
  var SEV = { CRITICAL: '#dc2626', WARNING: '#f59e0b', RECOVERY: '#16a34a' };

  function buildHtml(d) {
    var h = '<div style="padding:12px 14px;border-bottom:1px solid #e5e7eb;font-weight:700">التنبيهات <span style="font-weight:400;color:#6b7785;font-size:12px">(' + d.count + ' مشكلة حالياً)</span></div>';
    if (!d.problems.length) {
      h += '<div style="padding:16px 14px;color:#15803d">\u2714 كل حاجة تمام، مفيش مشاكل حالياً.</div>';
    } else {
      h += d.problems.map(function (p) {
        var t = ptext(p);
        var inner = '<div style="display:flex;gap:10px;padding:10px 14px;border-bottom:1px solid #f1f5f9"><span>' + t.icon + '</span><div style="flex:1;min-width:0"><div style="font-weight:600;font-size:14px;word-break:break-word">' + esc(t.title) + '</div><div style="font-size:12px;color:#6b7785;word-break:break-word">' + esc(t.detail) + '</div></div></div>';
        return t.href ? '<a href="' + esc(t.href) + '" style="text-decoration:none;color:inherit;display:block">' + inner + '</a>' : inner;
      }).join('');
    }
    h += '<div style="padding:7px 14px;background:#f3f4f6;font-size:12px;color:#6b7785">آخر الأحداث</div>';
    if (!d.incidents.length) {
      h += '<div style="padding:12px 14px;color:#6b7785;font-size:13px">لا توجد أحداث مسجلة.</div>';
    } else {
      h += d.incidents.map(function (i) {
        var isNew = i.id > seen;
        return '<div style="display:flex;gap:10px;padding:9px 14px;border-bottom:1px solid #f1f5f9"><span style="width:9px;height:9px;border-radius:50%;margin-top:5px;flex:none;background:' + (SEV[i.severity] || '#94a3b8') + '"></span>' +
          '<div style="flex:1;min-width:0"><div style="font-size:13px;word-break:break-word">' + esc(i.title) + (isNew ? ' <span style="font-size:10px;background:#fee2e2;color:#b91c1c;border-radius:999px;padding:1px 7px">جديد</span>' : '') + '</div>' +
          '<div style="font-size:11px;color:#6b7785;word-break:break-word">' + esc(i.server_name) + ' \u2014 ' + esc(i.created_at) + '</div></div></div>';
      }).join('');
    }
    return h;
  }

  // ---------- الجرس ----------
  var root = document.createElement('div');
  root.id = 'bell-root';
  root.style.cssText = 'position:fixed;' + POS + ';z-index:99998;font-family:inherit;direction:rtl;font-size:14px';
  root.innerHTML =
    '<style>#bell-btn{position:relative;width:42px;height:42px;border-radius:50%;border:1px solid #d1d5db;background:#fff;cursor:pointer;font-size:19px;line-height:1;box-shadow:0 2px 8px rgba(0,0,0,.15);padding:0}' +
    '#bell-btn:hover{background:#f9fafb}' +
    '#bell-badge{position:absolute;top:-7px;right:-7px;min-width:21px;height:21px;padding:0 5px;box-sizing:border-box;border-radius:999px;background:#e11d48;color:#fff;font-size:12px;font-weight:700;display:none;align-items:center;justify-content:center;box-shadow:0 0 0 4px rgba(34,197,94,.30)}' +
    '#bell-panel{display:none;position:absolute;top:50px;left:0;width:370px;max-width:92vw;max-height:72vh;overflow:auto;background:#fff;color:#1f2933;border:1px solid #d1d5db;border-radius:12px;box-shadow:0 10px 30px rgba(0,0,0,.25)}</style>' +
    '<button id="bell-btn" type="button" title="التنبيهات">\uD83D\uDD14<span id="bell-badge">0</span></button>' +
    '<div id="bell-panel"><div id="bell-body"></div>' +
    '<div style="display:flex;justify-content:space-between;align-items:center;padding:9px 14px;border-top:1px solid #e5e7eb;font-size:12px;color:#6b7785">' +
    '<label style="display:flex;align-items:center;gap:6px;cursor:pointer"><input type="checkbox" id="bell-snd" style="width:auto"> صوت عند ظهور مشكلة جديدة</label>' +
    '<a href="#" id="bell-refresh" style="color:#2563eb;text-decoration:none">تحديث</a></div></div>';
  document.body.appendChild(root);

  // ---------- شريط التنبيه الأحمر في أعلى الصفحة ----------
  function short(p) {
    if (p.kind === 'server') return 'السيرفر واقع: ' + p.server;
    if (p.kind === 'service') return 'خدمة واقعة: ' + p.name + ' (' + p.server + ')';
    if (p.kind === 'disk') return 'الهارد ' + p.value + '% \u2014 ' + p.server;
    if (p.kind === 'ram') return 'الرام ' + p.value + '% \u2014 ' + p.server;
    if (p.kind === 'tasks') return p.value + ' مهام متأخرة';
    return String(p.kind || '');
  }
  function sig(d) { return d.problems.map(function (p) { return p.kind + '|' + (p.server || '') + '|' + (p.name || ''); }).join(';'); }
  var banner = document.createElement('div');
  banner.id = 'bell-banner';
  banner.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:99997;display:none;align-items:center;gap:12px;padding:9px 16px;color:#fff;font-family:inherit;font-size:14px;font-weight:600;direction:rtl;box-shadow:0 2px 8px rgba(0,0,0,.25)';
  banner.innerHTML = '<span id="bn-text" style="flex:1;min-width:0;word-break:break-word"></span>' +
    '<a href="#" id="bn-more" style="color:#fff;text-decoration:underline;white-space:nowrap">التفاصيل</a>' +
    '<button type="button" id="bn-x" title="إخفاء" style="background:transparent;border:0;color:#fff;font-size:20px;line-height:1;cursor:pointer;padding:0 4px">\u00D7</button>';
  document.body.appendChild(banner);
  function updateBanner(d) {
    var show = d.count > 0 && S.dismissed !== sig(d);
    if (!show) {
      banner.style.display = 'none';
      document.body.style.paddingTop = S.basePad;
      root.style.top = '10px';
      return;
    }
    var crit = d.problems.some(function (p) { return p.level === 'critical'; });
    banner.style.background = crit ? '#dc2626' : '#d97706';
    var items = d.problems.slice(0, 3).map(short).join('   \u2022   ');
    document.getElementById('bn-text').textContent = '\u26A0 ' + d.count + ' مشكلة: ' + items + (d.count > 3 ? '   (+' + (d.count - 3) + ')' : '');
    banner.style.display = 'flex';
    var h = banner.offsetHeight || 42;
    document.body.style.paddingTop = h + 'px';
    root.style.top = (h + 10) + 'px';
  }
  var btn = document.getElementById('bell-btn'), badge = document.getElementById('bell-badge'), panel = document.getElementById('bell-panel'), body = document.getElementById('bell-body'), snd = document.getElementById('bell-snd');
  snd.checked = S.sound;

  function beep() {
    try {
      if (!S.ctx) S.ctx = new (window.AudioContext || window.webkitAudioContext)();
      var o = S.ctx.createOscillator(), g = S.ctx.createGain();
      o.type = 'sine'; o.frequency.value = 880; g.gain.value = 0.15;
      o.connect(g); g.connect(S.ctx.destination); o.start(); o.stop(S.ctx.currentTime + 0.35);
    } catch (e) {}
  }
  function maxIncident() { var m = 0; if (S.data) for (var i = 0; i < S.data.incidents.length; i++) if (S.data.incidents[i].id > m) m = S.data.incidents[i].id; return m; }
  function update() {
    var d = S.data; if (!d) return;
    badge.textContent = d.count > 99 ? '99+' : String(d.count);
    badge.style.display = d.count > 0 ? 'flex' : 'none';
    document.title = (d.count > 0 ? '(' + d.count + ') ' : '') + baseTitle;
    updateBanner(d);
    if (S.open) body.innerHTML = buildHtml(d);
    if (S.prev >= 0 && d.count > S.prev && S.sound) beep();
    S.prev = d.count;
  }
  function load() {
    fetch('/api/notifications', { cache: 'no-store', credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (d) { if (d && d.problems) { S.data = d; update(); } })
      .catch(function () {});
  }
  btn.addEventListener('click', function (e) {
    e.stopPropagation();
    S.open = !S.open;
    panel.style.display = S.open ? 'block' : 'none';
    if (S.open && S.data) {
      body.innerHTML = buildHtml(S.data);
      seen = maxIncident();   // بعد ما اتعرضت: نعتبرها اتشافت
      try { localStorage.setItem('bell_seen', String(seen)); } catch (e2) {}
    }
  });
  panel.addEventListener('click', function (e) { e.stopPropagation(); });
  document.addEventListener('click', function () { if (S.open) { S.open = false; panel.style.display = 'none'; } });
  snd.addEventListener('change', function () {
    S.sound = snd.checked;
    try { localStorage.setItem('bell_sound', S.sound ? '1' : '0'); } catch (e) {}
    if (S.sound) beep();   // بيفعّل الصوت في المتصفح ويجرّبه
  });
  document.getElementById('bell-refresh').addEventListener('click', function (e) { e.preventDefault(); load(); });
  document.getElementById('bn-more').addEventListener('click', function (e) {
    e.preventDefault(); e.stopPropagation();
    S.open = true; panel.style.display = 'block';
    if (S.data) body.innerHTML = buildHtml(S.data);
  });
  document.getElementById('bn-x').addEventListener('click', function () {
    if (S.data) { S.dismissed = sig(S.data); updateBanner(S.data); }
  });
  load();
  setInterval(load, 15000);
  window.__bell = { buildHtml: buildHtml, ptext: ptext, short: short, sig: sig, S: S };
})();

""" + "</script>"

# الجرس والشريط بيتحقنوا مع الودجت نفسه (من غير ما app.py يتغير)
DASHBOARD_WIDGET = DASHBOARD_WIDGET + BELL_WIDGET


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
        return None if not total else round(up * 100.0 / total, 3)

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
    @app.get("/status-settings", response_class=HTMLResponse)
    async def status_settings_page():
        return HTMLResponse(ADMIN_PAGE)

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

    # ───────────────────────── إعادة العد (Reset uptime) ─────────────────────────
    def _do_reset(sid):
        conn = get_db()
        if sid == "all":
            conn.execute("DELETE FROM uptime_checks")
            conn.execute("DELETE FROM uptime_daily")
            conn.execute("UPDATE server_state SET uptime_24h = 100.0")
        else:
            sid = int(sid)
            conn.execute("DELETE FROM uptime_checks WHERE server_id = ?", (sid,))
            conn.execute("DELETE FROM uptime_daily WHERE server_id = ?", (sid,))
            conn.execute("UPDATE server_state SET uptime_24h = 100.0 WHERE server_id = ?", (sid,))
        conn.commit()
        conn.close()

    @app.post("/api/status-reset")
    async def status_reset(request: Request):
        body = await request.json()
        await asyncio.to_thread(_do_reset, body.get("id"))
        return {"ok": True}

    # ───────────────────────── تفضيلات الخدمات ─────────────────────────
    @app.get("/api/service-prefs")
    def service_prefs_list():
        conn = get_db()
        rows = conn.execute("SELECT services_json FROM server_state").fetchall()
        conn.close()
        settings = get_settings_dict()
        prefs = parse_prefs(settings)
        seen = {}
        for r in rows:
            try:
                svcs = json.loads(r["services_json"] or "[]")
            except Exception:
                svcs = []
            for s in svcs:
                k = str(s.get("key", ""))
                if k and k not in seen:
                    seen[k] = s
        services = [
            {
                "key": k,
                "name": s.get("name", k),
                "show": svc_visible(s, prefs),
                "alert": svc_alert_enabled(s, prefs),
            }
            for k, s in sorted(seen.items(), key=lambda kv: str(kv[1].get("name", "")).lower())
        ]
        return {"services": services, "mail_alerts": settings.get("alert_on_spam", "1") == "1"}

    @app.post("/api/service-prefs")
    async def service_prefs_set(request: Request):
        body = await request.json()
        conn = get_db()
        if "mail_alerts" in body:
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES ('alert_on_spam', ?)",
                ("1" if body["mail_alerts"] else "0",),
            )
        key = body.get("key")
        if key:
            prefs = parse_prefs(get_settings_dict())
            p = prefs.get(key) if isinstance(prefs.get(key), dict) else {}
            if "show" in body:
                p["show"] = bool(body["show"])
            if "alert" in body:
                p["alert"] = bool(body["alert"])
            prefs[key] = p
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES ('service_prefs', ?)",
                (json.dumps(prefs, ensure_ascii=False),),
            )
        conn.commit()
        conn.close()
        return {"ok": True}

    # ───────────────────────── جرس التنبيهات (اللي فوق في اللوحة) ─────────────────────────
    @app.get("/api/notifications")
    def notifications():
        conn = get_db()
        rows = conn.execute(
            """SELECT s.id, s.name, s.ip, st.status, st.ssh_connected, st.ram_usage, st.ram_detail,
                      st.disk_usage, st.disk_detail, st.services_json
               FROM servers s LEFT JOIN server_state st ON s.id = st.server_id
               ORDER BY s.name COLLATE NOCASE"""
        ).fetchall()
        inc = conn.execute(
            "SELECT id, server_name, severity, service_name, title, details, created_at FROM incidents_logs ORDER BY id DESC LIMIT 15"
        ).fetchall()
        overdue = 0
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'tasks'").fetchone():
            today = datetime.now().strftime("%Y-%m-%d")
            overdue = conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE status != 'done' AND end_date != '' AND end_date < ?", (today,)
            ).fetchone()[0]
        conn.close()

        prefs = parse_prefs(get_settings_dict())
        problems = []
        for r in rows:
            status = r["status"] or "UNKNOWN"
            if status == "DOWN":
                problems.append({"level": "critical", "kind": "server", "server": r["name"], "detail": r["ip"]})
                continue
            if status != "UP":
                continue
            try:
                svcs = json.loads(r["services_json"] or "[]")
            except Exception:
                svcs = []
            for sv in svcs:
                if sv.get("status") == "DOWN" and svc_visible(sv, prefs) and svc_alert_enabled(sv, prefs):
                    problems.append({"level": "critical", "kind": "service", "server": r["name"],
                                     "name": sv.get("name", sv.get("key", "")), "detail": sv.get("detail", "")})
            if r["ssh_connected"]:
                if (r["disk_usage"] or 0) >= DISK_ALERT_PCT:
                    problems.append({"level": "warning", "kind": "disk", "server": r["name"],
                                     "value": round(r["disk_usage"], 1), "detail": r["disk_detail"] or ""})
                if (r["ram_usage"] or 0) >= RAM_ALERT_PCT:
                    problems.append({"level": "warning", "kind": "ram", "server": r["name"],
                                     "value": round(r["ram_usage"], 1), "detail": r["ram_detail"] or ""})
        if overdue:
            problems.append({"level": "warning", "kind": "tasks", "value": int(overdue)})
        return {
            "count": len(problems),
            "problems": problems,
            "incidents": [dict(i) for i in inc],
            "server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

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
