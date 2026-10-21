FROM python:3.11-slim

# ffmpeg for splitting/muxing/MP3 conversion, fonts-dejavu-core for the
# watermark/number text overlay font referenced by media_tools.DEFAULT_FONT.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        fonts-dejavu-core \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Where uploads/downloads/clips and the TikTok/job JSON state live.
# Point this at a mounted Railway Volume so data survives redeploys.
ENV APP_DATA_DIR=/app/data
RUN mkdir -p /app/data

EXPOSE 5000

# Single worker, multiple threads: the app keeps job/queue state and the
# TikTok poster-thread-started flag in process memory, so more than one
# gunicorn worker would see inconsistent state. --threads lets it still
# serve several requests (including long-running download/split jobs,
# which run on their own background threads) concurrently.
# Railway injects $PORT; default to 5000 for local/non-Railway runs.
CMD gunicorn --bind 0.0.0.0:${PORT:-5000} --workers 1 --threads 8 --timeout 120 --forwarded-allow-ips="*" app:app
