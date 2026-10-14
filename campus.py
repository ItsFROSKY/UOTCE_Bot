# batch stuff: follow folders (new file alerts), announcements, schedule
# all saved in redis as small json, no database needed
import json
import logging
import os
import re
import time
import uuid
from datetime import date, datetime, timedelta

from telebot import types
from telebot.apihelper import ApiTelegramException

import drive_index as di
from config import ADMIN_ID, Drive_ID, Drive_service, WEBAPP_URL, bot, redis
from functions_bot import safe_api_call
from tasks_source import BAGHDAD

USERS_KEY = "miniapp:users"
ADMINS_KEY = "miniapp:admins"      # admins added from the bot (owners are in ADMIN_IDS on vercel)
REMIND_KEY = "miniapp:remind"
GROUP_KEY = "miniapp:group"
ANN_KEY = "miniapp:announcements"
SCHED_KEY = "miniapp:schedule"
OVR_KEY = "miniapp:schedule:overrides"
KNOWN_KEY = "miniapp:known_files"
SUB_KEY = "miniapp:sub:{folder}"   # users following a folder
SUBS_KEY = "miniapp:subs:{uid}"    # folders a user follows
BOTNAME_KEY = "miniapp:botname"
PINNED_KEY = "miniapp:pinned"      # the dashboard message pinned in the group
EXAMS_KEY = "miniapp:exams"
ABS_KEY = "miniapp:abs:{uid}"
SECTION_KEY = "miniapp:section:{uid}"

# ministry rules: fail if absence without excuse > 10% of the subject hours,
# or > 15% with an excuse accepted by the college. first notice at 5%
ABS_LIMIT = float(os.environ.get("ABSENCE_LIMIT", 10))
ABS_LIMIT_EXCUSED = float(os.environ.get("ABSENCE_LIMIT_EXCUSED", 15))
ABS_NOTICE = float(os.environ.get("ABSENCE_NOTICE", 5))
SEMESTER_WEEKS = int(os.environ.get("SEMESTER_WEEKS", 15))
MAX_SUBS = 60

DAYS = ["الأحد", "الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت"]  # 0 = Sunday
MONTHS = ["كانون الثاني", "شباط", "آذار", "نيسان", "أيار", "حزيران", "تموز", "آب",
          "أيلول", "تشرين الأول", "تشرين الثاني", "كانون الأول"]
ANN_KINDS = {"general": ("📢", "إعلان"), "postpone": ("⏰", "تأجيل"), "cancel": ("❌", "إلغاء"),
             "room": ("🚪", "تغيير قاعة"), "exam": ("📝", "امتحان")}



def now():
    return datetime.now(BAGHDAD)


def _load(key, default):
    raw = redis.get(key)
    return json.loads(raw) if raw else default


def _save(key, value, **kw):
    redis.set(key, json.dumps(value, ensure_ascii=False, separators=(",", ":")), **kw)


def day_index(d: date):
    return (d.weekday() + 1) % 7  # python monday=0, we use sunday=0


def nice_date(d: date):
    return f"{DAYS[day_index(d)]} {d.day} {MONTHS[d.month - 1]}"


_botname = {"v": None}


def bot_username():
    if _botname["v"]:
        return _botname["v"]
    name = redis.get(BOTNAME_KEY)
    if not name:
        name = bot.get_me().username
        redis.set(BOTNAME_KEY, name, ex=7 * 86400)
    _botname["v"] = name
    return name


def app_button(text, param="", private=True):
    # web_app buttons dont work in groups, so groups get a t.me/bot?startapp link
    # (needs the Main Mini App set in BotFather)
    if private and WEBAPP_URL:
        return types.InlineKeyboardButton(text, web_app=types.WebAppInfo(WEBAPP_URL + (f"#{param}" if param else "")))
    return types.InlineKeyboardButton(text, url=f"https://t.me/{bot_username()}?startapp={param or 'files'}")


def markup(*buttons):
    kb = types.InlineKeyboardMarkup()
    for b in buttons:
        if b is not None:
            kb.add(b)
    return kb


def forget_user(uid):
    uid = str(uid)
    redis.srem(USERS_KEY, uid)
    redis.srem(REMIND_KEY, uid)
    for folder in redis.smembers(SUBS_KEY.format(uid=uid)) or []:
        redis.srem(SUB_KEY.format(folder=folder), uid)
    redis.delete(SUBS_KEY.format(uid=uid))


