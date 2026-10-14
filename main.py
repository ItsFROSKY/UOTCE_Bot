from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload
import io
import time
import urllib.parse
import requests
import json
import hmac
import logging
from pathlib import Path
from upstash_redis import Redis
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from PIL import Image
from config import*
from bot_texts import*
from functions_bot import*
import campus as cp
import fun
import drive_index as di
import tasks_source as ts
import datetime
from datetime import timedelta
from webapp_api import router as webapp_router







#logging to see errors in vercel logs
logging.basicConfig(level=logging.INFO)
app = FastAPI()
app.include_router(webapp_router)
WEBAPP_FILE = Path(__file__).parent / "public" / "app" / "index.html"


@app.get("/")
def home():
    return {"status": "online", "message": "bot is running on vercel"}


@app.get("/app")
def miniapp():
    # vercel serves public/ by itself, this is just for local testing
    if WEBAPP_FILE.exists():
        return FileResponse(WEBAPP_FILE)
    return RedirectResponse("/app/index.html")


@app.post("/webhook")
async def webhook_handler(request: Request, background_tasks: BackgroundTasks):
    # without this anyone can send a fake update with an admin id to /webhook
    if WEBHOOK_SECRET:
        got = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(got, WEBHOOK_SECRET):
            return JSONResponse({"status": "forbidden"}, status_code=403)
    else:
        logging.warning("WEBHOOK_SECRET is not set: anyone can post fake updates to /webhook")
    try:
        json_data = await request.json()
        update = types.Update.de_json(json_data)

        if update is not None:
            #offload processing so tele gets an instant 200 OK
            background_tasks.add_task(bot.process_new_updates, [update])

        return {"status": "ok"}
    except Exception as e:
        logging.error(f"Error handling update: {e}")
        return {"status": "error"}

#bot setup

def is_admin(message):
    return message.from_user is not None and cp.is_admin_id(message.from_user.id)


def app_keyboard(chat_type):
    if not WEBAPP_URL:
        return None
    # web_app buttons dont work in groups so cp.app_button gives a t.me link there
    private = chat_type == "private"
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(cp.app_button("📚 مكتبة القسم", "files", private))
    kb.add(cp.app_button("📅 الجدول", "schedule", private), cp.app_button("📋 الواجبات", "tasks", private))
    return kb


STUDENT_COMMANDS = [
    ("start", "فتح التطبيق"),
    ("help", "كل الأوامر"),
    ("app", "فتح التطبيق (يشتغل بالكروب)"),
    ("schedule", "محاضرات اليوم وباچر"),
    ("exams", "الامتحانات القادمة"),
    ("top", "أبطال Subnet Rush"),
    ("drive", "تصفح الملفات بالأزرار"),
    ("tasks", "صورة الواجبات"),
    ("source", "الكود المصدري"),
    ("myid", "رقمك بالتليكرام (ID)"),
]
ADMIN_COMMANDS = [
    ("announce", "إعلان لكل الطلاب والكروب"),
    ("pin_app", "تثبيت رسالة التطبيق بالكروب"),
    ("set_group", "تحديد كروب الدفعة (داخل الكروب)"),
    ("unset_group", "إيقاف النشر بالكروب"),
    ("schedule_set", "إدخال الجدول كامل"),
    ("schedule_add", "إضافة محاضرة للجدول"),
    ("exam_add", "إضافة امتحان"),
    ("poll", "تصويت جديد للدفعة"),
    ("exam_del", "حذف امتحان"),
    ("check_files", "فحص الملفات الجديدة هسه"),
    ("backup", "نسخة احتياطية من الجدول والبيانات"),
    ("admins", "قائمة الأدمنية"),
    ("add_admin", "إضافة أدمن (للمالك)"),
    ("remove_admin", "شيل أدمن (للمالك)"),
    ("addtask", "إضافة واجب"),
    ("deltask", "حذف واجب"),
    ("reindex", "تحديث فهرس الملفات"),
    ("setup_app", "تفعيل زر المكتبة وقائمة الأوامر"),
    ("tasks_update", "تحديث صورة الواجبات من Notion"),
    ("update_tele", "رفع ملفات الـ Drive لكروب المواد"),
    ("reset_topics", "تصفير مواضيع كروب المواد"),
    ("groupid", "آيدي الكروب"),
]


