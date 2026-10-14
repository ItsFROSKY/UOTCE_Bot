import json
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from conftest import ADMIN, BOT_TOKEN, ROOT_FOLDER
import drive_index as di
import tasks_source as ts
from tg_auth import sign_init_data, validate_init_data

STUDENT = 555


def init_data(uid=STUDENT, age=0, token=BOT_TOKEN):
    return sign_init_data({
        "auth_date": str(int(time.time()) - age),
        "query_id": "AAH",
        "user": json.dumps({"id": uid, "first_name": "طه"}, ensure_ascii=False),
    }, token)


@pytest.fixture
def client(env):
    import main
    return TestClient(main.app)


def h(uid=STUDENT):
    return {"X-Telegram-Init-Data": init_data(uid)}


# ---------- initData ----------

def test_init_data_valid():
    assert validate_init_data(init_data(), BOT_TOKEN)["id"] == STUDENT


def test_init_data_rejects_tampering_wrong_token_and_old():
    good = init_data()
    tampered = good.replace("555", "1966119616")
    assert validate_init_data(tampered, BOT_TOKEN) is None
    assert validate_init_data(init_data(token="999:OTHER"), BOT_TOKEN) is None
    assert validate_init_data(init_data(age=3 * 86400), BOT_TOKEN) is None
    assert validate_init_data("", BOT_TOKEN) is None
    assert validate_init_data("garbage", BOT_TOKEN) is None


# ---------- drive index ----------

def test_index_structure_shortcuts_and_loops(env):
    idx = di.build_index(env.drive, ROOT_FOLDER)
    nodes = idx["nodes"]
    assert "SECRET" not in nodes                 # outside the course folder
    assert nodes["MATH"]["parent"] == "S1"       # shortcut didn't duplicate it
    assert nodes["DOC"]["native"] is True
    assert nodes["BIG"]["size"] == 80_000_000
    # natural sort: الأولى=1, then 2 before 10
    names = [i["name"] for i in di.list_folder(idx, "MATH")]
    assert names == ["المحاضرة الأولى.pdf", "المحاضرة 2.pdf", "المحاضرة 10.pdf"]


def test_arabic_search(env):
    idx = di.build_index(env.drive, ROOT_FOLDER)
    # typed without hamza and with ه instead of ة
    hits = di.search(idx, "المحاضره الاولى")
    assert hits[0]["id"] == "F1"
    assert hits[0]["path"] == "المرحلة الأولى / رياضيات"
    # path terms narrow results: subject + number
    assert [x["id"] for x in di.search(idx, "logic 2")] == ["F4"]
    assert di.search(idx, "   ") == []


def test_recent_order(env):
    idx = di.build_index(env.drive, ROOT_FOLDER)
    assert [r["id"] for r in di.recent(idx, 3)] == ["DOC", "BIG", "F4"]


def test_index_is_cached(env):
    di.get_index(env.drive, env.redis, ROOT_FOLDER)
    calls = env.drive.list_calls
    di._memory.update(index=None, at=0)        # cold instance: comes from redis
    di.get_index(env.drive, env.redis, ROOT_FOLDER)
    assert env.drive.list_calls == calls


# ---------- API ----------

def test_api_requires_auth(client):
    assert client.get("/api/bootstrap").status_code == 401
    bad = {"X-Telegram-Init-Data": init_data(token="1:x")}
    assert client.get("/api/bootstrap", headers=bad).status_code == 401


def test_bootstrap_and_browse(client):
    r = client.get("/api/bootstrap", headers=h()).json()
    assert r["user"]["first_name"] == "طه" and r["is_admin"] is False
    assert r["recent"][0]["id"] == "DOC"

    root = client.get("/api/folder/root", headers=h()).json()
    assert [i["name"] for i in root["items"]] == ["المرحلة الأولى", "المرحلة الثانية"]
    assert root["items"][0]["count"] == 1

    math = client.get("/api/folder/MATH", headers=h()).json()
    assert [b["id"] for b in math["breadcrumb"]] == [ROOT_FOLDER, "S1", "MATH"]


def test_cannot_reach_outside_course_folder(client, env):
    assert client.get("/api/folder/ELSEWHERE", headers=h()).status_code == 404
    assert client.post("/api/send/SECRET", headers=h()).status_code == 404
    assert client.post("/api/favorites/SECRET", headers=h()).status_code == 404
    assert env.drive.downloads == []


