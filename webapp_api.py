# api for the mini app (public/app/index.html)
# every endpoint checks telegram initData so nobody can fake another student
import hmac
import logging
import os
import time
from datetime import datetime, timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from telebot.apihelper import ApiTelegramException
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

import campus as cp
import fun
import drive_index as di
import tasks_source as ts
from config import ADMIN_ID, CRON_SECRET, Drive_ID, Drive_service, TOKEN, WEBAPP_URL, bot, redis
from functions_bot import safe_api_call, send_drive_file
from tg_auth import validate_init_data

router = APIRouter(prefix="/api")

FAV_KEY = "miniapp:fav:{uid}"
USERS_KEY = cp.USERS_KEY
REMIND_KEY = cp.REMIND_KEY
REMINDER_WINDOWS = [(3, "⏰ باقي أقل من 3 ساعات"), (24, "📌 باقي أقل من يوم")]



def _rate_limit(uid, bucket, per_minute):
    key = f"rl:{bucket}:{uid}:{int(time.time() // 60)}"
    try:
        count = redis.incr(key)
        if count == 1:
            redis.expire(key, 70)
    except Exception:
        return  # if redis fails dont block anyone
    if count > per_minute:
        raise HTTPException(429, "rate_limited")


# general limit kept in memory so normal browsing costs zero redis commands
# (upstash free = 500k commands/month). sending files, votes and games still use redis
_hits = {}


def _memory_limit(uid, per_minute=120):
    minute = int(time.time() // 60)
    key = (uid, minute)
    _hits[key] = _hits.get(key, 0) + 1
    if len(_hits) > 5000:
        for k in [k for k in _hits if k[1] < minute]:
            _hits.pop(k, None)
    if _hits[key] > per_minute:
        raise HTTPException(429, "rate_limited")


def current_user(request: Request):
    user = validate_init_data(request.headers.get("X-Telegram-Init-Data", ""), TOKEN)
    if not user:
        raise HTTPException(401, "invalid_init_data")
    _memory_limit(user["id"])
    return user


def admin_user(user=Depends(current_user)):
    if not cp.is_admin_id(user["id"]):
        raise HTTPException(403, "admin_only")
    return user


def _index(force=False):
    try:
        return di.get_index(Drive_service, redis, Drive_ID, force=force)
    except Exception:
        logging.exception("building drive index failed")
        raise HTTPException(503, "drive_unavailable")


def _app_button(text, fragment=""):
    if not WEBAPP_URL:
        return None
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton(text, web_app=WebAppInfo(WEBAPP_URL + fragment)))
    return kb


def _dm(uid, text):
    # 409 = user never pressed Start in the bot, the app shows them a button for it
    try:
        bot.send_message(uid, text)
    except ApiTelegramException as e:
        if e.error_code in (400, 403):
            raise HTTPException(409, "start_bot_first")
        raise


async def _json(request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "bad_json")
    if not isinstance(body, dict):
        raise HTTPException(400, "bad_json")
    return body



@router.get("/bootstrap")
def bootstrap(user=Depends(current_user)):
    uid = user["id"]
    try:
        redis.sadd(USERS_KEY, uid)
        reminders = bool(redis.sismember(REMIND_KEY, uid))
    except Exception:
        reminders = False
    idx = _index()
    try:
        bot_name = cp.bot_username()
    except Exception:
        bot_name = None
    return {
        "user": {"id": uid, "first_name": user.get("first_name", "")},
        "is_admin": cp.is_admin_id(uid),
        "reminders": reminders,
        "bot": bot_name,
        "root": idx["root"],
        "built": idx["built"],
        "recent": di.recent(idx),
        "subs": sorted(i for i in cp.user_subscriptions(uid) if i in idx["nodes"]),
        "section": cp.get_section(uid),
        "announcements": cp.active_announcements(),
    }


@router.get("/folder/{folder_id}")
def folder(folder_id: str, user=Depends(current_user)):
    idx = _index()
    if folder_id == "root":
        folder_id = idx["root"]
    node = idx["nodes"].get(folder_id)
    if not node or node["kind"] != "folder":
        raise HTTPException(404, "not_found")
    return {"folder": di.public(node, idx), "breadcrumb": di.breadcrumb(idx, folder_id),
            "items": di.list_folder(idx, folder_id),
            "subscribed": folder_id in cp.user_subscriptions(user["id"])}


