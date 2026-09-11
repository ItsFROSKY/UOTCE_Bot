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


def download_and_send_file(file_id, chat_id_given, message, message_thread_id=None):
    #fetch file name to Telegram so that it knows what to name the file
    file_metadata = Drive_service.files().get(fileId=file_id, fields="name").execute()
    file_name = file_metadata.get("name", "downloaded_file")

    #download binary of file
    request = Drive_service.files().get_media(fileId=file_id)
    
    # stream the bytes into an in-memory buffer
    file_stream = io.BytesIO()
    downloader = MediaIoBaseDownload(file_stream, request)
    
    done = False
    #the loop to downlaod all chunks
        
    while not done:
        status, done = downloader.next_chunk()

    file_stream.seek(0)

    #send the file :)
    try:
        if message_thread_id is None:
            bot.send_document(chat_id_given, (file_name, file_stream), timeout=300)
        else:
            safe_api_call(bot.send_document, chat_id=chat_id_given, document=(file_name, file_stream), 
            message_thread_id=message_thread_id, caption=f"📁 `{file_name}`", parse_mode="Markdown", timeout=300)
            time.sleep(2.0)
    except Exception as e:
        print(f"Failed to send file {file_name}: {e}")


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
    safe_api_call(bot.send_message, message.chat.id, "🔄creating new topics...")
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
        safe_api_call(bot.send_message, message.chat.id, f"جار إرسال جميع ملفات {subfolder_name}⏬...")
        send_to_telegram(subfolder_id,topic_id, message, state)
        
        
    safe_api_call(bot.send_message, message.chat.id, "finished...")

@bot.message_handler(commands=['reset_topics'])
def handle_reset(message):
    state = load_state()
    state["topic_map"] = {}
    save_state(state)
    bot.reply_to(message, "✅ Topic map cleared! Run /update_tele again.")