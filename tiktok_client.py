"""TikTok OAuth + Content Posting API calls."""

import time
import urllib.parse
import requests

AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
PUBLISH_INIT_URL = "https://open.tiktokapis.com/v2/post/publish/video/init/"


def build_authorize_url(client_key, redirect_uri, state):
    return (
        AUTH_URL
        + f"?client_key={urllib.parse.quote(client_key)}"
        + "&scope=" + urllib.parse.quote("user.info.basic,video.publish")
        + "&response_type=code"
        + f"&redirect_uri={urllib.parse.quote(redirect_uri, safe='')}"
        + f"&state={state}"
    )


def exchange_code_for_token(client_key, client_secret, code, redirect_uri):
    resp = requests.post(
        TOKEN_URL,
        data={
            "client_key": client_key,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=20,
    )
    data = resp.json()
    if "access_token" not in data:
        raise RuntimeError(f"TikTok error: {data}")
    data["obtained_at"] = time.time()
    return data


def refresh_token(client_key, client_secret, refresh_token_value):
    resp = requests.post(
        TOKEN_URL,
        data={
            "client_key": client_key,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token_value,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=20,
    )
    data = resp.json()
    if "access_token" not in data:
        raise RuntimeError(f"TikTok refresh error: {data}")
    data["obtained_at"] = time.time()
    return data


def publish_video_file(access_token, file_path, caption, privacy_level):
    """
    Direct-post a local video file to the authenticated user's TikTok account.
    Single-chunk upload — fine for short clips; large files (>~64MB) would
    need TikTok's multi-chunk flow, not implemented here.
    """
    import os
    video_size = os.path.getsize(file_path)

    init_resp = requests.post(
        PUBLISH_INIT_URL,
        json={
            "post_info": {
                "title": caption,
                "privacy_level": privacy_level,
                "disable_duet": False,
                "disable_comment": False,
                "disable_stitch": False,
            },
            "source_info": {
                "source": "FILE_UPLOAD",
                "video_size": video_size,
                "chunk_size": video_size,
                "total_chunk_count": 1,
            },
        },
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=UTF-8",
        },
        timeout=30,
    )
    init_data = init_resp.json()
    if "data" not in init_data or "upload_url" not in init_data.get("data", {}):
        raise RuntimeError(f"TikTok init error: {init_data}")

    upload_url = init_data["data"]["upload_url"]
    publish_id = init_data["data"]["publish_id"]

    with open(file_path, "rb") as f:
        video_bytes = f.read()
    put_resp = requests.put(
        upload_url,
        data=video_bytes,
        headers={
            "Content-Type": "video/mp4",
            "Content-Range": f"bytes 0-{video_size - 1}/{video_size}",
        },
        timeout=180,
    )
    if put_resp.status_code not in (200, 201):
        raise RuntimeError(f"Upload failed: HTTP {put_resp.status_code} {put_resp.text[:300]}")

    return publish_id
