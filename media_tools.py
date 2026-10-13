"""
Core media logic: YouTube download, video splitting (with optional reshape /
watermark / numbering), and MP3 conversion. Ported from the desktop Tkinter
app's ffmpeg command-building — same behavior, no GUI dependency.
"""

import os
import shutil
import subprocess
import zipfile

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"

ASPECT_TARGETS = {
    "16:9": (1920, 1080),
    "9:16": (1080, 1920),
    "1:1": (1080, 1080),
}

QUALITY_OPTIONS = {
    "128k": "128k",
    "192k": "192k",
    "256k": "256k",
    "320k": "320k",
}

MEDIA_EXTENSIONS = {
    ".mp4", ".mov", ".mkv", ".avi", ".webm", ".flv", ".wmv", ".m4v",
    ".wav", ".m4a", ".aac", ".flac", ".ogg", ".wma",
}

# A font bundled in the Docker image (see Dockerfile) so watermark/numbering
# text always has something to render with, no user setup required.
DEFAULT_FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def escape_drawtext_value(text):
    text = text.replace("\\", "\\\\\\\\")
    text = text.replace(":", "\\:")
    text = text.replace("'", "’")
    text = text.replace("%", "\\%")
    return text


def escape_path_for_filter(path):
    path = path.replace("\\", "/")
    path = path.replace(":", "\\:")
    return path


def get_duration_seconds(path):
    result = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError(f"Could not read video duration:\n{result.stderr}")
    return float(result.stdout.strip())


def format_hms(seconds):
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


# ---------------------------------------------------------------------------
# YouTube download
# ---------------------------------------------------------------------------

def build_ytdlp_opts(mode, outtmpl, progress_hook=None, cookiefile=None):
    common = {
        "outtmpl": outtmpl,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        # YouTube increasingly challenges requests from datacenter/cloud IPs
        # (Railway, etc.) with "Sign in to confirm you're not a bot," even
        # for public videos. A cookies.txt exported from a logged-in browser
        # session lets yt-dlp present as a real signed-in session instead.
        "extractor_args": {"youtube": {"player_client": ["web", "android"]}},
    }
    if cookiefile and os.path.isfile(cookiefile):
        common["cookiefile"] = cookiefile
    if progress_hook:
        common["progress_hooks"] = [progress_hook]

    if mode == "audio_only":
        common.update({
            "format": "bestaudio[protocol^=http]/bestaudio/best",
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3",
                                 "preferredquality": "192"}],
        })
    elif mode == "video_only":
        common.update({"format": "bestvideo[ext=mp4][protocol^=http]/bestvideo[ext=mp4]/bestvideo/best"})
    else:  # video_audio
        common.update({
            "format": ("bestvideo[ext=mp4][protocol^=http]+bestaudio[ext=m4a][protocol^=http]"
                       "/best[ext=mp4][protocol^=http]"
                       "/bestvideo[ext=mp4]+bestaudio[ext=m4a]"
                       "/best[ext=mp4]/best"),
            "merge_output_format": "mp4",
        })
    return common


# ---------------------------------------------------------------------------
# Video splitting (with optional reshape / watermark / numbering)
# ---------------------------------------------------------------------------

