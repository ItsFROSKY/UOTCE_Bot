from upstash_redis import Redis
from dotenv import load_dotenv
import json
import logging
import os
import base64
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from telebot import TeleBot, types, apihelper
from fastapi import FastAPI, Request, BackgroundTasks



load_dotenv(override=True)


Google_Credentials_bot = json.loads(base64.b64decode(os.environ["Google_Credentials_bot"]).decode())
Drive_ID = os.environ.get("Drive_ID")
creds = Credentials.from_service_account_info(Google_Credentials_bot, scopes=["https://www.googleapis.com/auth/drive"])
Drive_service = build("drive", "v3", credentials=creds)
tasks_cache_File_ID = os.environ["tasks_cache_File_ID"]
TOKEN = os.environ.get("BOT_TOKEN") or "" #the or exists to avoid none returns that will crash runtime for bot
if not TOKEN:
    logging.error("BOT_TOKEN environment variable is missing")
    
bot = TeleBot(TOKEN, threaded=False)



redis = Redis(url=os.environ["KV_REST_API_URL"], token=os.environ["KV_REST_API_TOKEN"])
Drive_ID_year_key = f"state:{os.environ['Drive_ID_year']}"

Drive_ID_course = "1ssGM9XczIJHLuexrjsrcq1IL8T2ygt2a"
telegram_ID_course = -1004386145545

# Set long connection and read timeouts (e.g., 60s connect, 300s read for large files)
apihelper.CONNECT_TIMEOUT = 60
apihelper.READ_TIMEOUT = 300