def send_to_users(uids, text, make_markup=None):
    sent = 0
    for uid in uids:
        try:
            safe_api_call(bot.send_message, int(uid), text,
                          reply_markup=make_markup(uid) if make_markup else None,
                          disable_web_page_preview=True)
            sent += 1
        except ApiTelegramException as e:
            # 403 = blocked the bot or never started it
            if e.error_code == 403 or "chat not found" in str(getattr(e, "description", "")):
                forget_user(uid)
        except Exception:
            logging.exception("broadcast to %s failed", uid)
        time.sleep(0.04)  # telegram limit ~30 msg/s
    return sent



def set_group(chat_id, thread_id=None):
    _save(GROUP_KEY, {"chat_id": chat_id, "thread_id": thread_id})


def clear_group():
    redis.delete(GROUP_KEY)


def post_to_group(text, reply_markup=None):
    g = _load(GROUP_KEY, None)
    if not g:
        return False
    try:
        extra = {"message_thread_id": g["thread_id"]} if g.get("thread_id") else {}
        safe_api_call(bot.send_message, g["chat_id"], text, reply_markup=reply_markup,
                      disable_web_page_preview=True, **extra)
        return True
    except Exception:
        logging.exception("posting to group failed")
        return False



def subscribe(uid, folder_id):
    if (redis.scard(SUBS_KEY.format(uid=uid)) or 0) >= MAX_SUBS:
        raise ValueError("too_many_subscriptions")
    redis.sadd(SUB_KEY.format(folder=folder_id), uid)
    redis.sadd(SUBS_KEY.format(uid=uid), folder_id)


def unsubscribe(uid, folder_id):
    redis.srem(SUB_KEY.format(folder=folder_id), uid)
    redis.srem(SUBS_KEY.format(uid=uid), folder_id)


def user_subscriptions(uid):
    return set(redis.smembers(SUBS_KEY.format(uid=uid)) or [])


def _ancestors(idx, item_id):
    out, node = [], idx["nodes"].get(item_id)
    while node and node.get("parent"):
        out.append(node["parent"])
        node = idx["nodes"].get(node["parent"])
    return out


def check_new_files(idx=None):
    # compare drive with last time and notify followers
    # first run only saves the current files so nobody gets spammed
    idx = idx or di.get_index(Drive_service, redis, Drive_ID, force=True)
    current = {i for i, n in idx["nodes"].items() if n["kind"] == "file"}
    raw = redis.get(KNOWN_KEY)
    _save(KNOWN_KEY, sorted(current))  # save first so a crash doesnt resend everything
    if raw is None:
        return {"new": 0, "seeded": len(current)}
    new = sorted(current - set(json.loads(raw)), key=lambda i: idx["nodes"][i].get("modified") or "")
    if not new:
        return {"new": 0, "notified": 0}

    per_user = {}
    for fid in new:
        for folder in _ancestors(idx, fid):
            for uid in redis.smembers(SUB_KEY.format(folder=folder)) or []:
                per_user.setdefault(uid, [])
                if fid not in per_user[uid]:
                    per_user[uid].append(fid)

    def where(fid):
        return di.public(idx["nodes"][fid], idx, with_path=True).get("path", "")

    notified = 0
    for uid, fids in per_user.items():
        if len(fids) == 1:
            node = idx["nodes"][fids[0]]
            text = f"📄 ملف جديد\n\n{node['name']}\n📁 {where(fids[0])}"
            kb = markup(types.InlineKeyboardButton("📥 استلم الملف", callback_data=f"file:{node['id']}")
                        if not node.get("native") else None,
                        app_button("📂 افتح المجلد", f"f-{node['parent']}"))
        else:
            lines = [f"• {idx['nodes'][f]['name']}" for f in fids[:8]]
            if len(fids) > 8:
                lines.append(f"و {len(fids) - 8} ملفات ثانية")
            text = f"📚 انضافت {len(fids)} ملفات جديدة\n\n" + "\n".join(lines)
            kb = markup(app_button("📂 افتح المكتبة", f"f-{idx['nodes'][fids[0]]['parent']}"))
        notified += send_to_users([uid], text, lambda _u: kb)

    lines = [f"• {idx['nodes'][f]['name']} - {where(f)}" for f in new[:15]]
    if len(new) > 15:
        lines.append(f"و {len(new) - 15} ملفات ثانية")
    post_to_group(f"📚 ملفات جديدة بالمكتبة ({len(new)})\n\n" + "\n".join(lines),
                  markup(app_button("📂 افتح المكتبة", f"f-{idx['nodes'][new[0]]['parent']}", private=False)))
    return {"new": len(new), "notified": notified}



