import io
import json
import logging
import secrets

import requests
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from googleapiclient.http import MediaIoBaseUpload
from PIL import Image
from telebot import types

from bot_texts import *
from config import *
from functions_bot import *

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("uotce_bot")
app = FastAPI()
TASKS_FILE_KEY = f"tasks:file_id:{Drive_ID}"
TARGET_URL = "https://pouncing-donut-8de.notion.site/9fc6e320cbbd82cca21b81bcd086ac05?v=3366e320cbbd8082b0db000c05774ca"


@app.get("/")
def home():
    return {"status": "online", "message": "bot is running on vercel"}


@app.get("/health/live")
# Purpose: Provide a lightweight liveness check without calling external services.
def health_live():
    return {"status": "ok"}


@app.post("/webhook")
# Purpose: Authenticate Telegram updates, deduplicate them, and acknowledge quickly.
async def webhook_handler(request: Request, background_tasks: BackgroundTasks):
    # Verify Telegram's secret header before parsing or processing any update.
    received_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not secrets.compare_digest(received_secret, TELEGRAM_WEBHOOK_SECRET):
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        # Reject unexpectedly large requests before they consume application memory.
        content_length = int(request.headers.get("content-length", "0"))
        if content_length > 1_000_000:
            raise HTTPException(status_code=413, detail="Payload too large")

        json_data = await request.json()
        update = types.Update.de_json(json_data)
        if update is None:
            raise HTTPException(status_code=400, detail="Invalid update")

        # Telegram may retry an update; process each update_id only once per day.
        update_id = getattr(update, "update_id", None)
        if update_id is not None:
            accepted = redis.set(
                f"{REDIS_UPDATE_PREFIX}{update_id}",
                "1",
                ex=86400,
                nx=True,
            )
            if not accepted:
                return {"status": "duplicate"}

        # Keep the webhook response fast; long work still belongs in a durable worker.
        background_tasks.add_task(bot.process_new_updates, [update])
        return {"status": "ok"}
    except HTTPException:
        raise
    except (ValueError, TypeError, json.JSONDecodeError):
        logger.warning("Rejected malformed Telegram update")
        raise HTTPException(status_code=400, detail="Invalid JSON")
    except Exception:
        logger.exception("Webhook processing failed")
        raise HTTPException(status_code=503, detail="Temporary service error")


@bot.message_handler(commands=["start", "help"])
# Purpose: Give users a safe entry point and explain the first available command.
def send_welcome(message):
    bot.send_message(message.chat.id, "I am alive. Use /me to see your Telegram ID.")


@bot.message_handler(commands=["me"])
# Purpose: Show the requesting user non-sensitive Telegram identifiers for configuration.
def show_my_info(message):
    # Expose only non-sensitive Telegram profile identifiers to the requesting user.
    user = message.from_user
    name = " ".join(filter(None, [user.first_name, user.last_name])) or "Unknown"
    username = f"@{user.username}" if user.username else "No username"
    bot.reply_to(
        message,
        f"Name: {name}\nUsername: {username}\nUser ID: `{user.id}`\nChat ID: `{message.chat.id}`",
        parse_mode="Markdown",
    )


@bot.callback_query_handler(func=lambda call: True)
# Purpose: Validate Drive button input before reading or sending any file.
def handle_drive_click(call):
    # Parse callback data safely so malformed user-controlled data cannot crash the handler.
    action, separator, target_id = (call.data or "").partition(":")
    if not separator or action not in {"dir", "file"} or not target_id:
        bot.answer_callback_query(call.id, "Invalid action", show_alert=True)
        return

    try:
        bot.answer_callback_query(call.id)
        if action == "dir":
            new_menu = Google_menu(target_id, Drive_service)
            bot.edit_message_reply_markup(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=new_menu,
            )
        else:
            bot.send_message(call.message.chat.id, "جار التحميل 📥")
            download_and_send_file(target_id, call.message.chat.id, call)
    except Exception:
        logger.exception("Drive callback failed")
        bot.answer_callback_query(call.id, "Temporary error", show_alert=True)


@bot.message_handler(commands=["drive"])
# Purpose: Display the configured Google Drive folder through Telegram buttons.
def handle_drive(message):
    try:
        bot.send_message(message.chat.id, "Loading...")
        menu = Google_menu(Drive_ID, Drive_service)
        bot.send_message(message.chat.id, "اختر الملف", reply_markup=menu)
    except Exception:
        logger.exception("Drive menu failed")
        bot.reply_to(message, "حدث خطأ مؤقت أثناء قراءة الملفات.")