@bot.message_handler(commands=['help'])
def send_help(message):
    lines = ["📖 أوامر البوت", ""]
    lines += [f"/{c} - {d}" for c, d in STUDENT_COMMANDS]
    if is_admin(message):
        lines += ["", "🔧 أوامر الأدمن", ""]
        lines += [f"/{c} - {d}" for c, d in ADMIN_COMMANDS]
    lines += ["", "📱 من التطبيق تكدر:",
              "• تبحث بكل ملفات المواد وتستلمها بدوسة",
              "• تتابع أي مادة ويوصلك إشعار بكل ملف جديد",
              "• تشوف الجدول والإعلانات (تأجيل، قاعات)",
              "• تسجل غياباتك وتعرف كم باقيلك قبل الحد",
              "• تشوف الامتحانات ويا عداد تنازلي",
              "• تلعب Subnet Rush وتنافس الدفعة، وتصوّت بالتصويتات",
              "• تفعّل تذكير الواجبات",
              "", "Programmed by Taha AlAnbagi #1"]
    bot.send_message(message.chat.id, "\n".join(lines), reply_markup=app_keyboard(message.chat.type))


@bot.message_handler(commands=['start'])
def send_welcome(message):
    name = message.from_user.first_name if message.from_user else ""
    if message.chat.type == "private" and message.from_user:
        redis.sadd(cp.USERS_KEY, message.from_user.id)  # so he gets announcements
    text = (f"هلا {name} 👋\n\n"
            "هذا بوت قسم هندسة الحاسوب – الجامعة التكنولوجية.\n"
            "• كل ملفات المواد مع بحث فوري\n"
            "• جدول المحاضرات والإعلانات (تأجيل، قاعات)\n"
            "• تابع أي مادة ويوصلك إشعار أول ما تنزل محاضرة جديدة\n"
            "• الواجبات ومواعيدها مع تذكير تلقائي\n\n"
            "دوس الزر حتى تفتح التطبيق، أو /help حتى تشوف كل الأوامر\n\n"
            "Programmed by Taha AlAnbagi #1")
    bot.send_message(message.chat.id, text, reply_markup=app_keyboard(message.chat.type))


@bot.message_handler(commands=['app'])
def send_app(message):
    if not WEBAPP_URL:
        return bot.reply_to(message, "❌ WEBAPP_URL غير مضبوط")
    if message.chat.type in ("group", "supergroup"):
        # same buttons + today's lectures as the pinned message
        return bot.reply_to(message, cp.dashboard_text(), reply_markup=cp.dashboard_markup(),
                            disable_web_page_preview=True)
    bot.send_message(message.chat.id, "📚 مكتبة القسم", reply_markup=app_keyboard(message.chat.type))


@bot.message_handler(commands=["pin_app"])
def pin_app(message):
    if not is_admin(message):
        return bot.reply_to(message,"❌ ما عندك صلاحية")
    if message.chat.type not in ("group", "supergroup"):
        return bot.reply_to(message, "❌ دز هذا الأمر داخل كروب الدفعة")
    thread = message.message_thread_id if getattr(message, "is_topic_message", False) else None
    if not cp._load(cp.GROUP_KEY, None):
        cp.set_group(message.chat.id, thread)  # first time = this is the batch group
    if not cp.pin_dashboard(message.chat.id, thread):
        bot.send_message(message.chat.id, "⚠️ انرسلت الرسالة بس ما كدرت أثبتها. خلي البوت أدمن ويا صلاحية Pin messages",
                         **({"message_thread_id": thread} if thread else {}))
    try:
        bot.delete_message(message.chat.id, message.message_id)  # keep the group clean
    except Exception:
        pass


