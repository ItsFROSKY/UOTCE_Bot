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


@bot.message_handler(commands=['source'])
def source_text_end(message):
    bot.send_message(message.chat.id, "الكود OpenSource تكدر تشارك ببناءه")
    bot.send_message(message.chat.id, "github.com/ItsFROSKY/UOTCE_Bot/")

@bot.message_handler(commands=['tasks'])
def notion_screenshot(message):
    bot.send_message(message.chat.id, 'جاري  إرسال الصورة🖼️')
    bot.send_chat_action(message.chat.id, 'upload_photo')
    target_url = "https://pouncing-donut-8de.notion.site/9fc6e320cbbd82cca21b81bcd086ac05?v=3366e320cbbd8082b0db000c05774cae&source=copy_link"
    encoded_url = urllib.parse.quote_plus(target_url)
    
    params = (
        f"url={encoded_url}"
        "&screenshot=true"
        "&meta=false"
        "&embed=screenshot.url"
        "&viewport.width=1600"
        "&viewport.height=1600"
        "&viewport.deviceScaleFactor=1"
        "&screenshot.type=jpeg"
        "&colorScheme=dark"
        "&waitFor=9000"
    )
    
    screenshot_api_url = f"https://api.microlink.io/?{params}"

    try:
        img_response = requests.get(screenshot_api_url, timeout=30)
        
        if img_response.status_code == 200:
            img = Image.open(io.BytesIO(img_response.content))
            width, height = img.size
            left = 50
            top = 180
            crop_width = 800
            crop_height = 700

            #safety clamps
            right = min(left + crop_width, width)
            bottom = min(top + crop_height, height)

            crop_box = (left, top, right, bottom)
            cropped_img = img.crop(crop_box)

            output_bytes = io.BytesIO()
            cropped_img.save(output_bytes, format='JPEG', quality=85)
            output_bytes.seek(0)
            output_bytes.name = "tasks.jpg"
            
            bot.send_photo(message.chat.id, photo=output_bytes, caption="Uni Tasks📋", reply_to_message_id=message.message_id)
        else:
            bot.reply_to(message, "Failed to generate tasks snapshot.")
            
    except Exception as e:
        logging.error(f"error fetching task screenshot: {e}")
        bot.reply_to(message, "error loading snapshot")