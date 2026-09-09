import os
import logging
from fastapi import FastAPI, Request
from telebot import TeleBot, types
from dotenv import load_dotenv
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
import io
import urllib.parse
import requests
from PIL import Image
from bot_texts import*
from functions_bot import*


load_dotenv()

#initialize the Drive
Drive_api = os.environ.get("Google_Drive_API")
Drive_ID = os.environ.get("Drive_ID")
Drive_service = build('drive', 'v3', developerKey=Drive_api)

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


@bot.message_handler(commands=['tasks'])
def notion_screenshot(message):
    bot.send_message(message.chat.id, 'جاري إرسال الصورة🖼️')
    bot.send_chat_action(message.chat.id, 'upload_photo')

    target_url = "https://pouncing-donut-8de.notion.site/9fc6e320cbbd82cca21b81bcd086ac05?v=3366e320cbbd8082b0db000c05774ca&source=copy_link"

    def send_image(img):
    
        logging.info(f"Image received from API: {img.size}")

        output = io.BytesIO()
        img.save(output, format="PNG")
        output.seek(0)
        output.name = "tasks.png"

        bot.send_photo(message.chat.id, output, caption="RAW IMAGE")
    try:
        logging.info("Trying Thum.io")

        thum_url = (
            "https://image.thum.io/get/"
            "crop/1200/"
            "maxAge/0/"
            "png/"
            "?url=" + urllib.parse.quote(target_url, safe="")
                )

        r = requests.get(thum_url, timeout=40)
        logging.info(f"Thum.io: {r.status_code} {r.headers.get('content-type')} {len(r.content)} bytes")
        r.raise_for_status()

        if not r.headers.get("content-type", "").startswith("image/"):
            raise Exception("Thum.io did not return an image")

        send_image(Image.open(io.BytesIO(r.content)))
        logging.info("Thum.io succeeded")
        return

    except Exception as e:
        logging.exception(f"Thum.io failed: {e}")
        logging.exception(f"Thum.io response: {r.text[:500] if 'r' in locals() else 'no response'}")

    try:
        logging.info("Trying Microlink")
        params = (
            f"url={urllib.parse.quote_plus(target_url)}"
            "&screenshot=true&meta=false"
            "&viewport.width=1600&viewport.height=1600"
            "&viewport.deviceScaleFactor=1"
            "&screenshot.type=jpeg&colorScheme=dark&waitFor=15000"
        )

        r = requests.get(f"https://api.microlink.io/?{params}", timeout=40)
        logging.info(f"Microlink: {r.status_code} {r.headers.get('content-type')} {len(r.content)} bytes")
        r.raise_for_status()

        screenshot_url = r.json()["data"]["screenshot"]["url"]
        r = requests.get(screenshot_url, timeout=40)
        logging.info(f"Microlink image: {r.status_code} {r.headers.get('content-type')} {len(r.content)} bytes")
        r.raise_for_status()

        send_image(Image.open(io.BytesIO(r.content)))
        logging.info("Microlink succeeded")

    except Exception as e:
        logging.exception(f"Microlink failed: {e}")
        logging.exception(f"Microlink response: {r.text[:500] if 'r' in locals() else 'no response'}")
        bot.reply_to(message, f"❌ error: {type(e).__name__}: {e}")