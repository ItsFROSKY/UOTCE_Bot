# fake redis, fake drive and a fake bot so tests run offline
import base64
import fnmatch
import json
import os
import re
import sys
import time
import types as pytypes

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

BOT_TOKEN = "123456:TEST-TOKEN"
ROOT_FOLDER = "ROOT"
ADMIN = 1966119616


# ---------------- fake redis ----------------
class FakeRedis:
    def __init__(self, *a, **k):
        self.kv, self.sets, self.exp = {}, {}, {}

    def _alive(self, key):
        if key in self.exp and self.exp[key] < time.time():
            self.kv.pop(key, None)
            self.exp.pop(key, None)

    def get(self, key):
        self._alive(key)
        return self.kv.get(key)

    def set(self, key, value, nx=None, ex=None, **k):
        self._alive(key)
        if nx and key in self.kv:
            return None
        self.kv[key] = str(value) if not isinstance(value, str) else value
        if ex:
            self.exp[key] = time.time() + ex
        return True

    def incr(self, key):
        self.kv[key] = str(int(self.kv.get(key, 0)) + 1)
        return int(self.kv[key])

    def expire(self, key, seconds, **k):
        self.exp[key] = time.time() + int(seconds)
        return 1

    def delete(self, *keys):
        for k in keys:
            self.kv.pop(k, None)
            self.sets.pop(k, None)

    def sadd(self, key, *members):
        s = self.sets.setdefault(key, set())
        before = len(s)
        s.update(str(m) for m in members)
        return len(s) - before

    def srem(self, key, *members):
        s = self.sets.setdefault(key, set())
        for m in members:
            s.discard(str(m))

    def smembers(self, key):
        return list(self.sets.get(key, set()))

    def sismember(self, key, member):
        return 1 if str(member) in self.sets.get(key, set()) else 0

    def scard(self, key):
        return len(self.sets.get(key, set()))

    # hashes
    def hset(self, key, field=None, value=None, values=None):
        h = self.kv.setdefault(key, {})
        h[str(field)] = str(value)
        return 1

    def hget(self, key, field):
        return (self.kv.get(key) or {}).get(str(field))

    def hgetall(self, key):
        return dict(self.kv.get(key) or {})

    # sorted sets
    def zadd(self, key, scores, **k):
        z = self.kv.setdefault(key, {})
        for m, sc in scores.items():
            z[str(m)] = float(sc)
        return len(scores)

    def zscore(self, key, member):
        return (self.kv.get(key) or {}).get(str(member))

    def _zsorted(self, key):
        return sorted((self.kv.get(key) or {}).items(), key=lambda x: (-x[1], x[0]))

    def zrange(self, key, start, stop, rev=False, withscores=False, **k):
        rows = self._zsorted(key) if rev else list(reversed(self._zsorted(key)))
        rows = rows[start:stop + 1]
        return rows if withscores else [m for m, _ in rows]

    def zrevrank(self, key, member):
        ids = [m for m, _ in self._zsorted(key)]
        return ids.index(str(member)) if str(member) in ids else None

    def zcard(self, key):
        return len(self.kv.get(key) or {})


# ---------------- fake drive ----------------
FOLDER = "application/vnd.google-apps.folder"
SHORTCUT = "application/vnd.google-apps.shortcut"


def make_tree():
    # fake drive tree, id -> metadata
    f = lambda i, n, p: {"id": i, "name": n, "mimeType": FOLDER, "parent": p,
                         "modifiedTime": "2026-09-01T10:00:00.000Z"}
    pdf = lambda i, n, p, mod, size=2_000_000: {
        "id": i, "name": n, "mimeType": "application/pdf", "parent": p, "size": str(size),
        "modifiedTime": mod, "webViewLink": f"https://drive.google.com/file/d/{i}/view"}
    items = [
        f("S1", "المرحلة الأولى", ROOT_FOLDER),
        f("S2", "المرحلة الثانية", ROOT_FOLDER),
        f("MATH", "رياضيات", "S1"),
        f("LOGIC", "Digital Logic", "S2"),
        pdf("F1", "المحاضرة الأولى.pdf", "MATH", "2026-10-01T10:00:00.000Z"),
        pdf("F2", "المحاضرة 10.pdf", "MATH", "2026-10-03T10:00:00.000Z"),
        pdf("F3", "المحاضرة 2.pdf", "MATH", "2026-10-02T10:00:00.000Z"),
        pdf("F4", "Lec 2 - Flip Flops.pdf", "LOGIC", "2026-10-10T10:00:00.000Z"),
        pdf("BIG", "Lab recording.mp4", "LOGIC", "2026-10-11T10:00:00.000Z", size=80_000_000),
        {"id": "DOC", "name": "ملاحظات", "mimeType": "application/vnd.google-apps.document",
         "parent": "LOGIC", "modifiedTime": "2026-10-12T10:00:00.000Z",
         "webViewLink": "https://docs.google.com/document/d/DOC"},
        # shortcut inside S2 pointing at MATH (already indexed) and a loop back to ROOT
        {"id": "SC1", "name": "اختصار رياضيات", "mimeType": SHORTCUT, "parent": "S2",
         "shortcutDetails": {"targetId": "MATH", "targetMimeType": FOLDER}},
        {"id": "SC2", "name": "loop", "mimeType": SHORTCUT, "parent": "MATH",
         "shortcutDetails": {"targetId": ROOT_FOLDER, "targetMimeType": FOLDER}},
        # a secret file the service account can see but is NOT in the course folder
        pdf("SECRET", "salaries.pdf", "ELSEWHERE", "2026-01-01T00:00:00.000Z"),
    ]
    return {i["id"]: i for i in items}