@bot.callback_query_handler(func=lambda call: True)
def handle_drive_click(call):
    #Stop the loading spinner on the user button OTHERWISE it will lag
    bot.answer_callback_query(call.id)
    action, _, target_id = (call.data or "").partition(":") #split the prefix and the target id, use each one on its own
    if action not in ("dir", "file") or not target_id:
        return

    # callback data can be faked with a userbot, only allow ids from the course folder
    try:
        idx = di.get_index(Drive_service, redis, Drive_ID)
    except Exception:
        logging.exception("drive index unavailable")
        return bot.send_message(call.message.chat.id, "❌ الـ Drive مو متاح هسه، جرب بعد شوية")
    node = idx["nodes"].get(target_id)
    if not node or node["kind"] != ("folder" if action == "dir" else "file"):
        return bot.send_message(call.message.chat.id, "❌ الملف غير متاح")

    if action == "dir":
        new_menu = Google_menu(target_id, bot, Drive_service)
        bot.edit_message_reply_markup(chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=new_menu)

    elif action == "file":
        bot.send_message(call.message.chat.id, "....📥جار التحميل")
        download_and_send_file(target_id, call.message.chat.id, call)


@bot.message_handler(commands=['drive'])
def handle_drive(message):
    bot.send_message(message.chat.id, "loading")
    menu = Google_menu(Drive_ID, bot, Drive_service)
    bot.send_message(message.chat.id, "اختر الملف", reply_markup=menu)

@bot.message_handler(commands=['source'])
def source_text_end(message):
    bot.send_message(message.chat.id, "الكود OpenSource تكدر تشارك ببناءه")
    bot.send_message(message.chat.id, "github.com/ItsFROSKY/UOTCE_Bot/")
    bot.send_message(message.chat.id, "Mini App: Programmed by Taha AlAnbagi #1")


TARGET_URL="https://pouncing-donut-8de.notion.site/9fc6e320cbbd82cca21b81bcd086ac05?v=3366e320cbbd8082b0db000c05774ca"
TASKS_FILE_ID = os.getenv("TASKS_FILE_ID")
TASKS_FILE_ID_KEY = "tasks_file_id"
@bot.message_handler(commands=["tasks"])
def tasks(message):
    try:
        file_id = redis.get(TASKS_FILE_ID_KEY) or TASKS_FILE_ID
        if not file_id:
            return bot.reply_to(message,"أستخدم /Tasks_update أولاً❌ ")
        bot.send_photo(message.chat.id, file_id, reply_markup=app_keyboard(message.chat.type))
    except Exception as e:
        logging.exception("tasks failed")
        bot.reply_to(message,"❌ صار خطأ، جرب بعد شوية")




@bot.message_handler(commands=["tasks_update"])
def tasks_update(message):
    if not is_admin(message):
        return bot.reply_to(message,"❌ ما عندك صلاحية")
    bot.send_message(message.chat.id,"🔄 Updating...")
    try:
        token=os.getenv("BROWSERLESS_TOKEN")
        if not token:
            raise RuntimeError("BROWSERLESS_TOKEN is missing")
        #the browserless settings
        code=f"""
        export default async ({{page}})=>{{
        await page.setViewport({{width:2000,height:1600,deviceScaleFactor:2}});
        await page.emulateMediaFeatures([{{name:"prefers-color-scheme",value:"dark"}}]);
        await page.goto({json.dumps(TARGET_URL.split("?")[0])},{{waitUntil:"networkidle2",timeout:30000}});
        await new Promise(r=>setTimeout(r,10000));
        if(document.fonts)await document.fonts.ready;
        await new Promise(r=>setTimeout(r,1000));
        return await page.screenshot({{type:"png",fullPage:false}});
        }};"""
        r=requests.post("https://production-sfo.browserless.io/function",params={"token":token},headers={"Content-Type":"application/javascript"},data=code,timeout=60)
        if not r.ok:
            raise RuntimeError(f"Browserless {r.status_code}: {r.text}")
        image_bytes=r.content
        img=Image.open(io.BytesIO(image_bytes))
        left,top,right,bottom=150,150,2842,2150
        width,height=img.size
        img=img.crop((left,top,width-right,height-bottom))
        output=io.BytesIO()
        img.save(output,format="PNG")
        output.seek(0)
        image_bytes=output.getvalue()
        media=MediaIoBaseUpload(io.BytesIO(image_bytes),mimetype="image/png",resumable=False)
        Drive_service.files().update(fileId=tasks_cache_File_ID,media_body=media).execute() # type: ignore
        sent=bot.send_photo(message.chat.id,io.BytesIO(image_bytes),caption="✅ Updated")
        if not sent or not sent.photo:
            raise RuntimeError("Telegram did not return a photo message")
        redis.set(TASKS_FILE_ID_KEY, sent.photo[-1].file_id)
    except Exception as e:
        logging.exception("tasks_update failed")
        bot.reply_to(message,f"❌ {type(e).__name__}: {e}")

