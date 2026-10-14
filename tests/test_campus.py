import json
import time
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from conftest import ADMIN, ROOT_FOLDER
from test_miniapp import STUDENT, h


@pytest.fixture
def client(env):
    import main
    return TestClient(main.app)


@pytest.fixture
def cp(env):
    import campus
    return campus


def _next(weekday_sun0, cp):
    d = cp.now().date()
    while cp.day_index(d) != weekday_sun0:
        d += timedelta(days=1)
    return d


def _msg(text, uid=ADMIN, chat_type="private", chat_id=None):
    return SimpleNamespace(text=text, from_user=SimpleNamespace(id=uid, first_name="أدمن"),
                           chat=SimpleNamespace(id=chat_id or uid, type=chat_type),
                           message_thread_id=None, is_topic_message=False)


# ---------- follow folders + new files ----------

def test_follow_requires_started_bot_and_valid_folder(client, env):
    assert client.post("/api/subscriptions/SECRET", headers=h()).status_code == 404
    assert client.post("/api/subscriptions/F1", headers=h()).status_code == 404   # a file, not folder
    env.bot._blocked = {STUDENT}
    assert client.post("/api/subscriptions/MATH", headers=h()).status_code == 409
    env.bot._blocked = set()
    assert client.post("/api/subscriptions/MATH", headers=h()).json() == {"subscribed": True}
    assert client.get("/api/folder/MATH", headers=h()).json()["subscribed"] is True
    assert [i["id"] for i in client.get("/api/subscriptions", headers=h()).json()["items"]] == ["MATH"]
    assert client.get("/api/bootstrap", headers=h()).json()["subs"] == ["MATH"]
    client.delete("/api/subscriptions/MATH", headers=h())
    assert client.get("/api/subscriptions", headers=h()).json()["items"] == []


def test_new_files_notify_followers_of_any_ancestor(cp, env):
    first = cp.check_new_files()
    assert first["seeded"] == 6 and env.sent.of("message") == []      # no flood on first run

    cp.subscribe(STUDENT, "MATH")      # follows the subject
    cp.subscribe(777, "S1")            # follows the whole stage
    cp.subscribe(888, "LOGIC")         # unrelated subject
    cp.set_group(-100123)
    env.drive.tree["F9"] = {"id": "F9", "name": "المحاضرة 5.pdf", "mimeType": "application/pdf",
                            "parent": "MATH", "size": "1000", "modifiedTime": "2026-10-14T10:00:00.000Z"}

    r = cp.check_new_files()
    assert r == {"new": 1, "notified": 2}
    msgs = env.sent.of("message")
    to = {m[1] for m in msgs}
    assert to == {STUDENT, 777, -100123}
    dm = next(m for m in msgs if m[1] == STUDENT)
    assert "المحاضرة 5.pdf" in dm[2] and "المرحلة الأولى / رياضيات" in dm[2]
    buttons = [b for row in dm[3]["reply_markup"].keyboard for b in row]
    assert buttons[0].callback_data == "file:F9"
    assert buttons[1].web_app.url.endswith("#f-MATH")
    group = next(m for m in msgs if m[1] == -100123)
    assert group[3]["reply_markup"].keyboard[0][0].url == "https://t.me/uotce_bot?startapp=f-MATH"

    assert cp.check_new_files() == {"new": 0, "notified": 0}   # nothing twice


def test_many_new_files_are_summarised(cp, env):
    cp.check_new_files()
    cp.subscribe(STUDENT, ROOT_FOLDER)
    for i in range(12):
        env.drive.tree[f"N{i}"] = {"id": f"N{i}", "name": f"Lab {i}.pdf", "mimeType": "application/pdf",
                                   "parent": "LOGIC", "size": "10", "modifiedTime": f"2026-10-14T10:{i:02d}:00.000Z"}
    cp.check_new_files()
    dms = [m for m in env.sent.of("message") if m[1] == STUDENT]
    assert len(dms) == 1 and "انضافت 12 ملفات" in dms[0][2] and "و 4 ملفات ثانية" in dms[0][2]


def test_blocked_follower_is_forgotten(cp, env):
    cp.check_new_files()
    cp.subscribe(999, "MATH")
    env.bot._blocked = {999}
    env.drive.tree["F9"] = {"id": "F9", "name": "x.pdf", "mimeType": "application/pdf", "parent": "MATH",
                            "size": "1", "modifiedTime": "2026-10-14T10:00:00.000Z"}
    cp.check_new_files()
    assert cp.user_subscriptions(999) == set()
    assert env.redis.sismember("miniapp:sub:MATH", 999) == 0


