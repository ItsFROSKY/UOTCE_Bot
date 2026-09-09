from telebot import TeleBot, types
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup
from googleapiclient.discovery import build 
from googleapiclient.http import MediaIoBaseDownload
import io
from bot_texts import*

def download_and_send_file(file_id, drive_service, bot, chat_id):
    #fetch file name to Telegram so that it knows what to name the file
    file_metadata = drive_service.files().get(fileId=file_id, fields="name").execute()
    file_name = file_metadata.get("name", "downloaded_file")

    #download binary of file
    request = drive_service.files().get_media(fileId=file_id)
    
    # stream the bytes into an in-memory buffer
    file_stream = io.BytesIO()
    downloader = MediaIoBaseDownload(file_stream, request)
    
    done = False
    #the loop to downlaod all chunks
    while not done:
        _, done = downloader.next_chunk()

    file_stream.seek(0)

    #send the file :)
    bot.send_document(chat_id, (file_name, file_stream))


def Google_menu(Folder_ID, bot, drive_service):
    
    quary = f"'{Folder_ID}' in parents and trashed = false"
    response = drive_service.files().list(q = quary, fields ="files(id, name, mimeType, shortcutDetails, size)").execute()

    #this is the list of file dictionaries
    items = response.get('files', [])
    FOLDER_MIME_TYPE = "application/vnd.google-apps.folder" #this way yk if its a folder or file
    SHORTCUT_MIME_TYPE = "application/vnd.google-apps.shortcut"
    inline_keyboard_menu = InlineKeyboardMarkup(row_width = 5)
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