@router.get("/search")
def search(q: str = "", user=Depends(current_user)):
    if len(q) > 80:
        raise HTTPException(400, "query_too_long")
    return {"results": di.search(_index(), q)}


@router.get("/favorites")
def favorites(user=Depends(current_user)):
    idx = _index()
    ids = redis.smembers(FAV_KEY.format(uid=user["id"])) or []
    items = [di.public(idx["nodes"][i], idx, with_path=True) for i in ids if i in idx["nodes"]]
    items.sort(key=lambda n: n["name"])
    return {"items": items}


@router.post("/favorites/{item_id}")
def add_favorite(item_id: str, user=Depends(current_user)):
    if not di.contains(_index(), item_id):
        raise HTTPException(404, "not_found")
    key = FAV_KEY.format(uid=user["id"])
    if (redis.scard(key) or 0) >= 200:
        raise HTTPException(400, "too_many_favorites")
    redis.sadd(key, item_id)
    return {"ok": True}


@router.delete("/favorites/{item_id}")
def remove_favorite(item_id: str, user=Depends(current_user)):
    redis.srem(FAV_KEY.format(uid=user["id"]), item_id)
    return {"ok": True}


def _deliver(chat_id, file_id, notice_id):
    try:
        send_drive_file(chat_id, file_id)
    except Exception:
        logging.exception("mini app send failed")
        safe_api_call(bot.send_message, chat_id, "❌ صار خطأ بإرسال الملف، جرب مرة ثانية")
    finally:
        try:
            bot.delete_message(chat_id, notice_id)
        except Exception:
            pass


@router.post("/send/{file_id}")
def send_file(file_id: str, background_tasks: BackgroundTasks, user=Depends(current_user)):
    _rate_limit(user["id"], "send", 15)
    node = _index()["nodes"].get(file_id)
    if not node or node["kind"] != "file":
        raise HTTPException(404, "not_found")  # not in the course folder
    if node.get("native") or (node.get("size") or 0) > di.TG_LIMIT:
        return {"status": "link", "link": node.get("link")}
    try:
        # if this fails the user never pressed Start
        notice = bot.send_message(user["id"], f"📥 جار تجهيز {node['name']}…")
    except ApiTelegramException as e:
        if e.error_code in (400, 403):
            raise HTTPException(409, "start_bot_first")
        raise
    redis.sadd(USERS_KEY, user["id"])
    background_tasks.add_task(_deliver, user["id"], file_id, notice.message_id)
    return {"status": "queued"}


@router.get("/tasks")
def tasks(user=Depends(current_user)):
    return {"tasks": ts.get_tasks(redis), "now": datetime.now(ts.BAGHDAD).isoformat()}


@router.post("/reminders")
async def set_reminders(request: Request, user=Depends(current_user)):
    enabled = bool((await _json(request)).get("enabled"))
    (redis.sadd if enabled else redis.srem)(REMIND_KEY, user["id"])
    if enabled:
        try:
            bot.send_message(user["id"], "🔔 تم تفعيل تذكير الواجبات. راح يوصلك تنبيه قبل الموعد بيوم وقبله بـ3 ساعات.")
        except ApiTelegramException:
            redis.srem(REMIND_KEY, user["id"])
            raise HTTPException(409, "start_bot_first")
    return {"reminders": enabled}


@router.post("/admin/reindex")
def reindex(user=Depends(admin_user)):
    idx = _index(force=True)
    files = sum(1 for n in idx["nodes"].values() if n["kind"] == "file")
    return {"ok": True, "files": files, "folders": len(idx["children"])}


@router.get("/admin/stats")
def stats(user=Depends(admin_user)):
    return {"users": redis.scard(USERS_KEY) or 0, "reminders": redis.scard(REMIND_KEY) or 0}



def run_reminders(now=None):
    now = now or datetime.now(ts.BAGHDAD)
    users = [int(u) for u in (redis.smembers(REMIND_KEY) or [])]
    notified = 0
    for task in ts.get_tasks(redis, include_past_hours=0):
        left = datetime.fromisoformat(task["due"]) - now
        if left <= timedelta(0):
            continue
        for hours, label in REMINDER_WINDOWS:  # smallest window first
            if left > timedelta(hours=hours):
                continue
            if not redis.set(f"rem:{task['id']}:{hours}", "1", nx=True, ex=4 * 86400):
                break  # this window was already announced
            for bigger, _ in REMINDER_WINDOWS:  # already at 3h so skip the 24h one
                if bigger > hours:
                    redis.set(f"rem:{task['id']}:{bigger}", "1", nx=True, ex=4 * 86400)
            subject = f"\n📚 {task['subject']}" if task.get("subject") else ""
            text = f"{label}\n\n📝 {task['title']}{subject}\n🗓 {datetime.fromisoformat(task['due']).strftime('%Y-%m-%d %H:%M')}"
            for uid in users:
                try:
                    safe_api_call(bot.send_message, uid, text, reply_markup=_app_button("📋 كل الواجبات", "#tasks"))
                except ApiTelegramException as e:
                    if e.error_code == 403:  # user blocked the bot
                        cp.forget_user(uid)
                time.sleep(0.04)  # telegram limit ~30 msg/s
            notified += 1
            break
    return {"tasks_notified": notified, "users": len(users)}