# ---------- announcements ----------

def test_announcement_from_app_reaches_everyone(client, env, cp):
    env.redis.sadd(cp.USERS_KEY, 1, 2)
    cp.set_group(-100500, thread_id=7)
    assert client.post("/api/admin/announcements", json={"text": "x"}, headers=h()).status_code == 403
    assert client.post("/api/admin/announcements", json={"text": "  "}, headers=h(ADMIN)).status_code == 400
    r = client.post("/api/admin/announcements", json={"text": "المحاضرة بقاعة 5", "kind": "room", "days": 2},
                    headers=h(ADMIN)).json()["announcement"]
    assert r["kind"] == "room" and r["expires"] - r["created"] == 2 * 86400
    msgs = env.sent.of("message")
    assert {m[1] for m in msgs} == {1, 2, -100500}
    assert next(m for m in msgs if m[1] == -100500)[3]["message_thread_id"] == 7
    assert "تغيير قاعة" in msgs[0][2]
    items = client.get("/api/announcements", headers=h()).json()["items"]
    assert [a["text"] for a in items] == ["المحاضرة بقاعة 5"]
    client.delete(f"/api/admin/announcements/{r['id']}", headers=h(ADMIN))
    assert client.get("/api/announcements", headers=h()).json()["items"] == []


def test_expired_announcements_hidden(cp):
    a = cp.add_announcement("قديم", expires=time.time() - 1)
    cp.add_announcement("جديد")
    assert [x["text"] for x in cp.active_announcements()] == ["جديد"]


def test_announce_command(env, cp):
    import main
    env.redis.sadd(cp.USERS_KEY, 5)
    main.announce(_msg("/announce بكرة عطلة", uid=STUDENT))
    assert "صلاحية" in env.sent[-1][2]
    main.announce(_msg("/announce بكرة عطلة"))
    assert any(m[1] == 5 and "بكرة عطلة" in m[2] for m in env.sent.of("message"))
    assert "لـ 1 طالب" in env.sent[-1][2]


def test_set_group_only_inside_group(env, cp):
    import main
    main.set_group(_msg("/set_group"))
    assert "داخل كروب" in env.sent[-1][2]
    main.set_group(_msg("/set_group", chat_type="supergroup", chat_id=-1001))
    assert json.loads(env.redis.get(cp.GROUP_KEY))["chat_id"] == -1001
    # group keyboard uses deep links, not web_app buttons (those fail in groups)
    kb = env.sent[-1][3]["reply_markup"].keyboard
    assert kb[0][0].url == "https://t.me/uotce_bot?startapp=files"


# ---------- schedule ----------

SCHEDULE = """/schedule_set
الأحد | 08:30-10:30 | Digital Logic | قاعة 3 | نظري
الاحد | 10:30-12:30 | Electronics | مختبر 2 | عملي | A
اثنين | 9:00-11:00 | رياضيات | قاعة 1"""


def test_schedule_import_and_validation(env, cp):
    import main
    main.schedule_set(_msg(SCHEDULE))
    assert "3 محاضرة" in env.sent[-1][2]
    classes = cp.get_schedule()
    assert [(c["day"], c["start"], c["subject"]) for c in classes] == [
        (0, "08:30", "Digital Logic"), (0, "10:30", "Electronics"), (1, "09:00", "رياضيات")]
    assert classes[1]["section"] == "A" and classes[1]["kind"] == "عملي"

    main.schedule_set(_msg("/schedule_set\nالأحد | 10:00-09:00 | X\nفلان | 08:00-09:00 | Y"))
    assert "سطر 1" in env.sent[-1][2] and "سطر 2" in env.sent[-1][2]
    assert len(cp.get_schedule()) == 3        # bad import changes nothing

    main.schedule_set(_msg("/schedule_add الخميس | 12:00-13:00 | مناقشة"))
    assert len(cp.get_schedule()) == 4


