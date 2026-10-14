import ipaddress
import json
import random
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from conftest import ADMIN
from test_miniapp import STUDENT, h


@pytest.fixture
def client(env):
    import main
    return TestClient(main.app)


@pytest.fixture
def fun(env):
    import fun
    return fun


def test_questions_are_always_right(fun):
    rng = random.Random(7)
    for level in range(10):
        for _ in range(300):
            q = fun.make_question(level, rng)
            assert len(q["options"]) == 4 and len(set(q["options"])) == 4
            net = ipaddress.IPv4Network(f"{q['ip']}/{q['prefix']}", strict=False)
            truth = {"network": str(net.network_address), "broadcast": str(net.broadcast_address),
                     "hosts": str(net.num_addresses - 2), "mask": str(net.netmask)}[q["kind"]]
            assert q["options"][q["answer"]] == truth
            assert str(net.network_address) in q["explain"]


def test_harder_later(fun):
    rng = random.Random(1)
    assert min(fun.make_question(0, rng)["prefix"] for _ in range(200)) >= 25
    assert min(fun.make_question(9, rng)["prefix"] for _ in range(200)) <= 18


def test_titles(fun):
    assert fun.title_for(0).startswith("توه بادي")
    assert fun.title_for(950).startswith("سبع الشبكات")
    assert fun.title_for(5000).startswith("عمو")


def _play(client, env, uid, correct_n, name="علي"):
    import fun
    r = client.post("/api/game/subnet/start", headers=h(uid)).json()
    rid = r["round"]
    assert "answer" not in r["question"]          # answer never leaves the server
    for i in range(fun.QUESTIONS):
        st = json.loads(env.redis.get(fun.ROUND_KEY.format(rid=rid)))
        right = st["q"]["answer"]
        choice = right if i < correct_n else (right + 1) % 4
        out = client.post("/api/game/subnet/answer", json={"round": rid, "choice": choice}, headers=h(uid)).json()
        assert out["correct"] == (i < correct_n)
    return out


def test_full_round_and_leaderboard(client, env, fun):
    out = _play(client, env, STUDENT, 10)
    res = out["result"]
    assert out["done"] and res["correct"] == 10 and res["score"] >= 1500 and res["week_rank"] == 1
    _play(client, env, 777, 3)
    board = client.get("/api/game/leaderboard", headers=h(777)).json()
    assert [r["score"] for r in board["top"]] == sorted([r["score"] for r in board["top"]], reverse=True)
    assert board["top"][0]["me"] is False and board["me"]["rank"] == 2 and board["players"] == 2
    # a worse round doesn't lower your best
    _play(client, env, STUDENT, 1)
    assert client.get("/api/game/leaderboard", headers=h()).json()["me"]["score"] == res["score"]


def test_cannot_use_someone_elses_round_or_answer_late(client, env, fun, monkeypatch):
    rid = client.post("/api/game/subnet/start", headers=h()).json()["round"]
    assert client.post("/api/game/subnet/answer", json={"round": rid, "choice": 0}, headers=h(777)).status_code == 404
    st = json.loads(env.redis.get(fun.ROUND_KEY.format(rid=rid)))
    st["q"]["asked"] = time.time() - 60
    env.redis.set(fun.ROUND_KEY.format(rid=rid), json.dumps(st))
    out = client.post("/api/game/subnet/answer", json={"round": rid, "choice": st["q"]["answer"]}, headers=h()).json()
    assert out["correct"] is False and out["timeout"] is True


def test_week_champion_posted_once(client, env, fun, monkeypatch):
    import campus as cp
    from datetime import timedelta
    cp.set_group(-1005)
    last_week = fun.week_key(cp.now().date() - timedelta(days=7))
    env.redis.zadd(fun.LB_WEEK.format(week=last_week), {"1": 900, "2": 1500})
    env.redis.hset(fun.LB_NAMES, "2", "زينب")
    assert fun.announce_week_champion()[0] == "2"
    post = env.sent.of("message")[-1]
    assert post[1] == -1005 and "🥇 زينب - 1500" in post[2]
    assert fun.announce_week_champion() is None


# polls

def test_poll_flow(client, env, fun):
    import campus as cp
    cp.set_group(-1006)
    assert client.post("/api/admin/polls", json={"question": "نأجل؟", "options": ["اي", "لا"]}, headers=h()).status_code == 403
    assert client.post("/api/admin/polls", json={"question": "نأجل؟", "options": ["اي"]}, headers=h(ADMIN)).status_code == 400
    p = client.post("/api/admin/polls", json={"question": "نطلب تأجيل الامتحان؟", "options": ["اي", "لا", "ما يهمني"], "hours": 12},
                    headers=h(ADMIN)).json()["poll"]
    assert "تصويت جديد" in env.sent.of("message")[-1][2]

    before = client.get("/api/polls", headers=h()).json()["items"][0]
    assert before["counts"] is None and before["my_vote"] is None    # no peeking before voting
    r = client.post(f"/api/polls/{p['id']}/vote", json={"option": 0}, headers=h()).json()["poll"]
    assert r["my_vote"] == 0 and r["counts"] == [1, 0, 0]
    client.post(f"/api/polls/{p['id']}/vote", json={"option": 1}, headers=h(777))
    client.post(f"/api/polls/{p['id']}/vote", json={"option": 1}, headers=h())   # change my vote
    assert client.get("/api/polls", headers=h()).json()["items"][0]["counts"] == [0, 2, 0]
    assert client.post(f"/api/polls/{p['id']}/vote", json={"option": 9}, headers=h()).status_code == 400

    client.post(f"/api/admin/polls/{p['id']}/close", headers=h(ADMIN))
    res = env.sent.of("message")[-1][2]
    assert "نتيجة التصويت (2 صوت)" in res and "100%  لا (2)" in res
    assert client.post(f"/api/polls/{p['id']}/vote", json={"option": 0}, headers=h()).status_code == 400
    assert client.get("/api/polls", headers=h(999)).json()["items"][0]["counts"] == [0, 2, 0]   # closed: everyone sees