def test_send_file_then_cached(client, env):
    r = client.post("/api/send/F1", headers=h())
    assert r.json() == {"status": "queued"}
    docs = env.sent.of("document")
    assert len(docs) == 1 and docs[0][1] == STUDENT
    assert env.drive.downloads == ["F1"]
    assert env.sent.of("delete")                 # "preparing…" notice removed

    client.post("/api/send/F1", headers=h(777))  # another student
    assert env.drive.downloads == ["F1"]         # no second download
    assert env.sent.of("document")[-1][2] == "TG-المحاضرة الأولى.pdf"


def test_big_and_native_files_return_link(client, env):
    assert client.post("/api/send/BIG", headers=h()).json()["status"] == "link"
    assert client.post("/api/send/DOC", headers=h()).json()["link"].startswith("https://docs.google.com")
    assert env.drive.downloads == []


def test_send_when_user_never_started_bot(client, env):
    env.bot._blocked = {STUDENT}
    assert client.post("/api/send/F1", headers=h()).status_code == 409


def test_send_rate_limit(client):
    codes = [client.post("/api/send/BIG", headers=h()).status_code for _ in range(17)]
    assert codes[:15] == [200] * 15 and codes[-1] == 429


def test_favorites(client):
    assert client.post("/api/favorites/F4", headers=h()).status_code == 200
    assert client.post("/api/favorites/LOGIC", headers=h()).status_code == 200
    favs = client.get("/api/favorites", headers=h()).json()["items"]
    assert {f["id"] for f in favs} == {"F4", "LOGIC"}
    assert client.get("/api/favorites", headers=h(777)).json()["items"] == []
    client.delete("/api/favorites/F4", headers=h())
    assert [f["id"] for f in client.get("/api/favorites", headers=h()).json()["items"]] == ["LOGIC"]


def test_search_endpoint(client):
    res = client.get("/api/search", params={"q": "flip"}, headers=h()).json()["results"]
    assert res[0]["id"] == "F4"
    assert client.get("/api/search", params={"q": "x" * 81}, headers=h()).status_code == 400


def test_admin_endpoints(client):
    assert client.post("/api/admin/reindex", headers=h()).status_code == 403
    r = client.post("/api/admin/reindex", headers=h(ADMIN)).json()
    assert r["files"] == 6


# ---------- webhook ----------

