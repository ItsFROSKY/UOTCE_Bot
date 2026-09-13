from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup
from googleapiclient.http import MediaIoBaseDownload
import json
import io
import time
import logging
from googleapiclient.errors import HttpError
from ssl import SSLError
from telebot.apihelper import ApiTelegramException
from config import *
from telebot import types

# from bot_texts import *
# No shared text constants are currently defined in bot_texts.py.

logger = logging.getLogger("uotce_bot")


class TelegramRetryExhausted(RuntimeError):
    """Raised when Telegram remains rate-limited after bounded retries."""


# Purpose: Retry Telegram rate limits with a bounded delay without repeating permanent errors.
def safe_api_call(func, *args, **kwargs):
    # Retry only rate limits; retrying permanent Telegram errors creates duplicate actions.
    for attempt in range(5):
        try:
            return func(*args, **kwargs)
        except ApiTelegramException as exc:
            if exc.error_code != 429:
                raise
            retry_after = int(
                exc.result_json.get("parameters", {}).get("retry_after", 10)
            )
            delay = min(retry_after + 2, 60)
            logger.warning(
                "Telegram rate limit; retry=%s delay=%ss", attempt + 1, delay
            )
            time.sleep(delay)
    raise TelegramRetryExhausted("Telegram rate limit retry budget exhausted")