def active_announcements():
    t = time.time()
    return sorted((a for a in _load(ANN_KEY, []) if a["expires"] > t), key=lambda a: -a["created"])


def add_announcement(text, kind="general", days=3, by=None, expires=None):
    text = (text or "").strip()
    if not text:
        raise ValueError("empty")
    t = time.time()
    a = {"id": uuid.uuid4().hex[:10], "text": text[:1000], "kind": kind if kind in ANN_KINDS else "general",
         "created": t, "expires": expires or t + max(1, min(int(days), 30)) * 86400, "by": by}
    keep = [x for x in _load(ANN_KEY, []) if x["expires"] > t][-29:]
    _save(ANN_KEY, keep + [a])
    return a


def remove_announcement(aid):
    _save(ANN_KEY, [a for a in _load(ANN_KEY, []) if a["id"] != aid])


def broadcast_announcement(a):
    icon, label = ANN_KINDS[a["kind"]]
    text = f"{icon} {label}\n\n{a['text']}"
    group = post_to_group(text, markup(app_button("📅 افتح الجدول", "schedule", private=False)))
    users = send_to_users(redis.smembers(USERS_KEY) or [], text,
                          lambda _u: markup(app_button("📅 افتح الجدول", "schedule")))
    refresh_dashboard()
    return {"users": users, "group": group}



_DAY_ALIASES = {di.normalize(d): i for i, d in enumerate(DAYS)}
_DAY_ALIASES.update({di.normalize(d)[2:]: i for i, d in enumerate(DAYS)})  # without "ال"
_DAY_ALIASES.update({"اتنين": 1, "الاتنين": 1, "جمعه": 5})
_TIME = re.compile(r"^([01]?\d|2[0-3])[:.]([0-5]\d)$")


def parse_day(word):
    return _DAY_ALIASES.get(di.normalize(word))


def norm_time(s):
    m = _TIME.match((s or "").strip())
    if not m:
        raise ValueError(f"وقت غير صحيح: {s}")
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def clean_class(d):
    day = d.get("day")
    if isinstance(day, str):
        day = parse_day(day) if not day.isdigit() else int(day)
    if not isinstance(day, int) or not 0 <= day <= 6:
        raise ValueError("اليوم غير صحيح")
    start, end = norm_time(d.get("start")), norm_time(d.get("end"))
    if end <= start:
        raise ValueError("وقت النهاية لازم يكون بعد البداية")
    subject = str(d.get("subject") or "").strip()
    if not subject:
        raise ValueError("اسم المادة مطلوب")
    out = {"id": d.get("id") or uuid.uuid4().hex[:8], "day": day, "start": start, "end": end,
           "subject": subject[:80]}
    for key, limit in (("room", 40), ("kind", 20), ("section", 12), ("teacher", 60)):
        val = str(d.get(key) or "").strip()[:limit]
        if val:
            out[key] = val
    return out


def get_schedule():
    return sorted(_load(SCHED_KEY, []), key=lambda c: (c["day"], c["start"]))


def upsert_class(d):
    c = clean_class(d)
    classes = [x for x in get_schedule() if x["id"] != c["id"]] + [c]
    _save(SCHED_KEY, classes)
    refresh_dashboard()
    return c


def delete_class(cid):
    _save(SCHED_KEY, [c for c in get_schedule() if c["id"] != cid])
    _save(OVR_KEY, {k: v for k, v in get_overrides().items() if not k.startswith(cid + ":")})


def replace_schedule(classes):
    _save(SCHED_KEY, classes)
    _save(OVR_KEY, {})
    refresh_dashboard()


def parse_schedule_text(text):
    # one class per line: اليوم | 08:30-10:30 | المادة | القاعة | نظري/عملي | الشعبة
    classes, errors = [], []
    for n, line in enumerate((text or "").splitlines(), 1):
        if not line.strip():
            continue
        parts = [p.strip() for p in line.split("|")]
        try:
            if len(parts) < 3:
                raise ValueError("ناقص حقول")
            start, _, end = parts[1].replace("–", "-").partition("-")
            classes.append(clean_class({"day": parts[0], "start": start, "end": end, "subject": parts[2],
                                        "room": parts[3] if len(parts) > 3 else "",
                                        "kind": parts[4] if len(parts) > 4 else "",
                                        "section": parts[5] if len(parts) > 5 else ""}))
        except ValueError as e:
            errors.append(f"سطر {n}: {e}")
    return classes, errors


