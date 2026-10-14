from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup
from googleapiclient.http import MediaIoBaseDownload
import json
import io
import time
from telebot.apihelper import ApiTelegramException
from config import*
from bot_texts import*



from telebot.apihelper import ApiTelegramException

def safe_api_call(func, *args, **kwargs):
    max_retries = 5
    attempts = 0
    
    while attempts < max_retries:
        try:
            return func(*args, **kwargs)
        except ApiTelegramException as e:
            if e.error_code == 429:
                #telegram tells exactly how many seconds to wait
                retry_after = int(e.result_json.get("parameters", {}).get("retry_after", 10))
                logging.warning(f"Rate limited (429). Sleeping for {retry_after + 2} seconds...")
                time.sleep(retry_after + 2)
                attempts += 1
            else:
                raise e
                
    raise Exception("Exceeded max retries for Telegram API call due to persistent 429 limits.")


TG_FILE_KEY = "tgfile:{id}:{modified}"


def send_drive_file(chat_id, file_id, message_thread_id=None, caption=None):
    # first time: download from drive + upload to telegram (slow)
    # then we save telegram's file_id in redis so next time its instant
    # returns 'sent' or 'link' (google docs / files over 50MB get a link)
    meta = Drive_service.files().get(
        fileId=file_id, fields="name, size, mimeType, modifiedTime, webViewLink",
        supportsAllDrives=True).execute()
    file_name = meta.get("name", "downloaded_file")
    size = int(meta.get("size") or 0)
    extra = {"message_thread_id": message_thread_id} if message_thread_id else {}

    # google docs have no file to download and telegram refuses over 50MB
    if meta.get("mimeType", "").startswith("application/vnd.google-apps.") or size > LIMIT_50MB:
        link = meta.get("webViewLink") or f"https://drive.google.com/file/d/{file_id}/view"
        safe_api_call(bot.send_message, chat_id, f"📎 {file_name}\nالملف كبير على التليكرام، افتحه من هنا:\n{link}", **extra)
        return "link"

    cache_key = TG_FILE_KEY.format(id=file_id, modified=meta.get("modifiedTime", ""))
    cached = None
    try:
        cached = redis.get(cache_key)
    except Exception:
        logging.exception("file cache read failed")
    if cached:
        try:
            safe_api_call(bot.send_document, chat_id, cached, caption=caption, **extra)
            return "sent"
        except ApiTelegramException:
            logging.warning("cached telegram file_id rejected, re-uploading")

    #download binary of file into memory
    request = Drive_service.files().get_media(fileId=file_id, supportsAllDrives=True)
    file_stream = io.BytesIO()
    downloader = MediaIoBaseDownload(file_stream, request)
    done = False
    while not done:
        status, done = downloader.next_chunk()
    file_stream.seek(0)

    sent = safe_api_call(bot.send_document, chat_id, (file_name, file_stream),
                         caption=caption, timeout=300, **extra)
    try:
        if sent and sent.document:
            redis.set(cache_key, sent.document.file_id, ex=180 * 86400)
    except Exception:
        logging.exception("file cache write failed")
    return "sent"


def download_and_send_file(file_id, chat_id_given, message, message_thread_id=None):
    try:
        if message_thread_id is None:
            send_drive_file(chat_id_given, file_id)
        else:
            send_drive_file(chat_id_given, file_id, message_thread_id=message_thread_id,
                            caption=None)
            time.sleep(2.0)
    except Exception as e:
        logging.exception(f"Failed to send file {file_id}: {e}")


