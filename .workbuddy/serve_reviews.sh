#!/usr/bin/env bash
# 启动本机人工评审服务（固定 token，重启后链接不变）。
#
# 背景：CLI 的 serve-reviews 原本不暴露 --token，每次启动由上游随机生成，
# 进程一死链接即失效（本项目 2026-10-07 已修复，现支持 --token / --pid-file）。
# 上游 evidence-loop 是独立第三方仓库，改动应在本项目侧接线，不要改其源码。
#
# 用法：bash .workbuddy/serve_reviews.sh [端口] [token]
set -u

cd "$(dirname "$0")/.." || exit 1

PORT="${1:-8765}"
TOKEN="${2:-review-2026-10-07-local}"
PY=".workbuddy/venv/Scripts/python.exe"
READY=".workbuddy/review-server.ready"
PIDFILE=".workbuddy/review-server.pid"

# 重启前先清理同端口的残留监听，避免两个进程并存导致 token 校验失败。
if command -v netstat >/dev/null 2>&1; then
  for pid in $(netstat -ano | grep ":$PORT " | grep LISTENING | awk '{print $NF}' | sort -u); do
    "$PY" -c "import subprocess;subprocess.run(['taskkill','/PID','$pid','/F'],capture_output=True)" 2>/dev/null
  done
  sleep 1
fi

rm -f "$READY"
nohup "$PY" -m review_workflow.cli serve-reviews \
  --port "$PORT" --token "$TOKEN" \
  --ready-file "$READY" --pid-file "$PIDFILE" \
  > .workbuddy/review-server.log 2>&1 &

for _ in $(seq 1 20); do
  [ -f "$READY" ] && break
  sleep 1
done

if [ -f "$READY" ]; then
  echo "评审服务已就绪："
  grep -o 'http[^"]*' "$READY"
  echo "PID: $(cat "$PIDFILE" 2>/dev/null)"
else
  echo "启动失败，日志尾部："
  tail -20 .workbuddy/review-server.log
  exit 1
fi