import base64
import json
import logging
import os

from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from telebot import TeleBot, apihelper
from upstash_redis import Redis

load_dotenv(override=True)

#load .env only  for local development without overriding platform secrets
load_dotenv()


# Purpose: Fail fast when a required secret or deployment setting is missing.
def required_env(name: str) -> str:
    """Return a required environment variable or fail with a safe message."""
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value

# Decode service-account credentials from an environment variable, never from Git.
try:
    credentials_json = json.loads(
        base64.b64decode(required_env("Google_Credentials_bot")).decode("utf-8")
    )
except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
    raise RuntimeError("Google_Credentials_bot is not valid Base64 JSON") from exc

creds = Credentials.from_service_account_info(
    credentials_json,
    scopes=["https://www.googleapis.com/auth/drive"],
)
Drive_service = build("drive", "v3", credentials=creds, cache_discovery=False)

redis = Redis(
    url=required_env("KV_REST_API_URL"),
    token=required_env("KV_REST_API_TOKEN"),
)

# Configure Telegram network timeouts centrally instead of scattering magic values.
apihelper.CONNECT_TIMEOUT = int(os.getenv("TELEGRAM_CONNECT_TIMEOUT", "30"))
apihelper.READ_TIMEOUT = int(os.getenv("TELEGRAM_READ_TIMEOUT", "120"))

bot = TeleBot(TOKEN, threaded=False)

REDIS_KEY_DRIVE = f"state:{Drive_ID}"
REDIS_UPDATE_PREFIX = "telegram:update:"
MAX_FILE_SIZE = int(os.getenv("MAX_FILE_SIZE_BYTES", str(50 * 1024 * 1024)))
JOB_LOCK_TTL = int(os.getenv("JOB_LOCK_TTL_SECONDS", "900"))