def get_overrides():
    cutoff = (now().date() - timedelta(days=2)).isoformat()
    return {k: v for k, v in _load(OVR_KEY, {}).items() if k.split(":", 1)[1] >= cutoff}


OVERRIDE_TEXT = {"postponed": ("postpone", "تأجلت"), "cancelled": ("cancel", "انلغت"),
                 "room": ("room", "تغيرت قاعة")}


def set_override(cid, day: str, status, room=None, note=None, announce=True, by=None):
    cls = next((c for c in get_schedule() if c["id"] == cid), None)
    if not cls:
        raise LookupError("class_not_found")
    d = date.fromisoformat(day)
    if day_index(d) != cls["day"]:
        raise ValueError("هذا التاريخ مو يوم المحاضرة")
    ovr = get_overrides()
    key = f"{cid}:{day}"
    if status == "clear":
        ovr.pop(key, None)
        _save(OVR_KEY, ovr)
        refresh_dashboard()
        return None
    if status not in OVERRIDE_TEXT:
        raise ValueError("حالة غير معروفة")
    if status == "room" and not (room or "").strip():
        raise ValueError("اكتب القاعة الجديدة")
    entry = {"status": status}
    if room:
        entry["room"] = room.strip()[:40]
    if note:
        entry["note"] = note.strip()[:200]
    ovr[key] = entry
    _save(OVR_KEY, ovr)
    refresh_dashboard()

    if announce:
        kind, verb = OVERRIDE_TEXT[status]
        text = f"{verb} محاضرة {cls['subject']}"
        if cls.get("section"):
            text += f" (شعبة {cls['section']})"
        text += f"\n📅 {nice_date(d)} · {cls['start']}"
        if status == "room":
            text += f"\n🚪 القاعة الجديدة: {entry['room']}"
        if entry.get("note"):
            text += f"\n📝 {entry['note']}"
        end_of_day = datetime.combine(d + timedelta(days=1), datetime.min.time(), BAGHDAD).timestamp()
        a = add_announcement(text, kind, by=by, expires=end_of_day)
        return a
    return None


def classes_on(d: date):
    ovr = get_overrides()
    out = []
    for c in get_schedule():
        if c["day"] == day_index(d):
            out.append({**c, **({"override": ovr[f"{c['id']}:{d.isoformat()}"]}
                                if f"{c['id']}:{d.isoformat()}" in ovr else {})})
    return out


def day_text(d: date):
    rows = classes_on(d)
    if not rows:
        return f"📅 {nice_date(d)}\nماكو محاضرات"
    lines = [f"📅 {nice_date(d)}"]
    for c in rows:
        o = c.get("override", {})
        room = o.get("room") or c.get("room") or ""
        mark = {"cancelled": " ❌ ملغاة", "postponed": " ⏰ مؤجلة", "room": " 🚪 تغيرت القاعة"}.get(o.get("status"), "")
        sec = f" · {c['section']}" if c.get("section") else ""
        lines.append(f"{c['start']}–{c['end']}  {c['subject']}{sec}" + (f" · {room}" if room else "") + mark)
    return "\n".join(lines)


# pinned dashboard in the group: buttons that open the mini app + today's lectures
# it edits itself on every cron tick / announcement / schedule change

def dashboard_markup():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(app_button("📚 المكتبة", "files", private=False))
    kb.add(app_button("📅 الجدول", "schedule", private=False), app_button("📋 الواجبات", "tasks", private=False))
    kb.add(app_button("⭐ المفضلة والمتابعة", "favs", private=False))
    return kb