def test_override_postpone_announces_and_shows(client, env, cp):
    import main
    main.schedule_set(_msg(SCHEDULE))
    env.redis.sadd(cp.USERS_KEY, 11)
    sunday = _next(0, cp)
    dl = cp.get_schedule()[0]

    bad = client.post(f"/api/admin/schedule/{dl['id']}/override",
                      json={"date": (sunday + timedelta(days=1)).isoformat(), "status": "postponed"}, headers=h(ADMIN))
    assert bad.status_code == 400                     # Monday isn't this class's day
    r = client.post(f"/api/admin/schedule/{dl['id']}/override",
                    json={"date": sunday.isoformat(), "status": "postponed", "note": "للأسبوع الجاي"},
                    headers=h(ADMIN)).json()
    assert r["overrides"][f"{dl['id']}:{sunday.isoformat()}"]["status"] == "postponed"
    dm = next(m for m in env.sent.of("message") if m[1] == 11)
    assert "تأجلت محاضرة Digital Logic" in dm[2] and "08:30" in dm[2] and "للأسبوع الجاي" in dm[2]

    assert "⏰ مؤجلة" in cp.day_text(sunday)
    sched = client.get("/api/schedule", headers=h()).json()
    assert len(sched["classes"]) == 3 and sched["overrides"]

    client.post(f"/api/admin/schedule/{dl['id']}/override",
                json={"date": sunday.isoformat(), "status": "clear"}, headers=h(ADMIN))
    assert "مؤجلة" not in cp.day_text(sunday)


def test_room_change_needs_room(client, cp, env):
    import main
    main.schedule_set(_msg(SCHEDULE))
    c = cp.get_schedule()[0]
    sunday = _next(0, cp).isoformat()
    assert client.post(f"/api/admin/schedule/{c['id']}/override", json={"date": sunday, "status": "room"},
                       headers=h(ADMIN)).status_code == 400
    ok = client.post(f"/api/admin/schedule/{c['id']}/override",
                     json={"date": sunday, "status": "room", "room": "قاعة 9", "announce": False}, headers=h(ADMIN))
    assert ok.json()["announcement"] is None


def test_schedule_admin_crud(client, cp):
    assert client.post("/api/admin/schedule", json={}, headers=h()).status_code == 403
    bad = client.post("/api/admin/schedule", json={"day": 2, "start": "9", "end": "10:00", "subject": "x"}, headers=h(ADMIN))
    assert bad.status_code == 400
    c = client.post("/api/admin/schedule", json={"day": 2, "start": "9:00", "end": "10:00", "subject": "Networks",
                                                 "room": "قاعة 4"}, headers=h(ADMIN)).json()["class"]
    c["room"] = "قاعة 6"
    client.post("/api/admin/schedule", json=c, headers=h(ADMIN))
    assert [x["room"] for x in cp.get_schedule()] == ["قاعة 6"]
    client.delete(f"/api/admin/schedule/{c['id']}", headers=h(ADMIN))
    assert cp.get_schedule() == []


def test_schedule_command_public(env, cp):
    import main
    main.schedule_set(_msg(SCHEDULE))
    main.schedule(_msg("/schedule", uid=STUDENT))
    assert "📅" in env.sent[-1][2]


def test_cron_tick(client, env, cp):
    assert client.get("/api/cron/tick").status_code == 403
    r = client.get("/api/cron/tick", params={"key": "cron-secret"}).json()
    assert r["files"]["seeded"] == 6 and r["reminders"]["tasks_notified"] == 0


def test_help_lists_commands(env, monkeypatch):
    import main
    main.send_help(_msg("/help", uid=STUDENT))
    text = env.sent[-1][2]
    assert "/schedule" in text and "/announce" not in text and "Taha AlAnbagi" in text
    main.send_help(_msg("/help"))
    assert "/announce" in env.sent[-1][2] and "/schedule_set" in env.sent[-1][2]
    # every admin command in the list really exists as a handler
    handled = {c for h in main.bot.message_handlers for c in (h["filters"].get("commands") or [])}
    assert {c for c, _ in main.STUDENT_COMMANDS + main.ADMIN_COMMANDS} <= handled


def test_setup_app_sets_admin_menu(env, monkeypatch):
    import main
    calls = []
    monkeypatch.setattr(main.bot, "set_chat_menu_button", lambda **k: None)
    monkeypatch.setattr(main.bot, "set_my_commands", lambda cmds, scope=None: calls.append((len(cmds), scope)))
    main.setup_app(_msg("/setup_app"))
    assert calls[0] == (len(main.STUDENT_COMMANDS), None)
    assert all(n == len(main.STUDENT_COMMANDS) + len(main.ADMIN_COMMANDS) for n, s in calls[1:]) and len(calls) == 3


def _group_msg(text, uid=ADMIN):
    return SimpleNamespace(text=text, message_id=55, from_user=SimpleNamespace(id=uid, first_name="أدمن"),
                           chat=SimpleNamespace(id=-1009, type="supergroup"), message_thread_id=None, is_topic_message=False)


