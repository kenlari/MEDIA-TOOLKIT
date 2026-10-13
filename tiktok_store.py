"""
TikTok credentials, OAuth tokens, and the post queue — persisted to a JSON
file on disk (under the Railway volume, see README) so they survive restarts.

The redirect_uri is NOT hardcoded: it's built from the incoming request's own
host at connect-time (https://<your-railway-domain>/tiktok/callback), so this
works wherever the app is deployed without editing code. This also sidesteps
the PKCE/localhost issues the desktop version ran into — a plain confidential-
client flow (client_key + client_secret, no code_challenge) is what TikTok's
own Login Kit docs describe, and is what we use here.
"""

import os
import json
import time
import secrets
import threading

DATA_DIR = os.environ.get("APP_DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
STORE_PATH = os.path.join(DATA_DIR, "tiktok_store.json")

TIKTOK_PRIVACY_OPTIONS = {
    "Only me (always used until your app is audited)": "SELF_ONLY",
    "Public": "PUBLIC_TO_EVERYONE",
    "Friends only (mutual follows)": "MUTUAL_FOLLOW_FRIENDS",
    "Followers only": "FOLLOWER_OF_CREATOR",
}

_lock = threading.Lock()


def _default_store():
    return {
        "client_key": "",
        "client_secret": "",
        "tokens": None,       # {access_token, refresh_token, expires_in, obtained_at, open_id, ...}
        "queue": [],           # [{id, file, caption, privacy, when (iso), status}]
        "pending_states": {},  # state -> {created_at}  (CSRF check only; no PKCE)
    }


def load():
    with _lock:
        if os.path.isfile(STORE_PATH):
            try:
                with open(STORE_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                merged = _default_store()
                merged.update(data)
                return merged
            except Exception:
                return _default_store()
        return _default_store()


def save(data):
    with _lock:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp_path = STORE_PATH + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, STORE_PATH)


def new_state():
    return secrets.token_urlsafe(16)


def token_is_fresh(tokens):
    if not tokens:
        return False
    expires_in = tokens.get("expires_in", 0)
    obtained_at = tokens.get("obtained_at", 0)
    return time.time() < obtained_at + expires_in - 120