def dashboard_text():
    today = now().date()
    rows, label, d = classes_on(today), "اليوم", today
    # after the last lecture show tomorrow instead
    if not rows or all(c["end"] <= now().strftime("%H:%M") for c in rows):
        d, label = today + timedelta(days=1), "باچر"
        rows = classes_on(d)
    lines = ["📚 بوت الدفعة - هندسة الحاسوب", "دوس أي زر حتى ينفتح التطبيق 👇", ""]
    soon = [e for e in upcoming_exams() if e["days_left"] <= 21][:3]
    if soon:
        lines.append("📝 الامتحانات القادمة")
        for e in soon:
            lines.append(f"• {e['subject']} ({e['kind']}) - {when_text(e['days_left'])} · {nice_date(date.fromisoformat(e['date']))}"
                         + (f" {e['time']}" if e.get("time") else ""))
        lines.append("")
    lines.append(f"📅 محاضرات {label} ({nice_date(d)})")
    if rows:
        for c in rows:
            o = c.get("override", {})
            room = o.get("room") or c.get("room") or ""
            mark = {"cancelled": " ❌ ملغاة", "postponed": " ⏰ مؤجلة", "room": " 🚪"}.get(o.get("status"), "")
            sec = f" ({c['section']})" if c.get("section") else ""
            lines.append(f"{c['start']}  {c['subject']}{sec}" + (f" · {room}" if room else "") + mark)
    else:
        lines.append("ماكو محاضرات 😌")
    import fun  # here to avoid a circular import
    poll = fun.open_poll_line()
    if poll:
        lines += ["", poll]
    anns = active_announcements()
    if anns:
        icon, _ = ANN_KINDS[anns[0]["kind"]]
        first = anns[0]["text"].splitlines()[0]
        lines += ["", f"{icon} آخر إعلان: {first[:120]}"]
    lines += ["", "بالتطبيق: كل ملفات المواد ويا بحث، الجدول والإعلانات، الواجبات،",
              "وتكدر تتابع أي مادة حتى يوصلك إشعار بكل محاضرة جديدة.",
              "", "Programmed by Taha AlAnbagi #1"]
    return "\n".join(lines)


def pin_dashboard(chat_id, thread_id=None):
    extra = {"message_thread_id": thread_id} if thread_id else {}
    text = dashboard_text()
    msg = safe_api_call(bot.send_message, chat_id, text, reply_markup=dashboard_markup(),
                        disable_web_page_preview=True, **extra)
    pinned = True
    try:
        bot.pin_chat_message(chat_id, msg.message_id, disable_notification=True)
    except ApiTelegramException:
        pinned = False  # bot isnt admin / no pin permission
    _save(PINNED_KEY, {"chat_id": chat_id, "message_id": msg.message_id, "text": text})
    return pinned


def refresh_dashboard():
    d = _load(PINNED_KEY, None)
    if not d:
        return False
    text = dashboard_text()
    if text == d.get("text"):
        return False  # nothing changed, dont spam telegram with edits
    try:
        bot.edit_message_text(text, d["chat_id"], d["message_id"], reply_markup=dashboard_markup(),
                              disable_web_page_preview=True)
    except ApiTelegramException as e:
        if "not modified" not in str(getattr(e, "description", "")):
            logging.warning(f"dashboard edit failed: {e}")
            if "not found" in str(getattr(e, "description", "")):
                redis.delete(PINNED_KEY)  # someone deleted it
            return False
    d["text"] = text
    _save(PINNED_KEY, d)
    return True


# exams

EXAM_KINDS = ["شهري", "نهائي", "كوز", "عملي", "تكميلي"]


def when_text(days):
    if days <= 0:
        return "اليوم!"
    if days == 1:
        return "باچر"
    if days == 2:
        return "بعد يومين"
    return f"بعد {days} أيام" if days <= 10 else f"بعد {days} يوم"


def clean_exam(d):
    subject = str(d.get("subject") or "").strip()[:80]
    if not subject:
        raise ValueError("اسم المادة مطلوب")
    try:
        day = date.fromisoformat(str(d.get("date") or ""))
    except ValueError:
        raise ValueError("التاريخ غير صحيح")
    out = {"id": d.get("id") or uuid.uuid4().hex[:8], "subject": subject, "date": day.isoformat(),
           "kind": str(d.get("kind") or "شهري").strip()[:20] or "شهري"}
    if d.get("time"):
        out["time"] = norm_time(d["time"])
    for key, limit in (("room", 40), ("note", 200), ("section", 12)):
        val = str(d.get(key) or "").strip()[:limit]
        if val:
            out[key] = val
    return out


def get_exams():
    return sorted(_load(EXAMS_KEY, []), key=lambda e: (e["date"], e.get("time", "")))


def upcoming_exams():
    today = now().date()
    out = []
    for e in get_exams():
        left = (date.fromisoformat(e["date"]) - today).days
        if left >= 0:
            out.append({**e, "days_left": left})
    return out