def test_pinned_dashboard_and_auto_refresh(env, cp, monkeypatch):
    import main
    pins, edits, deleted = [], [], []
    monkeypatch.setattr(env.bot, "pin_chat_message", lambda c, m, **k: pins.append((c, m)))
    monkeypatch.setattr(env.bot, "edit_message_text", lambda t, c, m, **k: edits.append(t))
    monkeypatch.setattr(env.bot, "delete_message", lambda c, m: deleted.append(m))
    main.schedule_set(_msg(SCHEDULE))

    main.pin_app(_group_msg("/pin_app", uid=STUDENT))
    assert "صلاحية" in env.sent[-1][2] and not pins

    main.pin_app(_group_msg("/pin_app"))
    post = env.sent.of("message")[-1]
    assert post[1] == -1009 and "Taha AlAnbagi" in post[2] and "📅 محاضرات" in post[2]
    kb = post[3]["reply_markup"].keyboard
    assert kb[0][0].url == "https://t.me/uotce_bot?startapp=files"
    assert {b.url.split("=")[1] for row in kb for b in row} == {"files", "schedule", "tasks", "favs"}
    assert len(pins) == 1 and pins[0][0] == -1009 and deleted == [55]
    assert json.loads(env.redis.get(cp.GROUP_KEY))["chat_id"] == -1009      # became the batch group

    assert cp.refresh_dashboard() is False and not edits                    # nothing changed
    cp.add_announcement("بكرة عطلة رسمية")
    assert cp.refresh_dashboard() is True and "بكرة عطلة رسمية" in edits[-1]


def test_pin_without_permission_warns(env, cp, monkeypatch):
    import main
    from telebot.apihelper import ApiTelegramException
    def no(*a, **k):
        raise ApiTelegramException("pinChatMessage", None, {"error_code": 400, "description": "not enough rights"})
    monkeypatch.setattr(env.bot, "pin_chat_message", no)
    monkeypatch.setattr(env.bot, "delete_message", lambda *a: None)
    main.pin_app(_group_msg("/pin_app"))
    assert "Pin messages" in env.sent.of("message")[-1][2]


def test_app_command_in_group_shows_dashboard(env):
    import main
    main.send_app(_group_msg("/app", uid=STUDENT))
    reply = env.sent[-1]
    assert reply[0] == "reply" and "دوس أي زر" in reply[2]
    assert reply[3]["reply_markup"].keyboard[0][0].url.startswith("https://t.me/uotce_bot?startapp=")


# ---------- exams ----------

def test_exam_add_command_and_list(env, cp):
    import main
    future = (cp.now().date() + timedelta(days=10))
    main.exam_add(_msg(f"/exam_add Digital Logic | شهري | {future.isoformat()} | 9:00 | قاعة 3"))
    assert "انضاف امتحان" in env.sent[-1][2]
    main.exam_add(_msg("/exam_add ناقص"))
    assert "الصيغة" in env.sent[-1][2]
    main.exam_add(_msg("/exam_add x | شهري | 2026-10-20", uid=STUDENT))
    assert "صلاحية" in env.sent[-1][2]
    e = cp.upcoming_exams()[0]
    assert e["time"] == "09:00" and e["room"] == "قاعة 3" and e["days_left"] == 10
    main.exams(_msg("/exams", uid=STUDENT))
    assert "Digital Logic (شهري) - بعد 10 أيام" in env.sent[-1][2]
    main.exam_del(_msg("/exam_del 1"))
    assert cp.upcoming_exams() == []


def test_exam_reminders_week_then_day(env, cp):
    env.redis.sadd(cp.USERS_KEY, 70)
    cp.set_group(-1002)
    today = cp.now().date()
    cp.upsert_exam({"subject": "Electronics", "kind": "نهائي", "date": (today + timedelta(days=6)).isoformat()})
    cp.upsert_exam({"subject": "Math", "kind": "شهري", "date": (today + timedelta(days=1)).isoformat()})
    cp.upsert_exam({"subject": "Far", "kind": "شهري", "date": (today + timedelta(days=30)).isoformat()})
    assert cp.run_exam_reminders() == {"exams_notified": 2}
    texts = [m[2] for m in env.sent.of("message") if m[1] == 70]
    assert any("Electronics" in t and "بعد 6 أيام" in t for t in texts)
    assert any("Math" in t and "باچر" in t for t in texts)
    assert cp.run_exam_reminders() == {"exams_notified": 0}      # no repeats
    assert "📝 الامتحانات القادمة" in cp.dashboard_text() and "Far" not in cp.dashboard_text()