def test_expired_polls_close_on_cron(client, env, fun):
    p = fun.create_poll("x؟", ["a", "b"], 1)
    polls = json.loads(env.redis.get(fun.POLLS_KEY))
    polls[0]["closes"] = time.time() - 10
    env.redis.set(fun.POLLS_KEY, json.dumps(polls))
    r = client.get("/api/cron/tick", params={"key": "cron-secret"}).json()
    assert r["polls_closed"] == 1
    assert client.get("/api/cron/tick", params={"key": "cron-secret"}).json()["polls_closed"] == 0


def test_poll_and_top_commands(env, fun):
    import main
    msg = lambda t, uid=ADMIN: SimpleNamespace(text=t, from_user=SimpleNamespace(id=uid, first_name="x"),
                                               chat=SimpleNamespace(id=uid, type="private"))
    main.poll(msg("/poll نطلب تعويضي؟ | الخميس | السبت"))
    assert "انفتح التصويت" in env.sent[-1][2]
    main.poll(msg("/poll بس سؤال"))
    assert "الصيغة" in env.sent[-1][2]
    main.top(msg("/top", STUDENT))
    assert "ماكو أحد لعب" in env.sent[-1][2]
    assert "🗳 تصويت مفتوح" in __import__("campus").dashboard_text()


def test_backup_command(env, fun):
    import main, json as j
    import campus as cp
    cp.replace_schedule(cp.parse_schedule_text("الأحد | 08:30-10:30 | DL | 3")[0])
    msg = lambda uid, t="private": SimpleNamespace(text="/backup", from_user=SimpleNamespace(id=uid, first_name="x"),
                                                    chat=SimpleNamespace(id=uid, type=t))
    main.backup(msg(STUDENT))
    assert "صلاحية" in env.sent[-1][2]
    main.backup(msg(ADMIN, "group"))
    assert "بالخاص" in env.sent[-1][2]
    main.backup(msg(ADMIN))
    name, stream = env.sent.of("document")[-1][2]
    data = j.loads(stream.getvalue())
    assert name.startswith("uotce-backup-") and data["schedule"][0]["subject"] == "DL"


def test_browsing_costs_no_redis_rate_limit(client, env):
    calls = []
    orig = env.redis.incr
    env.redis.incr = lambda k: (calls.append(k), orig(k))[1]
    for _ in range(5):
        client.get("/api/folder/root", headers=h())
    assert calls == []
    codes = [client.get("/api/search", params={"q": "x"}, headers=h(4242)).status_code for _ in range(122)]
    assert codes[-1] == 429


# admins

def _m(text, uid, reply_to=None, chat="private"):
    return SimpleNamespace(text=text, from_user=SimpleNamespace(id=uid, first_name="x", is_bot=False),
                           chat=SimpleNamespace(id=uid, type=chat), reply_to_message=reply_to)


def test_owner_adds_and_removes_admin(client, env, monkeypatch):
    import main, campus as cp
    monkeypatch.setattr(env.bot, "set_my_commands", lambda *a, **k: None)
    monkeypatch.setattr(env.bot, "delete_my_commands", lambda *a, **k: None)
    new = 8080
    assert client.get("/api/bootstrap", headers=h(new)).json()["is_admin"] is False
    assert client.post("/api/admin/reindex", headers=h(new)).status_code == 403

    main.add_admin_cmd(_m(f"/add_admin {new}", STUDENT))           # a student can't
    assert "بس المالك" in env.sent[-1][2] and not cp.is_admin_id(new)

    main.add_admin_cmd(_m(f"/add_admin {new}", ADMIN))
    assert "صار أدمن" in env.sent[-1][2]
    assert client.get("/api/bootstrap", headers=h(new)).json()["is_admin"] is True
    assert client.post("/api/admin/reindex", headers=h(new)).status_code == 200
    main.announce(_m("/announce تجربة", new))                       # bot commands work too
    assert "انرسل الإعلان" in env.sent[-1][2]

    main.add_admin_cmd(_m("/add_admin 9090", new))                   # admins can't make admins
    assert "بس المالك" in env.sent[-1][2]

    main.remove_admin_cmd(_m(f"/remove_admin {ADMIN}", ADMIN))       # owners stay owners
    assert "ADMIN_IDS" in env.sent[-1][2]
    main.remove_admin_cmd(_m(f"/remove_admin {new}", ADMIN))
    assert client.get("/api/bootstrap", headers=h(new)).json()["is_admin"] is False


def test_add_admin_by_reply_and_lists(env, monkeypatch):
    import main, campus as cp
    monkeypatch.setattr(env.bot, "set_my_commands", lambda *a, **k: None)
    replied = SimpleNamespace(from_user=SimpleNamespace(id=7070, first_name="حيدر", is_bot=False))
    main.add_admin_cmd(_m("/add_admin", ADMIN, reply_to=replied, chat="supergroup"))
    assert cp.is_admin_id(7070)
    main.admins_cmd(_m("/admins", ADMIN))
    assert "🔧 حيدر - 7070" in env.sent[-1][2] and f"👑" in env.sent[-1][2]
    main.myid(_m("/myid", 7070))
    assert "7070" in env.sent[-1][2] and "أدمن" in env.sent[-1][2]
    main.myid(_m("/myid", STUDENT))
    assert "طالب" in env.sent[-1][2]
