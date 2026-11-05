FROM debian:trixie-slim

ARG SCRCPY_VERSION=5.0.1
ARG SCRCPY_SHA256=764eb6f79811d5211fe9df341120882ba9994c7a61b897d7bf3fb662e53bc536

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 adb ffmpeg qrencode ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

# scrcpy-server runs on the phone (over adb) for the live view, sound and screen-off.
RUN curl -fsSL -o /usr/local/share/scrcpy-server \
      "https://github.com/Genymobile/scrcpy/releases/download/v${SCRCPY_VERSION}/scrcpy-server-v${SCRCPY_VERSION}" \
 && echo "${SCRCPY_SHA256}  /usr/local/share/scrcpy-server" | sha256sum -c -

WORKDIR /app
COPY server.py index.html ./

# /data keeps adb pairing keys (HOME/.android), settings and thumbnails across restarts.
ENV HOME=/data \
    PCR_DATA=/data \
    PCR_HOST=0.0.0.0 \
    PCR_PORT=8080 \
    SCRCPY_SERVER=/usr/local/share/scrcpy-server \
    SCRCPY_VERSION=${SCRCPY_VERSION} \
    PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s CMD curl -fs http://127.0.0.1:8080/ -o /dev/null || exit 1
CMD ["python3", "-I", "/app/server.py"]
