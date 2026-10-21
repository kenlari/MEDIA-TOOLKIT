#!/usr/bin/env python3
"""
Media Toolkit — web version.

YouTube Downloader + Video Splitter + MP3 Converter + TikTok Scheduler,
as a Flask app instead of a desktop Tkinter app. Built so it can run on
Railway (or any host) behind a real HTTPS domain — which TikTok requires
for app review, and which also makes scheduled posts fire even when your
own computer is off (the background poster thread runs on the server).

Run locally:
    pip install -r requirements.txt
    python app.py

Deploy: see README.md (Dockerfile + Railway instructions included).
"""

import os
import time
import datetime as dt
import threading
import traceback
import uuid

from flask import (
    Flask, render_template, request, redirect, url_for, jsonify,
    send_from_directory, flash, abort, Response
)

import yt_dlp

import jobs
import media_tools
import tiktok_store
import tiktok_client

APP_DATA_DIR = os.environ.get("APP_DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
DOWNLOADS_DIR = os.path.join(APP_DATA_DIR, "downloads")
CLIPS_DIR = os.path.join(APP_DATA_DIR, "clips")
CONVERTED_DIR = os.path.join(APP_DATA_DIR, "converted")
UPLOADS_DIR = os.path.join(APP_DATA_DIR, "uploads")

for d in (DOWNLOADS_DIR, CLIPS_DIR, CONVERTED_DIR, UPLOADS_DIR):
    os.makedirs(d, exist_ok=True)

app = Flask(__name__)

# Railway terminates HTTPS at its proxy and forwards plain HTTP to us. Trust the
# X-Forwarded-* headers so url_for(..., _external=True) builds https:// URLs.
# Without this the TikTok redirect_uri comes out as http://... and TikTok
# rejects it ("redirect_uri" error).
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-change-me")
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024 * 1024  # 2GB upload cap


def safe_filename(name):
    name = os.path.basename(name)
    name = "".join(c for c in name if c.isalnum() or c in "._- ")
    return name.strip() or "file"


def list_media_files():
    """Every video/audio file currently in downloads/uploads, newest first — used
    to populate the 'pick an existing file' dropdowns on Splitter/MP3/TikTok."""
    out = []
    for folder, label in ((DOWNLOADS_DIR, "Downloaded"), (UPLOADS_DIR, "Uploaded")):
        if not os.path.isdir(folder):
            continue
        for name in os.listdir(folder):
            path = os.path.join(folder, name)
            if os.path.isfile(path) and os.path.splitext(name)[1].lower() in media_tools.MEDIA_EXTENSIONS:
                out.append({"path": path, "name": name, "label": label, "mtime": os.path.getmtime(path)})
    out.sort(key=lambda f: f["mtime"], reverse=True)
    return out


# ===========================================================================
# Home
# ===========================================================================

@app.route("/")
def home():
    return render_template("index.html")


CONTACT_EMAIL = os.environ.get("CONTACT_EMAIL", "thefitfola@gmail.com")

# YouTube increasingly challenges requests from datacenter IPs (Railway,
# etc.) with "Sign in to confirm you're not a bot," even for public videos.
# A cookies.txt exported from a logged-in browser session on your own
# computer lets yt-dlp present as that signed-in session instead. Uploaded
# once here, stored under APP_DATA_DIR so it survives redeploys.
COOKIES_PATH = os.path.join(APP_DATA_DIR, "cookies.txt")


@app.route("/privacy")
def privacy_page():
    return render_template("privacy.html", contact_email=CONTACT_EMAIL)


@app.route("/terms")
def terms_page():
    return render_template("terms.html", contact_email=CONTACT_EMAIL)


# --- TikTok domain-verification files -------------------------------------
# One-time proof-of-ownership files TikTok asks for when you register the
# Web URL / Privacy Policy URL / Terms of Service URL for the Production
# app. Each must be served as plain text at the exact path TikTok gives you.
# Hardcoded here (rather than a generic catch-all route) so there's no risk
# of clashing with any other route on the site.

@app.route("/tiktokVa2mVGNYjMGPp5BJkGVrTrD1J0U1klfF.txt")
def tiktok_verify_root():
    return Response(
        "tiktok-developers-site-verification=Va2mVGNYjMGPp5BJkGVrTrD1J0U1klfF\n",
        mimetype="text/plain",
    )


@app.route("/privacy/tiktok9AZMt1owpACEu6xlrWZlYSJIxacP4rx6.txt")
def tiktok_verify_privacy():
    return Response(
        "tiktok-developers-site-verification=9AZMt1owpACEu6xlrWZlYSJIxacP4rx6\n",
        mimetype="text/plain",
    )


@app.route("/terms/tiktok4VMjr0QwsBKmbpj0qKeNFRA0IiBx1a1h.txt")
def tiktok_verify_terms():
    return Response(
        "tiktok-developers-site-verification=4VMjr0QwsBKmbpj0qKeNFRA0IiBx1a1h\n",
        mimetype="text/plain",
    )


# ===========================================================================
# YouTube Downloader
# ===========================================================================

@app.route("/downloader", methods=["GET"])
def downloader_page():
    return render_template(
        "downloader.html",
        cookies_set=os.path.isfile(COOKIES_PATH),
    )


@app.route("/downloader/cookies", methods=["POST"])
def downloader_cookies():
    action = request.form.get("action", "upload")
    if action == "clear":
        if os.path.isfile(COOKIES_PATH):
            os.remove(COOKIES_PATH)
        flash("Cookies file removed.", "info")
        return redirect(url_for("downloader_page"))

    f = request.files.get("cookies_file")
    if not f or not f.filename:
        flash("Choose a cookies.txt file first.", "error")
        return redirect(url_for("downloader_page"))
    f.save(COOKIES_PATH)
    flash("Cookies file saved — downloads will now use it.", "info")
    return redirect(url_for("downloader_page"))


@app.route("/downloader/start", methods=["POST"])
def downloader_start():
    url = request.form.get("url", "").strip()
    mode = request.form.get("mode", "video_audio")
    if not url:
        flash("Paste a YouTube link first.", "error")
        return redirect(url_for("downloader_page"))

    job_id = jobs.create_job("download")
    jobs.run_in_background(_run_download, job_id, url, mode)
    return redirect(url_for("job_status_page", job_id=job_id, next="downloader"))


def _run_download(job_id, url, mode):
    outtmpl = os.path.join(DOWNLOADS_DIR, "%(title)s.%(ext)s")

    def hook(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            downloaded = d.get("downloaded_bytes", 0)
            if total:
                pct = downloaded / total * 100
                jobs.update_job(job_id, progress=pct, message=f"Downloading... {pct:.0f}%")
        elif d["status"] == "finished":
            jobs.update_job(job_id, progress=100, message="Processing / converting...")

    cookiefile = COOKIES_PATH if os.path.isfile(COOKIES_PATH) else None
    opts = media_tools.build_ytdlp_opts(mode, outtmpl, progress_hook=hook, cookiefile=cookiefile)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            final_path = info.get("filepath") or info.get("_filename")
            if not final_path:
                try:
                    final_path = ydl.prepare_filename(info)
                except Exception:
                    final_path = None
        filename = os.path.basename(final_path) if final_path and os.path.isfile(final_path) else None
        jobs.finish_job(job_id, result={"filename": filename, "path": final_path, "mode": mode})
    except Exception as e:
        jobs.append_log(job_id, traceback.format_exc())
        jobs.fail_job(job_id, str(e))


# ===========================================================================
# Video Splitter
# ===========================================================================

@app.route("/splitter", methods=["GET"])
def splitter_page():
    preselect = request.args.get("file", "")
    return render_template("splitter.html", files=list_media_files(), preselect=preselect)


@app.route("/splitter/start", methods=["POST"])
def splitter_start():
    existing_path = request.form.get("existing_file", "").strip()
    upload = request.files.get("upload_file")

    in_path = None
    if upload and upload.filename:
        fname = safe_filename(upload.filename)
        in_path = os.path.join(UPLOADS_DIR, fname)
        upload.save(in_path)
    elif existing_path and os.path.isfile(existing_path):
        in_path = existing_path

    if not in_path:
        flash("Choose a video to split (pick an existing one or upload a file).", "error")
        return redirect(url_for("splitter_page"))

    try:
        clip_length = float(request.form.get("clip_length", "60"))
        if clip_length <= 0:
            raise ValueError
    except ValueError:
        flash("Clip length must be a positive number of seconds.", "error")
        return redirect(url_for("splitter_page"))

    aspect = request.form.get("aspect", "original")
    style = {
        "reshape": aspect != "original",
        "aspect": aspect,
        "fit_fill": request.form.get("fit_fill", "fit"),
        "watermark": request.form.get("watermark", "").strip(),
        "watermark_size": int(request.form.get("watermark_size", 28) or 28),
        "watermark_color": request.form.get("watermark_color", "white"),
        "number_enabled": request.form.get("number_enabled") == "on",
        "number_template": request.form.get("number_template", "{n}").strip() or "{n}",
        "number_start": int(request.form.get("number_start", 1) or 1),
        "number_size": int(request.form.get("number_size", 34) or 34),
        "number_color": request.form.get("number_color", "white"),
    }
    precise = request.form.get("precise") == "on"

    job_id = jobs.create_job("split")
    jobs.run_in_background(_run_split, job_id, in_path, clip_length, style, precise)
    return redirect(url_for("job_status_page", job_id=job_id, next="splitter"))


def _run_split(job_id, in_path, clip_length, style, precise):
    out_dir = os.path.join(CLIPS_DIR, job_id)
    try:
        def log(line):
            jobs.append_log(job_id, line)

        def progress_cb(done, total):
            jobs.update_job(job_id, progress=(done / total * 100) if total else 0,
                             message=f"Splitting clip {done} of {total}...")

        def should_stop():
            job = jobs.get_job(job_id)
            return bool(job and job.get("stop_requested"))

        written, errors = media_tools.split_video(
            in_path, out_dir, clip_length, style, precise=precise,
            log=log, progress_cb=progress_cb, should_stop=should_stop,
        )
        if not written:
            jobs.fail_job(job_id, "No clips were created." + (f" Errors: {errors}" if errors else ""))
            return

        zip_name = f"{os.path.splitext(os.path.basename(in_path))[0]}_clips.zip"
        zip_path = os.path.join(CLIPS_DIR, f"{job_id}.zip")
        media_tools.zip_files(written, zip_path)
        jobs.finish_job(job_id, result={
            "zip_filename": os.path.basename(zip_path),
            "zip_job_id": job_id,
            "clip_count": len(written),
            "error_count": len(errors),
        })
    except Exception as e:
        jobs.append_log(job_id, traceback.format_exc())
        jobs.fail_job(job_id, str(e))


@app.route("/splitter/stop/<job_id>", methods=["POST"])
def splitter_stop(job_id):
    jobs.update_job(job_id, stop_requested=True)
    return jsonify({"ok": True})


@app.route("/splitter/download/<job_id>")
def splitter_download(job_id):
    job = jobs.get_job(job_id)
    if not job or job["status"] != "done" or not job.get("result"):
        abort(404)
    return send_from_directory(CLIPS_DIR, f"{job_id}.zip",
                                as_attachment=True, download_name=job["result"]["zip_filename"])


# ===========================================================================
# MP3 Converter
# ===========================================================================

@app.route("/mp3", methods=["GET"])
def mp3_page():
    preselect = request.args.get("file", "")
    return render_template("mp3.html", files=list_media_files(), preselect=preselect)


@app.route("/mp3/start", methods=["POST"])
def mp3_start():
    existing_path = request.form.get("existing_file", "").strip()
    upload = request.files.get("upload_file")

    in_path = None
    if upload and upload.filename:
        fname = safe_filename(upload.filename)
        in_path = os.path.join(UPLOADS_DIR, fname)
        upload.save(in_path)
    elif existing_path and os.path.isfile(existing_path):
        in_path = existing_path

    if not in_path:
        flash("Choose a file to convert (pick an existing one or upload a file).", "error")
        return redirect(url_for("mp3_page"))

    quality = request.form.get("quality", "192k")
    job_id = jobs.create_job("mp3")
    jobs.run_in_background(_run_mp3, job_id, in_path, quality)
    return redirect(url_for("job_status_page", job_id=job_id, next="mp3"))


def _run_mp3(job_id, in_path, quality):
    base_name = os.path.splitext(os.path.basename(in_path))[0]
    out_path = os.path.join(CONVERTED_DIR, f"{job_id}_{base_name}.mp3")
    try:
        jobs.update_job(job_id, message="Converting...", progress=50)
        media_tools.convert_to_mp3(in_path, out_path, quality=quality)
        jobs.finish_job(job_id, result={"filename": os.path.basename(out_path)})
    except Exception as e:
        jobs.append_log(job_id, traceback.format_exc())
        jobs.fail_job(job_id, str(e))


@app.route("/mp3/download/<job_id>")
def mp3_download(job_id):
    job = jobs.get_job(job_id)
    if not job or job["status"] != "done" or not job.get("result"):
        abort(404)
    fname = job["result"]["filename"]
    return send_from_directory(CONVERTED_DIR, fname, as_attachment=True, download_name=fname)


# ===========================================================================
# Shared job-status page / polling API
# ===========================================================================

@app.route("/job/<job_id>")
def job_status_page(job_id):
    next_tool = request.args.get("next", "")
    job = jobs.get_job(job_id)
    if not job:
        abort(404)
    return render_template("job_status.html", job=job, next_tool=next_tool)


@app.route("/api/job/<job_id>")
def job_status_api(job_id):
    job = jobs.get_job(job_id)
    if not job:
        abort(404)
    return jsonify(job)


@app.route("/downloads/<path:filename>")
def serve_download(filename):
    return send_from_directory(DOWNLOADS_DIR, filename, as_attachment=True)


# ===========================================================================
# TikTok Scheduler
# ===========================================================================

@app.route("/tiktok", methods=["GET"])
def tiktok_page():
    store = tiktok_store.load()
    connected = bool(store["tokens"] and store["tokens"].get("access_token"))
    preselect = request.args.get("file", "")
    return render_template(
        "tiktok.html",
        store=store,
        connected=connected,
        files=list_media_files(),
        preselect=preselect,
        privacy_options=tiktok_store.TIKTOK_PRIVACY_OPTIONS,
        redirect_uri=_tiktok_redirect_uri(),
        today=dt.date.today().isoformat(),
    )


def _tiktok_redirect_uri():
    # Built from the live request/host, so this works on any domain this app
    # ends up deployed at without touching code (Railway, a custom domain, etc).
    return url_for("tiktok_callback", _external=True)


@app.route("/tiktok/save_credentials", methods=["POST"])
def tiktok_save_credentials():
    store = tiktok_store.load()
    store["client_key"] = request.form.get("client_key", "").strip()
    store["client_secret"] = request.form.get("client_secret", "").strip()
    tiktok_store.save(store)
    flash("Credentials saved.", "success")
    return redirect(url_for("tiktok_page"))


@app.route("/tiktok/connect", methods=["POST"])
def tiktok_connect():
    store = tiktok_store.load()
    if not store["client_key"] or not store["client_secret"]:
        flash("Enter and save your Client Key/Secret first.", "error")
        return redirect(url_for("tiktok_page"))

    state = tiktok_store.new_state()
    store["pending_states"][state] = {"created_at": time.time()}
    # prune anything older than 10 minutes so this dict never grows unbounded
    store["pending_states"] = {
        s: v for s, v in store["pending_states"].items() if time.time() - v["created_at"] < 600
    }
    tiktok_store.save(store)

    auth_url = tiktok_client.build_authorize_url(store["client_key"], _tiktok_redirect_uri(), state)
    return redirect(auth_url)


@app.route("/tiktok/callback")
def tiktok_callback():
    store = tiktok_store.load()
    error = request.args.get("error")
    state = request.args.get("state")
    code = request.args.get("code")

    if error:
        flash(f"TikTok returned an error: {error}", "error")
        return redirect(url_for("tiktok_page"))
    if not state or state not in store["pending_states"]:
        flash("This login link has expired or doesn't match a recent Connect click. Try again.", "error")
        return redirect(url_for("tiktok_page"))
    store["pending_states"].pop(state, None)
    if not code:
        flash("No authorization code was received from TikTok.", "error")
        tiktok_store.save(store)
        return redirect(url_for("tiktok_page"))

    try:
        tokens = tiktok_client.exchange_code_for_token(
            store["client_key"], store["client_secret"], code, _tiktok_redirect_uri()
        )
        store["tokens"] = tokens
        tiktok_store.save(store)
        flash("TikTok account connected.", "success")
    except Exception as e:
        tiktok_store.save(store)
        flash(f"Connect failed: {e}", "error")
    return redirect(url_for("tiktok_page"))


@app.route("/tiktok/disconnect", methods=["POST"])
def tiktok_disconnect():
    store = tiktok_store.load()
    store["tokens"] = None
    tiktok_store.save(store)
    flash("Disconnected.", "success")
    return redirect(url_for("tiktok_page"))


@app.route("/tiktok/queue/add", methods=["POST"])
def tiktok_queue_add():
    store = tiktok_store.load()
    existing_path = request.form.get("existing_file", "").strip()
    upload = request.files.get("upload_file")

    in_path = None
    if upload and upload.filename:
        fname = safe_filename(upload.filename)
        in_path = os.path.join(UPLOADS_DIR, fname)
        upload.save(in_path)
    elif existing_path and os.path.isfile(existing_path):
        in_path = existing_path

    if not in_path:
        flash("Choose a video to queue (pick an existing one or upload a file).", "error")
        return redirect(url_for("tiktok_page"))

    date_str = request.form.get("post_date", "").strip()
    time_str = request.form.get("post_time", "").strip()
    try:
        when_dt = dt.datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    except ValueError:
        flash("Use YYYY-MM-DD for the date and HH:MM (24h) for the time.", "error")
        return redirect(url_for("tiktok_page"))

    privacy_label = request.form.get("privacy", "")
    privacy = tiktok_store.TIKTOK_PRIVACY_OPTIONS.get(privacy_label, "SELF_ONLY")

    item = {
        "id": uuid.uuid4().hex[:8],
        "file": in_path,
        "caption": request.form.get("caption", "").strip(),
        "privacy": privacy,
        "when": when_dt.isoformat(timespec="minutes"),
        "status": "pending",
    }
    store["queue"].append(item)
    tiktok_store.save(store)
    flash(f"Queued: {os.path.basename(in_path)} for {item['when']}", "success")
    return redirect(url_for("tiktok_page"))


@app.route("/tiktok/queue/<item_id>/remove", methods=["POST"])
def tiktok_queue_remove(item_id):
    store = tiktok_store.load()
    store["queue"] = [i for i in store["queue"] if i["id"] != item_id]
    tiktok_store.save(store)
    return redirect(url_for("tiktok_page"))


@app.route("/tiktok/queue/<item_id>/post_now", methods=["POST"])
def tiktok_queue_post_now(item_id):
    store = tiktok_store.load()
    for item in store["queue"]:
        if item["id"] == item_id:
            item["when"] = dt.datetime.now().isoformat(timespec="minutes")
    tiktok_store.save(store)
    flash("Queued to post immediately — the background poster checks every 20s.", "success")
    return redirect(url_for("tiktok_page"))


# ===========================================================================
# Background TikTok poster — runs for the life of the server process
# ===========================================================================

def _tiktok_poster_loop():
    while True:
        try:
            store = tiktok_store.load()
            now = dt.datetime.now()
            due = [i for i in store["queue"] if i["status"] == "pending"
                   and dt.datetime.fromisoformat(i["when"]) <= now]
            for item in due:
                _publish_queue_item(item["id"])
        except Exception:
            traceback.print_exc()
        time.sleep(20)


def _publish_queue_item(item_id):
    store = tiktok_store.load()
    item = next((i for i in store["queue"] if i["id"] == item_id), None)
    if not item or item["status"] != "pending":
        return
    item["status"] = "posting"
    tiktok_store.save(store)

    store = tiktok_store.load()
    tokens = store.get("tokens")
    if not tokens:
        item = next((i for i in store["queue"] if i["id"] == item_id), None)
        if item:
            item["status"] = "failed (not connected)"
        tiktok_store.save(store)
        return

    try:
        if not tiktok_store.token_is_fresh(tokens):
            tokens = tiktok_client.refresh_token(store["client_key"], store["client_secret"],
                                                  tokens["refresh_token"])
            store["tokens"] = tokens
            tiktok_store.save(store)

        item = next((i for i in store["queue"] if i["id"] == item_id), None)
        publish_id = tiktok_client.publish_video_file(
            tokens["access_token"], item["file"], item["caption"], item["privacy"]
        )
        item["status"] = f"posted ({publish_id[:10]}...)"
    except Exception as e:
        store = tiktok_store.load()
        item = next((i for i in store["queue"] if i["id"] == item_id), None)
        if item:
            item["status"] = f"failed: {e}"
    tiktok_store.save(store)


_poster_thread_started = False
_poster_lock = threading.Lock()


def ensure_poster_thread_started():
    global _poster_thread_started
    with _poster_lock:
        if not _poster_thread_started:
            threading.Thread(target=_tiktok_poster_loop, daemon=True).start()
            _poster_thread_started = True


ensure_poster_thread_started()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