@bot.message_handler(commands=["groupid"])
def get_group_id(message):
    if not is_admin(message):
        return bot.reply_to(message,"❌ ما عندك صلاحية")
    if message.chat.type not in ["group","supergroup"]:
        return bot.reply_to(message,"❌ استخدم الأمر داخل الكروب")
    bot.reply_to(message,f"🆔 Group ID:\n`{message.chat.id}`",parse_mode="Markdown")

@bot.message_handler(commands=["update_tele"])
def Dive_to_telegram(message):
    if not is_admin(message):
            return bot.reply_to(message,"❌ ما عندك صلاحية")
    state = load_state()
    create_topic(state, message)

@bot.message_handler(commands=['reset_topics'])
def handle_reset(message):
    if not is_admin(message):
        return bot.reply_to(message,"❌ ما عندك صلاحية")
    state = load_state()
    state["topic_map"] = {}
    state["sent_files"] = []
    save_state(state)
    bot.reply_to(message, "✅ Topic map cleared! Run /update_tele again.")



@bot.message_handler(commands=["reindex"])
def reindex(message):
    if not is_admin(message):
        return bot.reply_to(message,"❌ ما عندك صلاحية")
    idx = di.get_index(Drive_service, redis, Drive_ID, force=True)
    files = sum(1 for n in idx["nodes"].values() if n["kind"] == "file")
    bot.reply_to(message, f"✅ تم تحديث الفهرس: {files} ملف، {len(idx['children'])} فولدر")


@bot.message_handler(commands=["addtask"])
def add_task(message):
    if not is_admin(message):
        return bot.reply_to(message,"❌ ما عندك صلاحية")
    usage = "الصيغة:\n/addtask المادة | الواجب | 2026-10-20 14:00\n(أو 20/10 أو 20/10 14:00)"
    parts = [p.strip() for p in (message.text or "").partition(" ")[2].split("|")]
    if len(parts) != 3 or not all(parts):
        return bot.reply_to(message, usage)
    due = ts.parse_due(parts[2])
    if not due:
        return bot.reply_to(message, "❌ التاريخ غير مفهوم\n" + usage)
    ts.add_manual_task(redis, parts[0], parts[1], due)
    bot.reply_to(message, f"✅ انضاف: {parts[1]} ({parts[0]})\n🗓 {due.strftime('%Y-%m-%d %H:%M')}")


@bot.message_handler(commands=["deltask"])
def del_task(message):
    if not is_admin(message):
        return bot.reply_to(message,"❌ ما عندك صلاحية")
    arg = (message.text or "").partition(" ")[2].strip()
    if not arg.isdigit():
        tasks_list = sorted(ts.manual_tasks(redis), key=lambda t: t["due"])
        if not tasks_list:
            return bot.reply_to(message, "ماكو واجبات مضافة يدوياً")
        lines = [f"{i}. {t['title']} ({t['subject']}) – {t['due'][:16].replace('T', ' ')}"
                 for i, t in enumerate(tasks_list, 1)]
        return bot.reply_to(message, "دز /deltask مع الرقم:\n" + "\n".join(lines))
    removed = ts.remove_manual_task(redis, int(arg))
    bot.reply_to(message, f"🗑 انحذف: {removed['title']}" if removed else "❌ رقم غير صحيح")