def _cron_auth(request: Request):
    supplied = request.headers.get("authorization", "").removeprefix("Bearer ").strip() \
        or request.query_params.get("key", "")
    if not CRON_SECRET or not hmac.compare_digest(supplied, CRON_SECRET):
        raise HTTPException(403, "forbidden")


@router.api_route("/cron/reminders", methods=["GET", "POST"])
def cron_reminders(request: Request):
    _cron_auth(request)
    return run_reminders()


@router.api_route("/cron/tick", methods=["GET", "POST"])
def cron_tick(request: Request):
    # one url for cronPing: reminders + new files
    _cron_auth(request)
    out = {}
    try:
        out["reminders"] = run_reminders()
    except Exception:
        logging.exception("reminders failed")
        out["reminders"] = "error"
    try:
        out["files"] = cp.check_new_files()
    except Exception:
        logging.exception("new files check failed")
        out["files"] = "error"
    try:
        out["exams"] = cp.run_exam_reminders()
    except Exception:
        logging.exception("exam reminders failed")
    try:
        out["polls_closed"] = fun.close_expired_polls()
        fun.announce_week_champion()
    except Exception:
        logging.exception("polls / champion failed")
    try:
        out["dashboard"] = cp.refresh_dashboard()
    except Exception:
        logging.exception("dashboard refresh failed")
    return out



@router.get("/subscriptions")
def subscriptions(user=Depends(current_user)):
    idx = _index()
    items = [di.public(idx["nodes"][i], idx, with_path=True)
             for i in cp.user_subscriptions(user["id"]) if i in idx["nodes"]]
    return {"items": sorted(items, key=lambda n: n["name"])}


@router.post("/subscriptions/{folder_id}")
def follow(folder_id: str, user=Depends(current_user)):
    idx = _index()
    node = idx["nodes"].get(folder_id)
    if not node or node["kind"] != "folder":
        raise HTTPException(404, "not_found")
    _dm(user["id"], f"🔔 راح يوصلك إشعار هنا أول ما ينضاف ملف جديد بـ «{node['name']}»")
    try:
        cp.subscribe(user["id"], folder_id)
    except ValueError:
        raise HTTPException(400, "too_many_subscriptions")
    redis.sadd(USERS_KEY, user["id"])
    return {"subscribed": True}


@router.delete("/subscriptions/{folder_id}")
def unfollow(folder_id: str, user=Depends(current_user)):
    cp.unsubscribe(user["id"], folder_id)
    return {"subscribed": False}



@router.get("/announcements")
def announcements(user=Depends(current_user)):
    return {"items": cp.active_announcements()}


@router.post("/admin/announcements")
async def create_announcement(request: Request, background_tasks: BackgroundTasks, user=Depends(admin_user)):
    body = await _json(request)
    try:
        a = cp.add_announcement(body.get("text"), body.get("kind", "general"), body.get("days", 3),
                                by=user.get("first_name"))
    except ValueError:
        raise HTTPException(400, "empty_text")
    if body.get("broadcast", True):
        background_tasks.add_task(cp.broadcast_announcement, a)
    return {"announcement": a}


@router.delete("/admin/announcements/{aid}")
def delete_announcement(aid: str, user=Depends(admin_user)):
    cp.remove_announcement(aid)
    return {"ok": True}



@router.get("/schedule")
def schedule(user=Depends(current_user)):
    today = cp.now().date()
    return {"classes": cp.get_schedule(), "overrides": cp.get_overrides(),
            "today": today.isoformat(), "now": cp.now().isoformat()}


@router.post("/admin/schedule")
async def save_class(request: Request, user=Depends(admin_user)):
    try:
        return {"class": cp.upsert_class(await _json(request))}
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.delete("/admin/schedule/{cid}")
def remove_class(cid: str, user=Depends(admin_user)):
    cp.delete_class(cid)
    return {"ok": True}


