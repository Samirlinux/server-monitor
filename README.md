# ⚡ Linux Server, Uptime, Services & Mail/Spam Monitor — Version 3.5

> **Developed & Engineered by Eng. Samir Abo El-Khair | Version 3.5**

![Version](https://img.shields.io/badge/Version-1.0-10b981)
![Author](https://img.shields.io/badge/Developer-Eng._Samir_Abo_El--Khair-0ea5e9)
![Python](https://img.shields.io/badge/Python-3.10%2B-blue?logo=python)
![FastAPI](https://img.shields.io/badge/FastAPI-RealTime_5s-009688?logo=fastapi)
![Linux](https://img.shields.io/badge/Linux-Systemd_%26_SSH-FCC624?logo=linux)
![Alerts](https://img.shields.io/badge/Alerts-Telegram_%26_Slack-26A5E4?logo=telegram)

نظام متكامل باللغة العربية لمراقبة جميع السيرفرات من لوحة تحكم واحدة، مصمم ليعمل على أي سيرفر لينكس على مدار الساعة (24/7) مع فحص لحظي كل **5 ثوانٍ** وتنبيهات فورية على **Telegram** و **Slack**.

---

## 🌟 المميزات الرئيسية

1. **مراقبة فورية بمجرد إضافة الـ IP (أو مع SSH):**
   - **بمجرد كتابة الـ IP فقط:** يفحص كل 5 ثوانٍ بورتات واستجابة **Apache / Web (`80`, `443`)**، **قواعد البيانات MySQL / MariaDB (`3306`) و PostgreSQL (`5432`)**، **إرسال البريد SMTP (`25`, `587`, `465`)**، و**SSH (`22`)**، بالإضافة إلى فحص وجود الـ IP في **قوائم السبام العالمية (Spamhaus, SpamCop, Barracuda, SORBS)**.
   - **عند إضافة بيانات الـ SSH (اختياري):** يقرأ تلقائياً **عدد الرسائل المرسلة (Sent)**، **طابور الإرسال (Mail Queue)**، **الرسائل المرتدة والمؤجلة والسبام (Bounced / Deferred / Rejected)**، **أكثر الإيميلات إرسالاً (Top Senders)**، وحالة خدمات `systemd` الفعلية (`apache2`, `httpd`, `lshttpd`, `nginx`, `mysql`, `mariadb`, `postgresql`, `redis`, `postfix`, `exim`, `dovecot`, `named`, `cpanel`, `fail2ban`) مع استهلاك المعالج والرامات والهارد.
2. **صفحة مستقلة لكل سيرفر (`/server/{id}`):**
   - تعرض مؤشر الـ Uptime لآخر 24 ساعة وآخر 7 أيام، وشريط النبض اللحظي (60 نبضة فحص كل 5 ثوانٍ).
   - تعرض تفاصيل البريد والـ Spam وفحص قوائم الحظر (DNSBL) مع أزرار **Flush Mail Queue** و **Clear Deferred/Spam Queue**.
   - **عارض لوجات مباشر (Live Logs Terminal):** تبويبات لعرض لوجات البريد (`mail.log` / `exim_mainlog`)، لوجات الأباتشي (`apache2/error.log`)، لوجات قواعد البيانات (`mysql/error.log`)، تفاصيل الـ Mail Queue، ولوجات النظام مع البحث الفوري.
   - إمكانية عمل **Restart** لأي خدمة مباشرة من المتصفح.
3. **تنبيهات فورية خلال 5 ثوانٍ على Telegram و Slack:**
   - يرسل إشعاراً مفصلاً فور سقوط أي سيرفر أو توقف أي خدمة أو عند اكتشاف Spam أو ارتفاع طابور البريد، وكذلك عند عودة الخدمة للعمل.

---

## 🚀 التسطيب المباشر على أي سيرفر Ubuntu / Linux من GitHub (أمر واحد)

يمكنك تسطيب النظام وتشغيله 24/7 على أي سيرفر Ubuntu أو Linux بالأمر التالي:

```bash
git clone https://github.com/Samirlinux/server-monitor.git /opt/server-monitor
cd /opt/server-monitor
sed -i 's/\r$//' install.sh
chmod +x install.sh
sudo ./install.sh
```

سيقوم الاسكريبت تلقائياً بتسطيب المتطلبات وإنشاء خدمة `systemd` باسم `server-monitor` تعمل تلقائياً حتى لو أُعيد تشغيل السيرفر، وستفتح اللوحة على البورت `8080`:
```
http://YOUR_SERVER_IP:8080
```

### أو التشغيل عبر Docker:
```bash
docker compose up -d --build
```

---

## 👨‍💻 Author & Credits
**Developed & Engineered by Eng. Samir Abo El-Khair — Version 3.5**

