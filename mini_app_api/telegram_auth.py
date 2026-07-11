"""Validates Telegram Mini App initData (WebAppData) per Telegram's documented algorithm."""
import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl


class InvalidInitData(Exception):
    pass


def validate_init_data(init_data: str, bot_token: str, max_age_seconds: int = 86400) -> dict:
    if not init_data:
        raise InvalidInitData("missing initData")

    try:
        parsed = dict(parse_qsl(init_data, strict_parsing=True))
        received_hash = parsed.pop("hash", None)
        if not received_hash:
            raise InvalidInitData("missing hash field")

        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

        if not hmac.compare_digest(computed_hash, received_hash):
            raise InvalidInitData("hash mismatch")

        auth_date = int(parsed.get("auth_date", "0"))
        if time.time() - auth_date > max_age_seconds:
            raise InvalidInitData("initData expired")

        if "user" in parsed:
            parsed["user"] = json.loads(parsed["user"])
    except InvalidInitData:
        raise
    except (ValueError, TypeError) as e:
        # malformed query string / non-numeric auth_date / invalid JSON in
        # user field, etc. — treat as invalid auth, not a server crash.
        raise InvalidInitData(f"malformed initData: {e}")

    return parsed