@bot.message_handler(commands=["setup_app"])
def setup_app(message):
    if not is_admin(message):
        return bot.reply_to(message,"❌ ما عندك صلاحية")
    if not WEBAPP_URL:
        return bot.reply_to(message, "❌ اضبط WEBAPP_URL أولاً")
    bot.set_chat_menu_button(menu_button=types.MenuButtonWebApp("📚 المكتبة", types.WebAppInfo(WEBAPP_URL)))
    student = [types.BotCommand(c, d) for c, d in STUDENT_COMMANDS]
    bot.set_my_commands(student)
    # admins get their commands in the / menu too (only if they started the bot)
    for admin in [a["id"] for a in cp.list_admins()]:
        try:
            bot.set_my_commands(student + [types.BotCommand(c, d) for c, d in ADMIN_COMMANDS],
                                scope=types.BotCommandScopeChat(admin))
        except Exception:
            logging.warning(f"couldnt set admin commands for {admin}")
    bot.reply_to(message, "✅ زر المكتبة وقائمة الأوامر صارت لكل المستخدمين")



def _args(message):
    return (message.text or "").partition(" ")[2].strip()


@bot.message_handler(commands=["set_group"])
def set_group(message):
    if not is_admin(message):
        return bot.reply_to(message,"❌ ما عندك صلاحية")
    if message.chat.type not in ("group", "supergroup"):
        return bot.reply_to(message, "❌ دز هذا الأمر داخل كروب الدفعة (وبالموضوع اللي تريده إذا الكروب بيه مواضيع)")
    thread = message.message_thread_id if getattr(message, "is_topic_message", False) else None
    cp.set_group(message.chat.id, thread)
    bot.reply_to(message, "✅ هذا الكروب صار كروب الدفعة: الإعلانات والملفات الجديدة تنزل هنا",
                 reply_markup=app_keyboard(message.chat.type))


@bot.message_handler(commands=["unset_group"])
def unset_group(message):
    if not is_admin(message):
        return bot.reply_to(message,"❌ ما عندك صلاحية")
    cp.clear_group()
    bot.reply_to(message, "✅ توقف النشر بالكروب")


@bot.message_handler(commands=["announce"])
def announce(message):
    if not is_admin(message):
        return bot.reply_to(message,"❌ ما عندك صلاحية")
    text = _args(message)
    if not text:
        return bot.reply_to(message, "الصيغة:\n/announce نص الإعلان\n\nيوصل لكل الطلاب ويطلع بالتطبيق 3 أيام")
    a = cp.add_announcement(text, "general", by=message.from_user.first_name)
    result = cp.broadcast_announcement(a)
    bot.reply_to(message, f"✅ انرسل الإعلان لـ {result['users']} طالب" + (" وللكروب" if result["group"] else ""))


@bot.message_handler(commands=["schedule"])
def schedule(message):
    today = cp.now().date()
    text = cp.day_text(today) + "\n\n" + cp.day_text(today + timedelta(days=1))
    bot.send_message(message.chat.id, text, reply_markup=cp.markup(
        cp.app_button("📅 الجدول الكامل", "schedule", message.chat.type == "private")))


SCHEDULE_FORMAT = ("كل محاضرة بسطر:\n"
                   "اليوم | البداية-النهاية | المادة | القاعة | نظري/عملي | الشعبة\n\n"
                   "مثال:\n/schedule_set\n"
                   "الأحد | 08:30-10:30 | Digital Logic | قاعة 3 | نظري\n"
                   "الأحد | 10:30-12:30 | Electronics | مختبر 2 | عملي | A")


@bot.message_handler(commands=["schedule_set", "schedule_add"])
def schedule_set(message):
    if not is_admin(message):
        return bot.reply_to(message,"❌ ما عندك صلاحية")
    command, _, body = (message.text or "").partition("\n")
    if not body.strip():
        body = _args(message)  # /schedule_add in one line
    classes, errors = cp.parse_schedule_text(body)
    if errors or not classes:
        return bot.reply_to(message, "❌ " + ("\n".join(errors[:10]) or "ماكو محاضرات") + "\n\n" + SCHEDULE_FORMAT)
    if command.startswith("/schedule_set"):
        cp.replace_schedule(classes)
        bot.reply_to(message, f"✅ انحفظ الجدول: {len(classes)} محاضرة")
    else:
        for c in classes:
            cp.upsert_class(c)
        bot.reply_to(message, f"✅ انضافت {len(classes)} محاضرة")