@bot.message_handler(commands=["source"])
# Purpose: Provide the public source-code link without exposing server configuration.
def source_text_end(message):
    bot.send_message(message.chat.id, "Source: https://github.com/ItsFROSKY/UOTCE_Bot/")


@bot.message_handler(commands=["tasks"])
# Purpose: Read the latest task image from durable Redis storage on serverless hosting.
def tasks(message):
    try:
        # Keep Telegram file_id in Redis because serverless local files are not durable.
        file_id = redis.get(TASKS_FILE_KEY)
        if not file_id:
            return bot.reply_to(message, "استخدم /tasks_update أولاً ❌")
        bot.send_photo(message.chat.id, file_id)
    except Exception:
        logger.exception("Tasks read failed")
        bot.reply_to(message, "حدث خطأ مؤقت أثناء عرض المهام.")


@bot.message_handler(commands=["tasks_update"])
# Purpose: Allow only the administrator to refresh the task screenshot once at a time.
def tasks_update(message):
    if message.from_user.id != ADMIN_ID:
        return bot.reply_to(message, "❌ ما عندك صلاحية")

    # Prevent two expensive screenshot jobs from running at the same time.
    lock_key = "lock:tasks_update"
    if not redis.set(lock_key, str(message.from_user.id), ex=JOB_LOCK_TTL, nx=True):
        return bot.reply_to(message, "⏳ يوجد تحديث قيد التنفيذ")

    bot.send_message(message.chat.id, "🔄 Updating...")
    try:
        token = required_env("BROWSERLESS_TOKEN")
        code = f"""
        export default async ({{page}})=>{{
        await page.setViewport({{width:2000,height:1600,deviceScaleFactor:2}});
        await page.emulateMediaFeatures([{{name:"prefers-color-scheme",value:"dark"}}]);
        await page.goto({json.dumps(TARGET_URL.split("?")[0])},{{waitUntil:"networkidle2",timeout:30000}});
        if(document.fonts) await document.fonts.ready;
        return await page.screenshot({{type:"png",fullPage:false}});
        }};"""
        response = requests.post(
            "https://production-sfo.browserless.io/function",
            params={"token": token},
            headers={"Content-Type": "application/javascript"},
            data=code,
            timeout=60,
        )
        response.raise_for_status()
        img = Image.open(io.BytesIO(response.content))
        left, top, right, bottom = 150, 150, 2842, 2150
        width, height = img.size
        if width <= left + right or height <= top + bottom:
            raise RuntimeError("Screenshot is smaller than the configured crop")
        img = img.crop((left, top, width - right, height - bottom))
        output = io.BytesIO()
        img.save(output, format="PNG")
        output.seek(0)
        media = MediaIoBaseUpload(output, mimetype="image/png", resumable=False)
        Drive_service.files().update(
            fileId=tasks_cache_File_ID, media_body=media
        ).execute()
        output.seek(0)
        sent = bot.send_photo(message.chat.id, output, caption="✅ Updated")
        if not sent or not sent.photo:
            raise RuntimeError("Telegram did not return a photo message")
        redis.set(TASKS_FILE_KEY, sent.photo[-1].file_id)
    except Exception:
        logger.exception("Tasks update failed")
        bot.reply_to(message, "حدث خطأ مؤقت أثناء تحديث المهام.")
    finally:
        # Release the lock even when Google, Browserless, or Telegram fails.
        redis.delete(lock_key)


@bot.message_handler(commands=["groupid"])
# Purpose: Return the current group ID only to the configured administrator.
def get_group_id(message):
    if message.from_user.id != ADMIN_ID:
        return bot.reply_to(message, "❌ ما عندك صلاحية")
    if message.chat.type not in ["group", "supergroup"]:
        return bot.reply_to(message, "❌ استخدم الأمر داخل الكروب")
    bot.reply_to(message, f"🆔 Group ID:\n`{message.chat.id}`", parse_mode="Markdown")


@bot.message_handler(commands=["update_tele"])
# Purpose: Start Drive-to-Telegram synchronization only for the administrator.
def update_telegram(message):
    if message.from_user.id != ADMIN_ID:
        return bot.reply_to(message, "❌ ما عندك صلاحية")
    state = load_state()
    create_topic(state, message)
