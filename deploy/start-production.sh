#!/bin/sh
# ikigai office – production on this Mac, published free over HTTPS with a Cloudflare Tunnel.
#
#   ./deploy/start-production.sh          start (Ctrl+C stops everything)
#
# - Production data lives in data-prod/ (copied once from data/ on the first run),
#   so local development on data/ never touches the live office.
# - The server only listens on 127.0.0.1; the only way in from outside is the tunnel.
# - caffeinate keeps the Mac awake while the office is running.
# - Realtime uses long-polling: Cloudflare quick tunnels hold Server-Sent Events back.
set -eu
cd "$(dirname "$0")/.."

PORT="${PORT:-8080}"
DATA_DIR="$(pwd)/data-prod"

command -v cloudflared >/dev/null || { echo "Thiếu cloudflared. Cài bằng: brew install cloudflared"; exit 1; }

if [ ! -f "$DATA_DIR/db.json" ]; then
  echo "Lần chạy đầu: sao chép dữ liệu hiện tại (mặt bằng, tài khoản) từ data/ sang data-prod/"
  mkdir -p "$DATA_DIR/blobs"
  cp data/db.json "$DATA_DIR/db.json"
  [ -d data/blobs ] && cp -R data/blobs/. "$DATA_DIR/blobs/" 2>/dev/null || true
fi

if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Cổng $PORT đang bận. Dừng chương trình đang dùng cổng này hoặc chạy: PORT=8081 $0"; exit 1
fi

LOG_DIR="$DATA_DIR/logs"; mkdir -p "$LOG_DIR"
IKIGAI_ENV=production IKIGAI_TRANSPORT=poll IKIGAI_TRUST_PROXY=1 IKIGAI_HOST=127.0.0.1 IKIGAI_DATA_DIR="$DATA_DIR" PORT="$PORT" \
  caffeinate -i python3 server.py >"$LOG_DIR/server.log" 2>&1 &
SERVER_PID=$!
cloudflared tunnel --no-autoupdate --url "http://127.0.0.1:$PORT" >"$LOG_DIR/tunnel.log" 2>&1 &
TUNNEL_PID=$!
trap 'echo; echo "Đang dừng..."; kill $TUNNEL_PID $SERVER_PID 2>/dev/null; wait 2>/dev/null; echo "Đã dừng."' INT TERM EXIT

i=0
until curl -fs "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; do
  i=$((i+1)); [ $i -gt 30 ] && { echo "Máy chủ không khởi động được, xem $LOG_DIR/server.log"; exit 1; }; sleep 1
done
URL=""; i=0
while [ -z "$URL" ]; do
  URL=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOG_DIR/tunnel.log" | head -1 || true)
  i=$((i+1)); [ $i -gt 60 ] && { echo "Tunnel chưa lên, xem $LOG_DIR/tunnel.log"; exit 1; }; sleep 1
done

echo ""
echo "  ikigai office đang chạy PRODUCTION"
echo "  Link cho mọi người:  $URL"
echo "  (Link đổi mỗi lần khởi động lại. Giữ cửa sổ này mở và máy Mac bật.)"
echo "  Log: $LOG_DIR/"
echo ""
wait $SERVER_PID
