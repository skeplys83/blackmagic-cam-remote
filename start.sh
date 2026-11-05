#!/bin/sh
# Start Blackmagic Cam Remote in Docker and open the dashboard.
#   ./start.sh          use the published image (downloads updates)
#   ./start.sh --build  build the image from this source tree
set -e
cd "$(dirname "$0")"
URL=http://localhost:8080

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is not installed. Get Docker Desktop: https://www.docker.com/products/docker-desktop/" >&2
  exit 1
fi
if ! docker info >/dev/null 2>&1; then
  echo "Docker is installed but not running. Start Docker Desktop and run this again." >&2
  exit 1
fi

# Shown on the phone (Wireless debugging → Paired devices) as blackmagic-cam-remote@<this computer>.
export BCR_COMPUTER_NAME="${BCR_COMPUTER_NAME:-$(hostname -s 2>/dev/null || echo docker)}"
if [ "$1" = "--build" ]; then
  docker compose -f compose.yaml -f compose.build.yaml up -d --build
else
  docker compose pull
  docker compose up -d
fi

printf "Waiting for the dashboard"
i=0
until curl -fs "$URL" -o /dev/null; do
  i=$((i + 1))
  if [ "$i" -gt 60 ]; then echo; echo "Not answering yet; check: docker compose logs" >&2; exit 1; fi
  printf "."; sleep 1
done
echo; echo "Open $URL"
if command -v open >/dev/null 2>&1; then open "$URL"; elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$URL"; fi
