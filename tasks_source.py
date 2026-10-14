# homework deadlines as data (not a screenshot)
# 1) notion database, columns detected by type so names dont matter
# 2) tasks admins add with /addtask
import hashlib
import json
import logging
import os
from datetime import datetime, timedelta, timezone

import requests

BAGHDAD = timezone(timedelta(hours=3))  # iraq has no DST
CACHE_KEY = "miniapp:tasks:notion"
MANUAL_KEY = "miniapp:tasks:manual"
CACHE_TTL = 300
DONE_WORDS = {"done", "complete", "completed", "منجز", "تم", "مكتمل", "انتهى"}


def _plain(rich):
    return "".join(r.get("plain_text", "") for r in (rich or [])).strip()


def _parse_date(value):
    # notion sends '2026-10-20' or '2026-10-20T14:00:00.000+03:00'
    if not value:
        return None
    if len(value) == 10:  # no time given = end of the day
        d = datetime.fromisoformat(value)
        return d.replace(hour=23, minute=59, tzinfo=BAGHDAD)
    d = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=BAGHDAD)


def parse_notion_page(page):
    title = subject = due = None
    done = False
    for prop in page.get("properties", {}).values():
        t = prop.get("type")
        if t == "title" and title is None:
            title = _plain(prop.get("title"))
        elif t == "date" and due is None and prop.get("date"):
            due = _parse_date(prop["date"].get("start"))
        elif t == "select" and subject is None and prop.get("select"):
            subject = prop["select"].get("name")
        elif t == "multi_select" and subject is None and prop.get("multi_select"):
            subject = ", ".join(o["name"] for o in prop["multi_select"])
        elif t == "checkbox" and prop.get("checkbox"):
            done = True
        elif t == "status" and prop.get("status"):
            done = done or prop["status"].get("name", "").strip().lower() in DONE_WORDS
    if not title or not due or done:
        return None
    return {"id": page["id"], "title": title, "subject": subject,
            "due": due.isoformat(), "url": page.get("public_url") or page.get("url")}


def fetch_notion(token, db_id):
    tasks, cursor = [], None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        r = requests.post(
            f"https://api.notion.com/v1/databases/{db_id}/query",
            headers={"Authorization": f"Bearer {token}", "Notion-Version": "2022-06-28",
                     "Content-Type": "application/json"},
            json=body, timeout=20)
        r.raise_for_status()
        data = r.json()
        tasks.extend(t for t in map(parse_notion_page, data.get("results", [])) if t)
        if not data.get("has_more"):
            return tasks
        cursor = data.get("next_cursor")


def manual_tasks(redis):
    raw = redis.get(MANUAL_KEY)
    return json.loads(raw) if raw else []


def add_manual_task(redis, subject, title, due: datetime):
    tasks = manual_tasks(redis)
    tid = "m" + hashlib.sha1(f"{subject}{title}{due.isoformat()}".encode()).hexdigest()[:10]
    tasks.append({"id": tid, "title": title, "subject": subject, "due": due.isoformat()})
    redis.set(MANUAL_KEY, json.dumps(tasks, ensure_ascii=False))
    return tid


def remove_manual_task(redis, number):
    tasks = sorted(manual_tasks(redis), key=lambda t: t["due"])
    if not 1 <= number <= len(tasks):
        return None
    removed = tasks.pop(number - 1)
    redis.set(MANUAL_KEY, json.dumps(tasks, ensure_ascii=False))
    return removed


def get_tasks(redis, include_past_hours=24):
    tasks = []
    token, db_id = os.getenv("NOTION_TOKEN"), os.getenv("NOTION_DB_ID")
    if token and db_id:
        cached = redis.get(CACHE_KEY)
        if cached:
            tasks = json.loads(cached)
        else:
            try:
                tasks = fetch_notion(token, db_id)
                redis.set(CACHE_KEY, json.dumps(tasks, ensure_ascii=False), ex=CACHE_TTL)
            except Exception:
                logging.exception("notion fetch failed")
    tasks = tasks + manual_tasks(redis)

    cutoff = datetime.now(BAGHDAD) - timedelta(hours=include_past_hours)
    tasks = [t for t in tasks if datetime.fromisoformat(t["due"]) >= cutoff]
    return sorted(tasks, key=lambda t: t["due"])


def parse_due(text):
    # '2026-10-20 14:00' / '2026-10-20' / '20/10' / '20/10 14:00'
    text = text.strip()
    now = datetime.now(BAGHDAD)
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d", "%d/%m/%Y %H:%M", "%d/%m/%Y",
                "%d/%m %H:%M", "%d/%m"):
        no_year = "%Y" not in fmt
        try:
            if no_year:  # add the year ourselves (python warns otherwise)
                day, _, clock = text.partition(" ")
                d = datetime.strptime(f"{day}/{now.year} {clock}".strip(),
                                      fmt.replace("%d/%m", "%d/%m/%Y", 1))
            else:
                d = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if no_year:
            if d.replace(tzinfo=BAGHDAD) < now - timedelta(days=1):
                d = d.replace(year=now.year + 1)
        if "%H" not in fmt:
            d = d.replace(hour=23, minute=59)
        return d.replace(tzinfo=BAGHDAD)
    return None