@router.post("/admin/schedule/{cid}/override")
async def override_class(cid: str, request: Request, background_tasks: BackgroundTasks, user=Depends(admin_user)):
    body = await _json(request)
    try:
        a = cp.set_override(cid, str(body.get("date", "")), body.get("status"), body.get("room"),
                            body.get("note"), announce=bool(body.get("announce", True)),
                            by=user.get("first_name"))
    except LookupError:
        raise HTTPException(404, "not_found")
    except ValueError as e:
        raise HTTPException(400, str(e))
    if a:
        background_tasks.add_task(cp.broadcast_announcement, a)
    return {"overrides": cp.get_overrides(), "announcement": a}



# exams

@router.get("/exams")
def exams(user=Depends(current_user)):
    return {"items": cp.upcoming_exams()}


@router.post("/admin/exams")
async def save_exam(request: Request, user=Depends(admin_user)):
    try:
        return {"exam": cp.upsert_exam(await _json(request))}
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.delete("/admin/exams/{eid}")
def remove_exam(eid: str, user=Depends(admin_user)):
    cp.delete_exam(eid)
    return {"ok": True}


# absences

@router.get("/absences")
def absences(user=Depends(current_user)):
    return {**cp.absence_summary(user["id"]), "entries": cp.get_absences(user["id"])}


@router.post("/absences")
async def add_absence(request: Request, user=Depends(current_user)):
    _rate_limit(user["id"], "abs", 20)
    body = await _json(request)
    try:
        summary, warning = cp.add_absence(user["id"], str(body.get("class_id", "")), str(body.get("date", "")),
                                          bool(body.get("excused")))
    except LookupError:
        raise HTTPException(404, "not_found")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {**summary, "entries": cp.get_absences(user["id"]), "warning": warning}


@router.delete("/absences/{aid}")
def delete_absence(aid: str, user=Depends(current_user)):
    cp.remove_absence(user["id"], aid)
    return {**cp.absence_summary(user["id"]), "entries": cp.get_absences(user["id"])}


@router.post("/me/section")
async def set_section(request: Request, user=Depends(current_user)):
    return {"section": cp.set_section(user["id"], str((await _json(request)).get("section") or ""))}



# القعدة: game, leaderboard, polls

@router.post("/game/subnet/start")
def game_start(user=Depends(current_user)):
    _rate_limit(user["id"], "game", 20)
    return fun.start_round(user["id"], user.get("first_name"))


@router.post("/game/subnet/answer")
async def game_answer(request: Request, user=Depends(current_user)):
    body = await _json(request)
    choice = body.get("choice")
    try:
        return fun.answer(user["id"], str(body.get("round", "")), choice if isinstance(choice, int) else -1)
    except LookupError:
        raise HTTPException(404, "round_not_found")


@router.get("/game/leaderboard")
def game_board(which: str = "week", user=Depends(current_user)):
    return fun.leaderboard(user["id"], "all" if which == "all" else "week")


@router.get("/polls")
def polls(user=Depends(current_user)):
    return {"items": fun.list_polls(user["id"], cp.is_admin_id(user["id"]))}


@router.post("/polls/{pid}/vote")
async def poll_vote(pid: str, request: Request, user=Depends(current_user)):
    _rate_limit(user["id"], "vote", 20)
    option = (await _json(request)).get("option")
    try:
        p = fun.vote(pid, user["id"], option if isinstance(option, int) else -1)
    except LookupError:
        raise HTTPException(404, "not_found")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"poll": fun.poll_view(p, user["id"], cp.is_admin_id(user["id"]))}


@router.post("/admin/polls")
async def poll_create(request: Request, user=Depends(admin_user)):
    body = await _json(request)
    try:
        p = fun.create_poll(body.get("question"), body.get("options"), body.get("hours", 24), by=user.get("first_name"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"poll": fun.poll_view(p, user["id"], True)}


@router.post("/admin/polls/{pid}/close")
def poll_close(pid: str, user=Depends(admin_user)):
    try:
        p = fun.close_poll(pid)
    except LookupError:
        raise HTTPException(404, "not_found")
    return {"poll": fun.poll_view(p, user["id"], True)}


@router.delete("/admin/polls/{pid}")
def poll_delete(pid: str, user=Depends(admin_user)):
    fun.delete_poll(pid)
    return {"ok": True}