def upsert_exam(d):
    e = clean_exam(d)
    _save(EXAMS_KEY, [x for x in get_exams() if x["id"] != e["id"]] + [e])
    refresh_dashboard()
    return e


def delete_exam(eid):
    _save(EXAMS_KEY, [x for x in get_exams() if x["id"] != eid])
    refresh_dashboard()


def parse_exam_line(text):
    # المادة | شهري | 2026-10-25 | 09:00 | قاعة 3   (time and room optional, date can be 25/10)
    parts = [p.strip() for p in (text or "").split("|")]
    if len(parts) < 3:
        raise ValueError("ناقص حقول")
    from tasks_source import parse_due
    due = parse_due(parts[2])
    if not due:
        raise ValueError("التاريخ غير صحيح")
    return clean_exam({"subject": parts[0], "kind": parts[1], "date": due.date().isoformat(),
                       "time": parts[3] if len(parts) > 3 and parts[3] else None,
                       "room": parts[4] if len(parts) > 4 else ""})


def exam_text(e, label):
    t = f"📝 امتحان {e['subject']} ({e['kind']}) {label}\n📅 {nice_date(date.fromisoformat(e['date']))}"
    if e.get("time"):
        t += f" · {e['time']}"
    if e.get("room"):
        t += f"\n🚪 {e['room']}"
    if e.get("note"):
        t += f"\n📝 {e['note']}"
    return t


def run_exam_reminders():
    sent = 0
    for e in upcoming_exams():
        for days, label in ((1, "باچر"), (7, "بعد أسبوع")):  # smallest first
            if e["days_left"] > days:
                continue
            if not redis.set(f"rem:exam:{e['id']}:{days}", "1", nx=True, ex=30 * 86400):
                break
            redis.set(f"rem:exam:{e['id']}:7", "1", nx=True, ex=30 * 86400)
            text = exam_text(e, label if e["days_left"] == days else when_text(e["days_left"]))
            post_to_group(text, markup(app_button("📅 الجدول", "schedule", private=False)))
            send_to_users(redis.smembers(USERS_KEY) or [], text, lambda _u: markup(app_button("📅 الجدول", "schedule")))
            sent += 1
            break
    return {"exams_notified": sent}


# absences (each student keeps his own)

def get_section(uid):
    return redis.get(SECTION_KEY.format(uid=uid)) or ""


def set_section(uid, section):
    section = (section or "").strip()[:12]
    if section:
        redis.set(SECTION_KEY.format(uid=uid), section)
    else:
        redis.delete(SECTION_KEY.format(uid=uid))
    return section


def _hours(c):
    return (int(c["end"][:2]) * 60 + int(c["end"][3:]) - int(c["start"][:2]) * 60 - int(c["start"][3:])) / 60


def get_absences(uid):
    return _load(ABS_KEY.format(uid=uid), [])


def _subject_classes(subject, section):
    classes = [c for c in get_schedule() if c["subject"] == subject]
    sections = sorted({c["section"] for c in classes if c.get("section")})
    if sections and section not in sections:
        section = sections[0]  # no section picked: guess the first one
    return [c for c in classes if not c.get("section") or c["section"] == section]


