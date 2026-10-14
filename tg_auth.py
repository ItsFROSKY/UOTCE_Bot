# checks the initData telegram sends with every mini app request
# docs: https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl


def validate_init_data(init_data: str, bot_token: str, max_age: int = 86400):
    # returns the user if the data is real and not older than max_age, otherwise None
    if not init_data or not bot_token:
        return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
    except ValueError:
        return None

    received_hash = pairs.pop("hash", None)
    if not received_hash:
        return None

    # every field except hash (signature stays in!), sorted, joined with \n
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received_hash):
        return None

    try:
        auth_date = int(pairs.get("auth_date", "0"))
    except ValueError:
        return None
    if max_age and time.time() - auth_date > max_age:
        return None  # old initData = rejected

    try:
        user = json.loads(pairs.get("user", "null"))
    except json.JSONDecodeError:
        return None
    if not isinstance(user, dict) or not isinstance(user.get("id"), int):
        return None
    return user


def sign_init_data(fields: dict, bot_token: str) -> str:
    # makes a valid initData, used in tests and local dev
    from urllib.parse import urlencode

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    h = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode({**fields, "hash": h})
