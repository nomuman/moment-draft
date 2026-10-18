FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MOMENT_DATA_DIR=/data \
    MOMENT_FFMPEG=/usr/bin/ffmpeg \
    MOMENT_HOST=127.0.0.1 \
    MOMENT_PORT=8770 \
    HOME=/tmp

RUN apt-get update \
    && apt-get install --no-install-recommends -y ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 moment \
    && useradd --system --uid 10001 --gid moment --home-dir /tmp moment \
    && mkdir /data \
    && chown moment:moment /data \
    && chmod 0700 /data

WORKDIR /app
COPY LICENSE ./LICENSE
COPY MUSIC_LICENSE ./MUSIC_LICENSE
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=moment:moment moment_draft/ ./moment_draft/
COPY --chown=moment:moment scripts/release_check.py ./scripts/release_check.py

USER moment:moment
EXPOSE 8770
CMD ["python", "-m", "moment_draft"]