def absence_summary(uid):
    section = get_section(uid)
    entries = get_absences(uid)
    subjects = sorted({c["subject"] for c in get_schedule()} | {a["subject"] for a in entries})
    out = []
    for subj in subjects:
        weekly = _subject_classes(subj, section)
        total = round(sum(_hours(c) for c in weekly) * SEMESTER_WEEKS, 2)
        mine = [a for a in entries if a["subject"] == subj]
        unexc = sum(a["hours"] for a in mine if not a.get("excused"))
        exc = sum(a["hours"] for a in mine if a.get("excused"))
        pct = round(unexc / total * 100, 1) if total else 0
        pct_all = round((unexc + exc) / total * 100, 1) if total else 0
        lecture = min((_hours(c) for c in weekly), default=2) or 2
        left_hours = min(total * ABS_LIMIT / 100 - unexc, total * ABS_LIMIT_EXCUSED / 100 - unexc - exc)
        left = max(0, int((left_hours + 1e-9) // lecture)) if total else None
        if total and (pct > ABS_LIMIT or pct_all > ABS_LIMIT_EXCUSED):
            status = "fail"
        elif total and left == 0:
            status = "danger"
        elif pct >= ABS_NOTICE or pct_all >= ABS_NOTICE:
            status = "warn"
        else:
            status = "ok"
        out.append({"subject": subj, "count": len(mine), "hours": unexc + exc, "excused_hours": exc,
                    "total_hours": total, "pct": pct, "pct_all": pct_all, "left": left, "status": status})
    out.sort(key=lambda r: (-r["pct_all"], r["subject"]))
    return {"subjects": out, "section": section,
            "limits": {"unexcused": ABS_LIMIT, "excused": ABS_LIMIT_EXCUSED, "notice": ABS_NOTICE, "weeks": SEMESTER_WEEKS}}


STATUS_RANK = {"ok": 0, "warn": 1, "danger": 2, "fail": 3}


def add_absence(uid, class_id, day: str, excused=False):
    cls = next((c for c in get_schedule() if c["id"] == class_id), None)
    if not cls:
        raise LookupError("class_not_found")
    d = date.fromisoformat(day)
    if day_index(d) != cls["day"]:
        raise ValueError("هذا التاريخ مو يوم المحاضرة")
    if d > now().date():
        raise ValueError("ما تكدر تسجل غياب ليوم بعده ما صار")
    if d < now().date() - timedelta(days=200):
        raise ValueError("التاريخ قديم هواية")
    entries = get_absences(uid)
    if any(a["class_id"] == class_id and a["date"] == day for a in entries):
        raise ValueError("مسجل غياب لهذي المحاضرة بهذا اليوم")
    if len(entries) >= 300:
        raise ValueError("too_many")
    before = {r["subject"]: r["status"] for r in absence_summary(uid)["subjects"]}
    entries.append({"id": uuid.uuid4().hex[:8], "class_id": class_id, "subject": cls["subject"],
                    "date": day, "start": cls["start"], "hours": round(_hours(cls), 2), "excused": bool(excused)})
    _save(ABS_KEY.format(uid=uid), entries)
    summary = absence_summary(uid)
    row = next(r for r in summary["subjects"] if r["subject"] == cls["subject"])
    warning = None
    if STATUS_RANK[row["status"]] > STATUS_RANK[before.get(cls["subject"], "ok")]:
        warning = absence_warning(row)
        try:
            bot.send_message(int(uid), warning)
        except Exception:
            pass
    return summary, warning


def absence_warning(row):
    lim = ABS_LIMIT
    if row["status"] == "fail":
        return f"⛔ غيابك بمادة {row['subject']} وصل {row['pct']}% وتجاوز الحد ({lim:g}%). راجع القسم بسرعة."
    if row["status"] == "danger":
        return f"🚨 غيابك بمادة {row['subject']} وصل {row['pct']}%. أي غياب ثاني بدون عذر يرسبك بالمادة (الحد {lim:g}%)."
    left = row["left"]
    lefts = "محاضرة وحدة" if left == 1 else f"{left} محاضرات"
    return f"⚠️ انتبه: غيابك بمادة {row['subject']} وصل {row['pct']}% من أصل {lim:g}%. باقيلك {lefts} بس."


def remove_absence(uid, aid):
    _save(ABS_KEY.format(uid=uid), [a for a in get_absences(uid) if a["id"] != aid])


# admins
# owners = ADMIN_IDS env var on vercel, they can add/remove other admins from the bot
# a telegram id never changes and telegram signs it, so nobody can pretend to be an admin

def is_owner(uid):
    return uid is not None and int(uid) in ADMIN_ID


def is_admin_id(uid):
    if uid is None:
        return False
    if int(uid) in ADMIN_ID:
        return True
    try:
        return bool(redis.sismember(ADMINS_KEY, str(uid)))
    except Exception:
        return False


def add_admin(uid, name=""):
    redis.sadd(ADMINS_KEY, str(uid))
    if name:
        redis.hset("miniapp:admin_names", str(uid), name[:40])


def remove_admin(uid):
    redis.srem(ADMINS_KEY, str(uid))


def list_admins():
    names = redis.hgetall("miniapp:admin_names") or {}
    extra = sorted(redis.smembers(ADMINS_KEY) or [])
    return [{"id": i, "owner": True, "name": names.get(str(i), "")} for i in sorted(ADMIN_ID)] + \
           [{"id": int(i), "owner": False, "name": names.get(i, "")} for i in extra if int(i) not in ADMIN_ID]