def Google_menu(Folder_ID, bot, drive_service):
    
    quary = f"'{Folder_ID}' in parents and trashed = false"
    response = drive_service.files().list(q = quary, fields ="files(id, name, mimeType, shortcutDetails, size)").execute()

    #this is the list of file dictionaries
    items = response.get('files', [])
    FOLDER_MIME_TYPE = "application/vnd.google-apps.folder" #this way yk if its a folder or file
    SHORTCUT_MIME_TYPE = "application/vnd.google-apps.shortcut"
    inline_keyboard_menu = InlineKeyboardMarkup(row_width = 8)
    for item in items:
        file_name = item['name']
        file_id = item['id']
        mime_type = item['mimeType']
        is_folder = (mime_type==FOLDER_MIME_TYPE)
        
        if mime_type == SHORTCUT_MIME_TYPE:
            shortcut_details = item.get('shortcutDetails', {})
            file_id = shortcut_details.get('targetId', file_id) # Use the real target ID
            if shortcut_details.get('targetMimeType') == FOLDER_MIME_TYPE:
                is_folder = True



        if  is_folder:
            button = InlineKeyboardButton(text=f"📁{file_name}", callback_data=f"dir:{file_id}")
            inline_keyboard_menu.add(button)
        else:
            button = InlineKeyboardButton(text=f"📄{file_name}", callback_data=f"file:{file_id}")
            inline_keyboard_menu.add(button)
        
    return inline_keyboard_menu



def load_state():
    raw_data = redis.get(REDIS_KEY_DRIVE)
    if raw_data:
        return json.loads(raw_data)
    return {"page_token": None, "topic_map": {}}

def save_state(state):
    redis.set(REDIS_KEY_DRIVE, json.dumps(state))

LIMIT_50MB = 52428800

def process_update(json_data):
    try:
        update = types.Update.de_json(json_data)
        if update is not None:
            bot.process_new_updates([update])
    except Exception as e:
        logging.error(f"Error handling update in background: {e}")

import time
from ssl import SSLError
from googleapiclient.errors import HttpError

def get_drive_subfolders(q_for_Drive):
    for attempt in range(3):
        try:
            return Drive_service.files().list(
                q=q_for_Drive, 
                fields="files(id, name)"
            ).execute()
        except (SSLError, OSError) as e:
            if attempt == 2:
                raise e
            time.sleep(1)
    return {}
            
def send_to_telegram(folder_ID, topic_id, message, state):
    state.setdefault("sent_files", [])
    
    quary = f"'{folder_ID}' in parents and trashed = false"
    response = Drive_service.files().list(q = quary, fields ="files(id, name, mimeType, size)").execute()

    #this is the list of file dictionaries
    items = response.get('files', [])
    FOLDER_MIME_TYPE = "application/vnd.google-apps.folder" #this way yk if its a folder or file
    for item in items:
        file_name = item['name']
        file_id = item['id']
        mime_type = item['mimeType']
        is_folder = (mime_type==FOLDER_MIME_TYPE)

        if file_id in state["sent_files"]:
            continue

        if  is_folder:
            send_to_telegram(file_id, topic_id, message, state) #the power of recursion lol
            continue

        if mime_type.startswith("application/vnd.google-apps."):
            continue
        else:
            download_and_send_file(file_id, telegram_ID_course, message, message_thread_id = topic_id)
            state["sent_files"].append(file_id)
            save_state(state)
            time.sleep(2.0)

def create_topic(state, message):
    safe_api_call(bot.send_message, message.chat.id, "بدأ")
    Number_sneds = 0
    q_for_Drive = f"'{Drive_ID_course}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    List_subfolders = get_drive_subfolders(q_for_Drive)
    
    for subfolder in List_subfolders["files"]:
        subfolder_name = subfolder["name"]
        subfolder_id = subfolder["id"]
        if subfolder_id not in state["topic_map"]:
            try:
                safe_api_call(bot.send_message, message.chat.id, f"new subfolder found, creating {subfolder_name} topic...")
                new_topic = safe_api_call(bot.create_forum_topic, chat_id=telegram_ID_course, name=subfolder_name)
                time.sleep(1.2)
                state["topic_map"][subfolder_id] = new_topic.message_thread_id #satore the ID in topic_map
                
                
                save_state(state) #get topic ID for dispatching files

            except ApiTelegramException as e:
                            if e.error_code == 400 and "not enough rights" in e.description:
                                safe_api_call(bot.send_message, message.chat.id, "❌Bot needs Admin permissions")
                                return  # Stop execution cleanly
                            raise e
            
        topic_id = state["topic_map"][subfolder_id]
        send_to_telegram(subfolder_id,topic_id, message, state)
        Number_sneds += 1
    safe_api_call(bot.send_message, message.chat.id, f"تم إرسال {Number_sneds}⏬...")