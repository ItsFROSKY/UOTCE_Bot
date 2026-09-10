import os
import logging
from fastapi import FastAPI, Request
from telebot import TeleBot, types
from dotenv import load_dotenv
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload
import io
import urllib.parse
import requests
import json
import base64
from google.oauth2.service_account import Credentials
from PIL import Image
from bot_texts import*
from functions_bot import*


load_dotenv(override=True)


Google_Credentials_bot = json.loads(
    base64.b64decode(os.environ["Google_Credentials_bot"]).decode()
)
Drive_ID = os.environ.get("Drive_ID")
creds = Credentials.from_service_account_info(Google_Credentials_bot, scopes=["https://www.googleapis.com/auth/drive"])
Drive_service = build("drive", "v3", credentials=creds)
tasks_cache_File_ID = os.environ["tasks_cache_File_ID"]

#logging to see errors in vercel logs
logging.basicConfig(level=logging.INFO)

TOKEN = os.environ.get("BOT_TOKEN") or "" #the or exists to avoid none returns that will crash runtime for bot
if not TOKEN:
    logging.error("BOT_TOKEN environment variable is missing")

bot = TeleBot(TOKEN, threaded=False)
app = FastAPI()

@app.get("/")
def home():
    return {"status": "online", "message": "bot is running on vercel"}

@app.post("/webhook")
async def webhook_handler(request: Request):
    try:
        json_data = await request.json()
        update = types.Update.de_json(json_data)

        #Check if update parsed successfully, safety measures
        if update is not None:
            bot.process_new_updates([update])
            
        return {"status": "ok"}
    except Exception as e:
        logging.error(f"Error handling update: {e}")
        return {"status": "error", "message": str(e)}

#bot setup

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    bot.reply_to(message, "I am live")

@bot.callback_query_handler(func=lambda call: True)
def handle_drive_click(call):
    action, target_id = call.data.split(":") #split the prefix and the target id, use each one on its own
    #Stop the loading spinner on the user button OTHERWISE it will lag
    bot.answer_callback_query(call.id)
    if action == "dir":
        new_menu = Google_menu(target_id, bot, Drive_service)
        bot.edit_message_reply_markup(chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=new_menu)

    elif action == "file":
        bot.send_message(call.message.chat.id, "....📥جار التحميل")
        download_and_send_file(target_id, Drive_service, bot, call.message.chat.id)


@bot.message_handler(commands=['drive'])
def handle_drive(message):
    bot.send_message(message.chat.id, "loading")
    menu = Google_menu(Drive_ID, bot, Drive_service)
    bot.send_message(message.chat.id, "اختر الملف", reply_markup=menu)

@bot.message_handler(commands=['source'])
def source_text_end(message):
    bot.send_message(message.chat.id, "الكود OpenSource تكدر تشارك ببناءه")
    bot.send_message(message.chat.id, "github.com/ItsFROSKY/UOTCE_Bot/")


TASKS_CACHE = "tasks_cache.png"
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
TARGET_URL = "https://pouncing-donut-8de.notion.site/9fc6e320cbbd82cca21b81bcd086ac05?v=3366e320cbbd8082b0db000c05774ca&source=copy_link"

@bot.message_handler(commands=["tasks"])
def tasks(message):
    try:
        request = Drive_service.files().get_media(fileId=tasks_cache_File_ID)
        output = io.BytesIO()
        downloader = MediaIoBaseDownload(output, request)

        done = False
        while not done:
            _, done = downloader.next_chunk()

        output.seek(0)
        img = Image.open(output)
        left, top, right, bottom = 0, 170, 2960, 2000
        w, h = img.size
        img = img.crop((left, top, w - right, h - bottom))

        output = io.BytesIO()
        img.save(output, format="PNG")
        output.seek(0)
        output.name = "tasks.png"

        bot.send_photo(message.chat.id, output)
    except Exception as e:
        logging.exception("tasks failed")
        bot.reply_to(message, f"❌ {type(e).__name__}: {e}")


@bot.message_handler(commands=["tasks_update"])
def tasks_update(message):
    if message.from_user.id != ADMIN_ID:
        return bot.reply_to(message, "❌ Not authorized")

    try:
        bot.send_message(message.chat.id, "🔄 Updating...")

        params = (
            f"url={urllib.parse.quote_plus(TARGET_URL)}"
            "&screenshot=true&meta=false"
            "&viewport.width=2000&viewport.height=1600"
            "&viewport.deviceScaleFactor=2"
            "&screenshot.type=png&colorScheme=dark&waitFor=15000"
        )

        r = requests.get(f"https://api.microlink.io/?{params}", timeout=40)
        r.raise_for_status()

        url = r.json()["data"]["screenshot"]["url"]
        r = requests.get(url, timeout=40)
        r.raise_for_status()

        media = MediaIoBaseUpload(io.BytesIO(r.content), mimetype="image/png",resumable=False)
        Drive_service.files().update(fileId=tasks_cache_File_ID,media_body=media).execute()
        bot.send_photo(message.chat.id,io.BytesIO(r.content),caption="✅ Updated")



    except Exception as e:
        logging.exception("tasks_update failed")
        bot.reply_to(message, f"❌ {type(e).__name__}: {e}")


@bot.message_handler(commands=["id"])
def get_id(message):
    bot.reply_to(message, str(message.from_user.id))