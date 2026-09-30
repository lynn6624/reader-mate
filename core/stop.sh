#!/usr/bin/env bash
# 读伴（ReaderMate）· v1.0 · Linux 容器 / proot 环境 停止脚本
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$SCRIPT_DIR/data/server.pid"

if [ ! -f "$PID_FILE" ]; then
  echo "[skip] 没有 PID 文件，服务应该没在跑"
  exit 0
fi

PID="$(cat "$PID_FILE")"
if kill -0 "$PID" 2>/dev/null; then
  kill -TERM "$PID" 2>/dev/null
  for _ in $(seq 1 10); do
    kill -0 "$PID" 2>/dev/null || break
    sleep 0.5
  done
  if kill -0 "$PID" 2>/dev/null; then
    echo "[warn] 优雅退出超时，强杀 $PID"
    kill -9 "$PID" 2>/dev/null
  fi
  echo "读伴已停止 (PID $PID)"
else
  echo "[skip] PID $PID 已经不在"
fi
rm -f "$PID_FILE"
