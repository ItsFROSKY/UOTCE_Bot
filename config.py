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


_google_raw = os.environ["Google_Credentials_bot"].strip()
# works with the json pasted as is, or base64 of it like before
Google_Credentials_bot = json.loads(_google_raw if _google_raw.startswith("{") else base64.b64decode(_google_raw).decode())
Drive_ID = os.environ.get("Drive_ID")
creds = Credentials.from_service_account_info(Google_Credentials_bot, scopes=["https://www.googleapis.com/auth/drive"])
Drive_service = build("drive", "v3", credentials=creds)
tasks_cache_File_ID = os.environ["tasks_cache_File_ID"]
TOKEN = os.environ.get("BOT_TOKEN") or "" #the or exists to avoid none returns that will crash runtime for bot
if not TOKEN:
    logging.error("BOT_TOKEN environment variable is missing")
    
bot = TeleBot(TOKEN, threaded=False)



redis = Redis(url=os.environ["KV_REST_API_URL"], token=os.environ["KV_REST_API_TOKEN"])
Drive_ID_course = "1rerWXAD3BpbnMjiaXEUPvD8eRlK46QFS"
telegram_ID_course = -1003344139710
REDIS_KEY_DRIVE = f"state:{Drive_ID_course}"

# Set long connection and read timeouts (e.g., 60s connect, 300s read for large files)
apihelper.CONNECT_TIMEOUT = 60
apihelper.READ_TIMEOUT = 300

ADMIN_ID = {int(x) for x in os.environ.get("ADMIN_IDS", "1966119616,1259549184").split(",") if x.strip()}

# same random string you give setWebhook(secret_token=...), telegram sends it back with every update
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")

# for /api/cron/*
CRON_SECRET = os.environ.get("CRON_SECRET", "")

# mini app url
_vercel_host = os.environ.get("VERCEL_PROJECT_PRODUCTION_URL")
WEBAPP_URL = os.environ.get("WEBAPP_URL") or (f"https://{_vercel_host}/app/index.html" if _vercel_host else "")