def build_style_filter(style, clip_number):
    """Build the -vf filter string for one clip. Returns '' if no styling requested."""
    filters = []

    if style.get("reshape"):
        tw, th = ASPECT_TARGETS[style["aspect"]]
        if style.get("fit_fill", "fit") == "fit":
            filters.append(
                f"scale={tw}:{th}:force_original_aspect_ratio=decrease,"
                f"pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2:color=black"
            )
        else:
            filters.append(f"scale={tw}:{th}:force_original_aspect_ratio=increase,crop={tw}:{th}")

    font_file = style.get("font_file") or DEFAULT_FONT
    font_esc = escape_path_for_filter(font_file)

    if style.get("watermark"):
        wm_esc = escape_drawtext_value(style["watermark"])
        filters.append(
            f"drawtext=fontfile='{font_esc}':text='{wm_esc}':fontsize={style.get('watermark_size', 28)}:"
            f"fontcolor={style.get('watermark_color', 'white')}:box=1:boxcolor=black@0.45:boxborderw=10:"
            f"x=(w-text_w)/2:y=h-text_h-40"
        )

    if style.get("number_enabled"):
        template = style.get("number_template") or "{n}"
        if "{n}" not in template:
            template += " {n}"
        label = template.replace("{n}", str(clip_number))
        num_esc = escape_drawtext_value(label)
        filters.append(
            f"drawtext=fontfile='{font_esc}':text='{num_esc}':fontsize={style.get('number_size', 34)}:"
            f"fontcolor={style.get('number_color', 'white')}:box=1:boxcolor=black@0.45:boxborderw=10:"
            f"x=(w-text_w)/2:y=30"
        )

    return ",".join(filters)


def split_video(in_path, out_dir, clip_length, style, precise=False,
                 log=None, progress_cb=None, should_stop=None):
    """
    Split in_path into fixed-length clips written to out_dir.
    log(str) is called with human-readable progress lines.
    progress_cb(done, total) is called after each clip.
    should_stop() -> bool lets the caller cancel between clips.
    Returns the list of successfully written clip paths.
    """
    log = log or (lambda *_: None)
    duration = get_duration_seconds(in_path)
    num_clips = max(1, int(duration // clip_length) + (1 if duration % clip_length > 0.5 else 0))
    base_name = os.path.splitext(os.path.basename(in_path))[0]
    log(f"Video duration: {format_hms(duration)} | Clip length: {clip_length}s | Estimated clips: {num_clips}")

    os.makedirs(out_dir, exist_ok=True)
    start, index, written, errors = 0.0, 1, [], []
    any_style = style.get("reshape") or style.get("watermark") or style.get("number_enabled")

    while start < duration - 0.25:
        if should_stop and should_stop():
            log("Stopped by user.")
            break

        remaining = duration - start
        this_len = min(clip_length, remaining)
        out_name = f"{base_name}_clip{index:03d}.mp4"
        out_path = os.path.join(out_dir, out_name)

        if any_style:
            clip_number = style.get("number_start", 1) + (index - 1)
            vf = build_style_filter(style, clip_number)
            cmd = [FFMPEG, "-y", "-ss", str(start), "-i", in_path, "-t", str(this_len)]
            if vf:
                cmd += ["-vf", vf]
            cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                    "-c:a", "aac", "-b:a", "160k", out_path]
        elif precise:
            cmd = [FFMPEG, "-y", "-ss", str(start), "-i", in_path, "-t", str(this_len),
                   "-c:v", "libx264", "-c:a", "aac", "-preset", "veryfast", out_path]
        else:
            cmd = [FFMPEG, "-y", "-ss", str(start), "-i", in_path, "-t", str(this_len),
                   "-c", "copy", "-avoid_negative_ts", "make_zero", out_path]

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            errors.append(f"{out_name}: {result.stderr.strip()[-300:]}")
            log(f"FAILED: {out_name}")
        else:
            written.append(out_path)
            log(f"Created: {out_name}")

        if progress_cb:
            progress_cb(index, num_clips)
        start += clip_length
        index += 1

    if errors:
        log(f"{len(errors)} clip(s) had errors.")
    return written, errors


def zip_files(file_paths, zip_path):
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in file_paths:
            zf.write(path, arcname=os.path.basename(path))
    return zip_path


# ---------------------------------------------------------------------------
# MP3 conversion
# ---------------------------------------------------------------------------

def convert_to_mp3(src_path, out_path, quality="192k"):
    cmd = [FFMPEG, "-y", "-i", src_path, "-vn", "-acodec", "libmp3lame", "-b:a", quality, out_path]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip()[-500:])
    return out_path