# Purpose: Enforce a size limit before downloading a Drive file into server memory.
def download_and_send_file(file_id, chat_id_given, message, message_thread_id=None):
    # Check the remote size before allocating memory for a potentially huge download.
    file_metadata = (
        Drive_service.files()
        .get(
            fileId=file_id,
            fields="name,size,mimeType",
            supportsAllDrives=True,
        )
        .execute()
    )
    file_name = file_metadata.get("name", "downloaded_file")
    file_size = int(file_metadata.get("size") or 0)
    if file_size > MAX_FILE_SIZE:
        raise ValueError(f"File exceeds configured limit: {MAX_FILE_SIZE} bytes")

    request = Drive_service.files().get_media(fileId=file_id, supportsAllDrives=True)
    file_stream = io.BytesIO()
    downloader = MediaIoBaseDownload(file_stream, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
        if file_stream.tell() > MAX_FILE_SIZE:
            raise ValueError("Downloaded file exceeded configured limit")
    file_stream.seek(0)

    try:
        if message_thread_id is None:
            safe_api_call(
                bot.send_document, chat_id_given, (file_name, file_stream), timeout=120
            )
        else:
            safe_api_call(
                bot.send_document,
                chat_id=chat_id_given,
                document=(file_name, file_stream),
                message_thread_id=message_thread_id,
                caption=f"📁 {file_name}",
                timeout=120,
            )
    except Exception:
        logger.exception("Failed to send file file_id=%s", file_id)
        raise


# Purpose: Build a deterministic, compact Telegram menu for a Drive folder.
def Google_menu(folder_id, drive_service):
    # Use explicit fields and ordering to make the menu deterministic and cheaper.
    query = f"'{folder_id}' in parents and trashed = false"
    response = (
        drive_service.files()
        .list(
            q=query,
            pageSize=100,
            orderBy="folder,name",
            fields="files(id,name,mimeType,shortcutDetails,size),nextPageToken",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        )
        .execute()
    )

    inline_keyboard_menu = InlineKeyboardMarkup(row_width=2)
    folder_mime = "application/vnd.google-apps.folder"
    shortcut_mime = "application/vnd.google-apps.shortcut"
    for item in response.get("files", []):
        file_name = item.get("name", "Unnamed")[:55]
        file_id = item["id"]
        mime_type = item.get("mimeType", "")
        is_folder = mime_type == folder_mime
        if mime_type == shortcut_mime:
            shortcut = item.get("shortcutDetails", {})
            file_id = shortcut.get("targetId", file_id)
            is_folder = shortcut.get("targetMimeType") == folder_mime
        prefix = "📁" if is_folder else "📄"
        action = "dir" if is_folder else "file"
        inline_keyboard_menu.add(
            InlineKeyboardButton(
                text=f"{prefix} {file_name}", callback_data=f"{action}:{file_id}"
            )
        )
    return inline_keyboard_menu


# Purpose: Load synchronization state safely even when Redis contains invalid JSON.
def load_state():
    # Recover from malformed Redis data instead of crashing every synchronization attempt.
    raw_data = redis.get(REDIS_KEY_DRIVE)
    if not raw_data:
        return {"version": 1, "topic_map": {}, "sent_files": []}
    try:
        state = json.loads(raw_data)
    except (TypeError, ValueError, json.JSONDecodeError):
        logger.exception("Invalid synchronization state in Redis")
        return {"version": 1, "topic_map": {}, "sent_files": []}
    state.setdefault("version", 1)
    state.setdefault("topic_map", {})
    state.setdefault("sent_files", [])
    return state


# Purpose: Persist the current synchronization state in a compact Redis record.
def save_state(state):
    # Store a compact JSON state; a worker lock should protect concurrent updates.
    redis.set(REDIS_KEY_DRIVE, json.dumps(state, separators=(",", ":")))


# Purpose: Convert a raw Telegram payload into an update and dispatch it to handlers.
def process_update(json_data):
    try:
        update = types.Update.de_json(json_data)
        if update is not None:
            bot.process_new_updates([update])
    except Exception:
        logger.exception("Error handling update")


# Purpose: Read Drive folders with limited retries for transient network failures.
def get_drive_subfolders(query):
    for attempt in range(3):
        try:
            return (
                Drive_service.files()
                .list(
                    q=query,
                    pageSize=100,
                    orderBy="name",
                    fields="files(id,name),nextPageToken",
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )
                .execute()
            )
        except (SSLError, OSError, HttpError):
            if attempt == 2:
                raise
            time.sleep(2**attempt)
    return {"files": []}


# Purpose: Send eligible Drive files to one Telegram topic while tracking sent IDs.
def send_to_telegram(folder_id, topic_id, message, state):
    state.setdefault("sent_files", [])
    query = f"'{folder_id}' in parents and trashed = false"
    response = (
        Drive_service.files()
        .list(
            q=query,
            pageSize=100,
            orderBy="folder,name",
            fields="files(id,name,mimeType,size),nextPageToken",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        )
        .execute()
    )

    for item in response.get("files", []):
        file_id = item["id"]
        mime_type = item.get("mimeType", "")
        if file_id in state["sent_files"]:
            continue
        if mime_type == "application/vnd.google-apps.folder":
            send_to_telegram(file_id, topic_id, message, state)
            continue
        if mime_type.startswith("application/vnd.google-apps."):
            continue
        download_and_send_file(
            file_id, telegram_ID_course, message, message_thread_id=topic_id
        )
        state["sent_files"].append(file_id)
        save_state(state)
        time.sleep(2.0)


# Purpose: Create missing Telegram topics and synchronize their Drive folder contents.
def create_topic(state, message):
    safe_api_call(bot.send_message, message.chat.id, "🔄 creating new topics...")
    query = f"'{Drive_ID_course}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    folders = get_drive_subfolders(query)

    for subfolder in folders.get("files", []):
        subfolder_name = subfolder["name"]
        subfolder_id = subfolder["id"]
        if subfolder_id not in state["topic_map"]:
            try:
                safe_api_call(
                    bot.send_message,
                    message.chat.id,
                    f"Creating topic: {subfolder_name}",
                )
                new_topic = safe_api_call(
                    bot.create_forum_topic,
                    chat_id=telegram_ID_course,
                    name=subfolder_name[:128],
                )
                state["topic_map"][subfolder_id] = new_topic.message_thread_id
                save_state(state)
            except ApiTelegramException as exc:
                if (
                    exc.error_code == 400
                    and "not enough rights" in exc.description.lower()
                ):
                    safe_api_call(
                        bot.send_message,
                        message.chat.id,
                        "❌ Bot needs admin permissions",
                    )
                    return
                raise

        topic_id = state["topic_map"][subfolder_id]
        safe_api_call(
            bot.send_message, message.chat.id, f"جار إرسال ملفات {subfolder_name} ⏬"
        )
        send_to_telegram(subfolder_id, topic_id, message, state)

    safe_api_call(bot.send_message, message.chat.id, "finished...")


@bot.message_handler(commands=["reset_topics"])
# Purpose: Let only the administrator clear topic mappings without deleting Telegram topics.
def handle_reset(message):
    # Restrict state-destructive commands to the configured administrator.
    if message.from_user.id != ADMIN_ID:
        return bot.reply_to(message, "❌ هذا الأمر متاح للمدير فقط")
    state = load_state()
    state["topic_map"] = {}
    save_state(state)
    bot.reply_to(message, "✅ Topic map cleared; Telegram topics were not deleted.")