@bot.message_handler(commands=["check_files"])
def check_files(message):
    if not is_admin(message):
        return bot.reply_to(message,"❌ ما عندك صلاحية")
    r = cp.check_new_files()
    if "seeded" in r:
        return bot.reply_to(message, f"✅ تم تسجيل {r['seeded']} ملف. من هسه أي ملف جديد يوصل إشعاره")
    bot.reply_to(message, f"✅ ملفات جديدة: {r['new']}، إشعارات انرسلت: {r.get('notified', 0)}")


EXAM_FORMAT = ("الصيغة:\n/exam_add المادة | النوع | التاريخ | الوقت | القاعة\n"
               "مثال:\n/exam_add Digital Logic | شهري | 25/10 | 09:00 | قاعة 3\n"
               "(الوقت والقاعة اختياري)")


@bot.message_handler(commands=["exams"])
def exams(message):
    items = cp.upcoming_exams()
    if not items:
        return bot.send_message(message.chat.id, "📝 ماكو امتحانات قادمة")
    lines = ["📝 الامتحانات القادمة", ""]
    for e in items:
        lines.append(f"• {e['subject']} ({e['kind']}) - {cp.when_text(e['days_left'])}\n   {cp.nice_date(datetime.date.fromisoformat(e['date']))}"
                     + (f" · {e['time']}" if e.get("time") else "") + (f" · {e['room']}" if e.get("room") else ""))
    bot.send_message(message.chat.id, "\n".join(lines), reply_markup=cp.markup(
        cp.app_button("📅 الجدول", "schedule", message.chat.type == "private")))


@bot.message_handler(commands=["exam_add"])
def exam_add(message):
    if not is_admin(message):
        return bot.reply_to(message,"❌ ما عندك صلاحية")
    try:
        e = cp.upsert_exam(cp.parse_exam_line(_args(message)))
    except ValueError as err:
        return bot.reply_to(message, f"❌ {err}\n\n{EXAM_FORMAT}")
    bot.reply_to(message, f"✅ انضاف امتحان {e['subject']} ({e['kind']}) يوم {cp.nice_date(datetime.date.fromisoformat(e['date']))}\n"
                          "راح يوصل تذكير للكل قبله بأسبوع وبيوم")


@bot.message_handler(commands=["exam_del"])
def exam_del(message):
    if not is_admin(message):
        return bot.reply_to(message,"❌ ما عندك صلاحية")
    items = cp.upcoming_exams()
    arg = _args(message)
    if not arg.isdigit() or not 1 <= int(arg) <= len(items):
        if not items:
            return bot.reply_to(message, "ماكو امتحانات")
        return bot.reply_to(message, "دز /exam_del ويا الرقم:\n" + "\n".join(
            f"{i}. {e['subject']} ({e['kind']}) {e['date']}" for i, e in enumerate(items, 1)))
    e = items[int(arg) - 1]
    cp.delete_exam(e["id"])
    bot.reply_to(message, f"🗑 انحذف امتحان {e['subject']}")


@bot.message_handler(commands=["top"])
def top(message):
    board = fun.leaderboard(message.from_user.id if message.from_user else 0, "week")
    if not board["top"]:
        text = "🎮 ماكو أحد لعب Subnet Rush هالأسبوع بعد. كون أول واحد!"
    else:
        medals = ["🥇", "🥈", "🥉"]
        lines = ["🏆 أبطال Subnet Rush هالأسبوع", ""]
        for r in board["top"]:
            lines.append(f"{medals[r['rank'] - 1] if r['rank'] <= 3 else str(r['rank']) + '.'} {r['name']} - {r['score']} ({r['title']})")
        text = "\n".join(lines)
    bot.send_message(message.chat.id, text, reply_markup=cp.markup(
        cp.app_button("🎮 العب", "fun", message.chat.type == "private")))


@bot.message_handler(commands=["poll"])
def poll(message):
    if not is_admin(message):
        return bot.reply_to(message,"❌ ما عندك صلاحية")
    parts = [p.strip() for p in _args(message).split("|")]
    try:
        p = fun.create_poll(parts[0] if parts else "", parts[1:], 24, by=message.from_user.first_name)
    except ValueError as e:
        return bot.reply_to(message, f"❌ {e}\n\nالصيغة:\n/poll نطلب تأجيل الامتحان؟ | اي | لا\n(يبقى مفتوح 24 ساعة، ومن التطبيق تكدر تختار المدة)")
    bot.reply_to(message, f"✅ انفتح التصويت: {p['question']}")