def test_webhook_rejects_forged_updates(client):
    update = {"update_id": 1, "message": {"message_id": 1, "date": 0, "text": "/update_tele",
              "chat": {"id": 1, "type": "private"}, "from": {"id": ADMIN, "is_bot": False, "first_name": "x"}}}
    assert client.post("/webhook", json=update).status_code == 403
    assert client.post("/webhook", json=update, headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"}).status_code == 403


def test_webhook_accepts_telegram(client, env):
    update = {"update_id": 1, "message": {"message_id": 1, "date": 0, "text": "/start",
              "chat": {"id": 42, "type": "private"}, "from": {"id": 42, "is_bot": False, "first_name": "Ali"}}}
    r = client.post("/webhook", json=update, headers={"X-Telegram-Bot-Api-Secret-Token": "hook-secret"})
    assert r.status_code == 200
    welcome = env.sent.of("message")[-1]
    assert "Ali" in welcome[2]
    buttons = welcome[3]["reply_markup"].keyboard[0]
    assert buttons[0].web_app.url == "https://uotce.example/app/index.html#files"


# ---------- bot hardening ----------

def _call(data, uid=STUDENT):
    return SimpleNamespace(id="1", data=data, from_user=SimpleNamespace(id=uid),
                           message=SimpleNamespace(chat=SimpleNamespace(id=uid), message_id=9))


def test_forged_callback_cannot_download_outside_files(env):
    import main
    main.handle_drive_click(_call("file:SECRET"))
    assert env.drive.downloads == []
    assert "غير متاح" in env.sent.of("message")[-1][2]
    main.handle_drive_click(_call("file:F2"))
    assert env.drive.downloads == ["F2"]
    main.handle_drive_click(_call("nonsense"))     # used to crash on split(":")


def _msg(text, uid=STUDENT):
    return SimpleNamespace(text=text, from_user=SimpleNamespace(id=uid, first_name="x"),
                           chat=SimpleNamespace(id=uid, type="private"))


def test_reset_topics_is_admin_only(env):
    import main
    env.redis.set("state:1rerWXAD3BpbnMjiaXEUPvD8eRlK46QFS", json.dumps({"topic_map": {"a": 1}, "sent_files": ["x"]}))
    main.handle_reset(_msg("/reset_topics"))
    assert "صلاحية" in env.sent[-1][2]
    assert json.loads(env.redis.get("state:1rerWXAD3BpbnMjiaXEUPvD8eRlK46QFS"))["sent_files"] == ["x"]


def test_addtask_and_deltask(env):
    import main
    main.add_task(_msg("/addtask منطق | تقرير المختبر | 2099-01-05 14:00", ADMIN))
    assert "انضاف" in env.sent[-1][2]
    main.add_task(_msg("/addtask منطق | ناقص", ADMIN))
    assert "الصيغة" in env.sent[-1][2]
    main.add_task(_msg("/addtask a | b | 2099-01-05", STUDENT))
    assert "صلاحية" in env.sent[-1][2]
    tasks = ts.get_tasks(env.redis)
    assert tasks[0]["title"] == "تقرير المختبر" and tasks[0]["due"].startswith("2099-01-05T14:00")
    main.del_task(_msg("/deltask 1", ADMIN))
    assert ts.get_tasks(env.redis) == []


# ---------- tasks & reminders ----------

def test_notion_page_parsing():
    page = {"id": "p1", "url": "https://notion.so/p1", "properties": {
        "Name": {"type": "title", "title": [{"plain_text": "Report 3"}]},
        "المادة": {"type": "select", "select": {"name": "Electronics"}},
        "Deadline": {"type": "date", "date": {"start": "2026-10-20"}},
        "Done": {"type": "checkbox", "checkbox": False}}}
    t = ts.parse_notion_page(page)
    assert t["title"] == "Report 3" and t["subject"] == "Electronics"
    assert t["due"] == "2026-10-20T23:59:00+03:00"
    page["properties"]["Done"]["checkbox"] = True
    assert ts.parse_notion_page(page) is None


def test_parse_due_formats():
    assert ts.parse_due("2026-10-20 14:00").isoformat() == "2026-10-20T14:00:00+03:00"
    assert ts.parse_due("20/10/2026").hour == 23
    assert ts.parse_due("tomorrow") is None


def test_reminders_windows_and_dedupe(client, env):
    import webapp_api
    now = datetime.now(ts.BAGHDAD)
    ts.add_manual_task(env.redis, "منطق", "واجب قريب", now + timedelta(hours=2))
    ts.add_manual_task(env.redis, "رياضيات", "واجب باچر", now + timedelta(hours=20))
    ts.add_manual_task(env.redis, "فيزياء", "واجب بعيد", now + timedelta(days=5))
    for uid in (1, 2, 3):
        env.redis.sadd(webapp_api.REMIND_KEY, uid)
    env.bot._blocked = {3}

    assert client.get("/api/cron/reminders").status_code == 403
    r = client.get("/api/cron/reminders", headers={"Authorization": "Bearer cron-secret"}).json()
    assert r == {"tasks_notified": 2, "users": 3}
    texts = [m[2] for m in env.sent.of("message")]
    assert sum("3 ساعات" in t for t in texts) == 2 and sum("أقل من يوم" in t for t in texts) == 2
    assert env.redis.sismember(webapp_api.REMIND_KEY, 3) == 0   # blocked user dropped

    # second run: nothing new
    before = len(env.sent)
    assert client.get("/api/cron/reminders", params={"key": "cron-secret"}).json()["tasks_notified"] == 0
    assert len(env.sent) == before


def test_reminders_toggle(client, env):
    import webapp_api
    assert client.post("/api/reminders", json={"enabled": True}, headers=h()).json() == {"reminders": True}
    assert env.redis.sismember(webapp_api.REMIND_KEY, STUDENT)
    assert client.get("/api/bootstrap", headers=h()).json()["reminders"] is True
    client.post("/api/reminders", json={"enabled": False}, headers=h())
    assert not env.redis.sismember(webapp_api.REMIND_KEY, STUDENT)


def test_stage_ordinals_sort_by_meaning():
    idx = {"nodes": {}, "children": {"R": []}}
    for i, name in enumerate(["المرحلة الرابعة", "المرحلة الأولى", "المرحلة الثالثة", "المرحلة الثانية"]):
        idx["nodes"][str(i)] = {"id": str(i), "name": name, "kind": "folder", "parent": "R"}
        idx["children"]["R"].append(str(i))
    idx["children"].update({str(i): [] for i in range(4)})
    assert [n["name"] for n in di.list_folder(idx, "R")] == [
        "المرحلة الأولى", "المرحلة الثانية", "المرحلة الثالثة", "المرحلة الرابعة"]