def test_exam_api(client, cp):
    future = (cp.now().date() + timedelta(days=3)).isoformat()
    assert client.post("/api/admin/exams", json={"subject": "x", "date": future}, headers=h()).status_code == 403
    assert client.post("/api/admin/exams", json={"subject": "x", "date": "bad"}, headers=h(ADMIN)).status_code == 400
    e = client.post("/api/admin/exams", json={"subject": "Networks", "kind": "كوز", "date": future, "time": "10:30"},
                    headers=h(ADMIN)).json()["exam"]
    items = client.get("/api/exams", headers=h()).json()["items"]
    assert items[0]["subject"] == "Networks" and items[0]["days_left"] == 3
    client.delete(f"/api/admin/exams/{e['id']}", headers=h(ADMIN))
    assert client.get("/api/exams", headers=h()).json()["items"] == []


# ---------- absences ----------

def _past(weekday, cp, weeks_back=0):
    d = cp.now().date()
    while cp.day_index(d) != weekday:
        d -= timedelta(days=1)
    return (d - timedelta(days=7 * weeks_back)).isoformat()


def test_absence_percentages_and_limits(client, env, cp):
    # Digital Logic: 2h/week -> 30h per semester, 10% = 3h, so 1 lecture (2h) ok, 2nd one fails
    cp.replace_schedule(cp.parse_schedule_text(
        "الأحد | 08:30-10:30 | Digital Logic | قاعة 3\nالاثنين | 08:30-10:30 | Math | قاعة 1\nالاثنين | 10:30-12:30 | Math | قاعة 1")[0])
    dl = cp.get_schedule()[0]
    math = [c for c in cp.get_schedule() if c["subject"] == "Math"]

    r = client.post("/api/absences", json={"class_id": dl["id"], "date": _past(0, cp)}, headers=h())
    body = r.json()
    row = next(x for x in body["subjects"] if x["subject"] == "Digital Logic")
    assert row["total_hours"] == 30 and row["pct"] == 6.7 and row["left"] == 0 and row["status"] == "danger"
    assert "أي غياب ثاني" in body["warning"]
    assert any(m[1] == STUDENT and "Digital Logic" in m[2] for m in env.sent.of("message"))   # DM warning

    dup = client.post("/api/absences", json={"class_id": dl["id"], "date": _past(0, cp)}, headers=h())
    assert dup.status_code == 400
    r = client.post("/api/absences", json={"class_id": dl["id"], "date": _past(0, cp, 1)}, headers=h()).json()
    assert next(x for x in r["subjects"] if x["subject"] == "Digital Logic")["status"] == "fail"

    # Math 4h/week -> 60h; one excused 2h = 3.3% total, still ok
    r = client.post("/api/absences", json={"class_id": math[0]["id"], "date": _past(1, cp), "excused": True}, headers=h()).json()
    m = next(x for x in r["subjects"] if x["subject"] == "Math")
    assert m["pct"] == 0 and m["pct_all"] == 3.3 and m["status"] == "ok" and r["warning"] is None
    assert m["left"] == 3   # 6h unexcused allowed -> 3 lectures of 2h

    # other students don't see it
    assert all(x["count"] == 0 for x in client.get("/api/absences", headers=h(777)).json()["subjects"])

    aid = r["entries"][0]["id"]
    after = client.delete(f"/api/absences/{aid}", headers=h()).json()
    assert len(after["entries"]) == 2


def test_absence_validation(client, cp):
    cp.replace_schedule(cp.parse_schedule_text("الأحد | 08:30-10:30 | DL | 1")[0])
    c = cp.get_schedule()[0]
    future = cp.now().date() + timedelta(days=7)
    while cp.day_index(future) != 0:
        future += timedelta(days=1)
    wrong_day = _past(1, cp)
    assert client.post("/api/absences", json={"class_id": c["id"], "date": future.isoformat()}, headers=h()).status_code == 400
    assert client.post("/api/absences", json={"class_id": c["id"], "date": wrong_day}, headers=h()).status_code == 400
    assert client.post("/api/absences", json={"class_id": "nope", "date": wrong_day}, headers=h()).status_code == 404


def test_absence_uses_my_section(client, cp):
    cp.replace_schedule(cp.parse_schedule_text(
        "الأحد | 08:30-10:30 | Lab | م1 | عملي | A\nالأحد | 10:30-13:30 | Lab | م2 | عملي | B")[0])
    client.post("/api/me/section", json={"section": "B"}, headers=h())
    assert client.get("/api/bootstrap", headers=h()).json()["section"] == "B"
    row = client.get("/api/absences", headers=h()).json()["subjects"][0]
    assert row["total_hours"] == 45   # section B lab is 3h/week
    client.post("/api/me/section", json={"section": ""}, headers=h())
    assert client.get("/api/absences", headers=h()).json()["subjects"][0]["total_hours"] == 30   # guesses A