@bot.message_handler(commands=["backup"])
def backup(message):
    # upstash free has no backups, so admins can keep a copy of the data they typed in
    if not is_admin(message):
        return bot.reply_to(message,"❌ ما عندك صلاحية")
    if message.chat.type != "private":
        return bot.reply_to(message, "❌ دز هذا الأمر بالخاص")
    data = {"made": cp.now().isoformat(), "schedule": cp.get_schedule(), "overrides": cp.get_overrides(),
            "exams": cp.get_exams(), "announcements": cp.active_announcements(),
            "manual_tasks": ts.manual_tasks(redis), "polls": fun._load_polls()}
    raw = json.dumps(data, ensure_ascii=False, indent=1).encode()
    bot.send_document(message.chat.id, (f"uotce-backup-{cp.now().date()}.json", io.BytesIO(raw)),
                      caption="💾 نسخة احتياطية. إذا انمسح شي، دزلي الملف وأرجعه")


@bot.message_handler(commands=["myid"])
def myid(message):
    u = message.from_user
    role = "👑 مالك" if cp.is_owner(u.id) else "🔧 أدمن" if cp.is_admin_id(u.id) else "👤 طالب"
    bot.reply_to(message, f"🆔 رقمك: `{u.id}`\nصلاحيتك: {role}", parse_mode="Markdown")


def _target(message):
    # /add_admin 123456  or reply to someone's message with /add_admin
    r = getattr(message, "reply_to_message", None)
    if r is not None and r.from_user is not None and not r.from_user.is_bot:
        return r.from_user.id, r.from_user.first_name or ""
    arg = _args(message)
    if arg.lstrip("-").isdigit():
        return int(arg), ""
    return None, ""


@bot.message_handler(commands=["add_admin"])
def add_admin_cmd(message):
    if not cp.is_owner(message.from_user.id):
        return bot.reply_to(message, "❌ بس المالك يكدر يضيف أدمنية")
    uid, name = _target(message)
    if not uid:
        return bot.reply_to(message, "الصيغة:\n/add_admin رقم_الشخص\nأو سوّي رد على رسالته بالكروب واكتب /add_admin\n\nالشخص يعرف رقمه بأمر /myid")
    cp.add_admin(uid, name)
    try:
        bot.set_my_commands([types.BotCommand(c, d) for c, d in STUDENT_COMMANDS + ADMIN_COMMANDS],
                            scope=types.BotCommandScopeChat(uid))
    except Exception:
        pass  # he didnt start the bot yet, menu comes with /setup_app later
    bot.reply_to(message, f"✅ {name or uid} صار أدمن. يكدر يعدل الجدول والإعلانات والامتحانات من التطبيق")


@bot.message_handler(commands=["remove_admin"])
def remove_admin_cmd(message):
    if not cp.is_owner(message.from_user.id):
        return bot.reply_to(message, "❌ بس المالك يكدر يشيل أدمنية")
    uid, name = _target(message)
    if not uid:
        return bot.reply_to(message, "الصيغة: /remove_admin رقم_الشخص (أو رد على رسالته)")
    if cp.is_owner(uid):
        return bot.reply_to(message, "❌ هذا مالك، يتشال بس من ADMIN_IDS بـ Vercel")
    cp.remove_admin(uid)
    try:
        bot.delete_my_commands(scope=types.BotCommandScopeChat(uid))
    except Exception:
        pass
    bot.reply_to(message, f"✅ {name or uid} ما بقى أدمن")


@bot.message_handler(commands=["admins"])
def admins_cmd(message):
    if not is_admin(message):
        return bot.reply_to(message,"❌ ما عندك صلاحية")
    lines = ["👥 الأدمنية", ""]
    for a in cp.list_admins():
        lines.append(f"{'👑' if a['owner'] else '🔧'} {a['name'] or 'بدون اسم'} - {a['id']}")
    bot.reply_to(message, "\n".join(lines))
