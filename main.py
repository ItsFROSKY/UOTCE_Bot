from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload
import io
import time
import urllib.parse
import requests
import json
import logging
from upstash_redis import Redis
from fastapi import FastAPI, Request, BackgroundTasks
from PIL import Image
from config import*
from bot_texts import*
from functions_bot import*







#logging to see errors in vercel logs
logging.basicConfig(level=logging.INFO)
app = FastAPI()

@app.get("/")
def home():
    return {"status": "online", "message": "bot is running on vercel"}

@app.post("/webhook")
async def webhook_handler(request: Request, background_tasks: BackgroundTasks):
    try:
        json_data = await request.json()
        update = types.Update.de_json(json_data)

        if update is not None:
            #offload processing so tele gets an instant 200 OK
            background_tasks.add_task(bot.process_new_updates, [update])
            
        return {"status": "ok"}
    except Exception as e:
        logging.error(f"Error handling update: {e}")
        return {"status": "error", "message": str(e)}

#bot setup

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    bot.send_message(message.chat.id, "i am alive")

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


ADMIN_ID=int(os.getenv("ADMIN_ID","0"))
TARGET_URL="https://pouncing-donut-8de.notion.site/9fc6e320cbbd82cca21b81bcd086ac05?v=3366e320cbbd8082b0db000c05774ca"
TASKS_FILE="tasks_file_id.txt"
@bot.message_handler(commands=["tasks"])
def tasks(message):
    try:
        if not os.path.exists(TASKS_FILE):
            return bot.reply_to(message,"أستخدم /Tasks_update أولاً❌ ")
        with open(TASKS_FILE,"r") as f:
            file_id=f.read().strip()
        if not file_id:
            return bot.reply_to(message,"أستخدم /Tasks_update أولاً❌ ")
        bot.send_photo(message.chat.id,file_id)
    except Exception as e:
        logging.exception("tasks failed")
        bot.reply_to(message,f"❌ {type(e).__name__}: {e}")




@bot.message_handler(commands=["tasks_update"])
def tasks_update(message):
    bot.send_message(message.chat.id,"🔄 Updating...")
    if message.from_user.id!=ADMIN_ID:
        return bot.reply_to(message,"❌ ما عندك صلاحية")
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
        with open(TASKS_FILE,"w") as f:
            f.write(sent.photo[-1].file_id)
    except Exception as e:
        logging.exception("tasks_update failed")
        bot.reply_to(message,f"❌ {type(e).__name__}: {e}")

@bot.message_handler(commands=["groupid"])
def get_group_id(message):
    if message.from_user.id!=ADMIN_ID:
        return bot.reply_to(message,"❌ ما عندك صلاحية")
    if message.chat.type not in ["group","supergroup"]:
        return bot.reply_to(message,"❌ استخدم الأمر داخل الكروب")
    bot.reply_to(message,f"🆔 Group ID:\n`{message.chat.id}`",parse_mode="Markdown")

@bot.message_handler(commands=["update_tele"])
def Dive_to_telegram(message):
    if message.from_user.id!=ADMIN_ID:
            return bot.reply_to(message,"❌ ما عندك صلاحية")
    state = load_state()
    create_topic(state, message)