class _Exec:
    def __init__(self, fn):
        self.fn = fn

    def execute(self):
        return self.fn()


class FakeFiles:
    def __init__(self, drive):
        self.d = drive

    def list(self, q="", fields=None, pageToken=None, **k):
        self.d.list_calls += 1
        parent = re.search(r"'([^']+)' in parents", q).group(1)
        only_folders = "mimeType = 'application/vnd.google-apps.folder'" in q
        files = [dict(v) for v in self.d.tree.values() if v["parent"] == parent
                 and (not only_folders or v["mimeType"] == FOLDER)]
        return _Exec(lambda: {"files": files})

    def get(self, fileId, fields=None, **k):
        return _Exec(lambda: dict(self.d.tree[fileId]))

    def get_media(self, fileId, **k):
        self.d.downloads.append(fileId)
        return ("media", fileId)

    def update(self, **k):
        return _Exec(lambda: {})


class FakeDrive:
    def __init__(self):
        self.tree = make_tree()
        self.list_calls = 0
        self.downloads = []

    def files(self):
        return FakeFiles(self)


class FakeDownloader:
    def __init__(self, stream, request):
        self.stream, self.request = stream, request

    def next_chunk(self):
        self.stream.write(b"%PDF-1.4 fake " + request_id(self.request).encode())
        return None, True


def request_id(req):
    return req[1]


# ---------------- environment ----------------
def _service_account():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    info = {"type": "service_account", "project_id": "t", "private_key_id": "x",
            "private_key": pem, "client_email": "bot@t.iam.gserviceaccount.com",
            "client_id": "1", "token_uri": "https://oauth2.googleapis.com/token"}
    return base64.b64encode(json.dumps(info).encode()).decode()


os.environ.update({
    "Google_Credentials_bot": _service_account(),
    "Drive_ID": ROOT_FOLDER,
    "tasks_cache_File_ID": "TASKIMG",
    "BOT_TOKEN": BOT_TOKEN,
    "KV_REST_API_URL": "https://fake.upstash.io",
    "KV_REST_API_TOKEN": "x",
    "WEBHOOK_SECRET": "hook-secret",
    "CRON_SECRET": "cron-secret",
    "WEBAPP_URL": "https://uotce.example/app/index.html",
})
os.environ.pop("NOTION_TOKEN", None)

import upstash_redis  # noqa: E402
import googleapiclient.discovery  # noqa: E402

upstash_redis.Redis = FakeRedis
_drive = FakeDrive()
googleapiclient.discovery.build = lambda *a, **k: _drive

import config  # noqa: E402,F401  (now built on the fakes)


class Sent(list):
    def of(self, kind):
        return [s for s in self if s[0] == kind]


@pytest.fixture
def env(monkeypatch):
    import config as cfg
    import drive_index as di
    import functions_bot as fb

    fake_redis = FakeRedis()
    drive = FakeDrive()
    for mod_name in ("config", "functions_bot", "campus", "fun", "webapp_api", "main"):
        mod = sys.modules.get(mod_name) or __import__(mod_name)
        if hasattr(mod, "redis"):
            monkeypatch.setattr(mod, "redis", fake_redis)
        if hasattr(mod, "Drive_service"):
            monkeypatch.setattr(mod, "Drive_service", drive)
    di._memory.update(index=None, at=0)
    import webapp_api
    webapp_api._hits.clear()
    monkeypatch.setattr(fb, "MediaIoBaseDownload", FakeDownloader)
    monkeypatch.setattr(fb.time, "sleep", lambda s: None)

    sent = Sent()
    bot = cfg.bot
    counter = {"n": 100}

    def msg(chat_id, **extra):
        counter["n"] += 1
        m = pytypes.SimpleNamespace(message_id=counter["n"], chat=pytypes.SimpleNamespace(id=chat_id))
        m.__dict__.update(extra)
        return m

    def send_message(chat_id, text, **k):
        if chat_id in getattr(bot, "_blocked", set()):
            from telebot.apihelper import ApiTelegramException
            raise ApiTelegramException("sendMessage", None, {"error_code": 403, "description": "Forbidden: bot was blocked"})
        sent.append(("message", chat_id, text, k))
        return msg(chat_id)

    def send_document(chat_id, document, **k):
        sent.append(("document", chat_id, document, k))
        doc_id = document if isinstance(document, str) else f"TG-{document[0]}"
        return msg(chat_id, document=pytypes.SimpleNamespace(file_id=doc_id))

    monkeypatch.setattr(bot, "send_message", send_message)
    monkeypatch.setattr(bot, "send_document", send_document)
    monkeypatch.setattr(bot, "reply_to", lambda m, text, **k: sent.append(("reply", m.chat.id, text, k)))
    monkeypatch.setattr(bot, "delete_message", lambda *a, **k: sent.append(("delete",) + a))
    monkeypatch.setattr(bot, "answer_callback_query", lambda *a, **k: None)
    monkeypatch.setattr(bot, "edit_message_reply_markup", lambda **k: sent.append(("edit", k)))
    monkeypatch.setattr(bot, "get_me", lambda: pytypes.SimpleNamespace(username="uotce_bot"))
    import campus
    campus._botname["v"] = None
    bot._blocked = set()
    return pytypes.SimpleNamespace(redis=fake_redis, drive=drive, sent=sent, bot=bot)
