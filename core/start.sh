#!/usr/bin/env bash
# 读伴（ReaderMate）· v1.0 · Linux 容器 / proot 环境 启动脚本（proot Ubuntu / aarch64）
# 用法：bash start.sh      停止：bash stop.sh
set -u

# —— UTF-8 三件套：proot 默认 locale 不是 UTF-8，不设的话 print 中文路径会 UnicodeEncodeError ——
export LANG=C.UTF-8
export LC_ALL=C.UTF-8
export PYTHONIOENCODING=utf-8

# —— 端口与绑定（另一台设备侧默认 8831，避开常见占用）——
export READER_HOST="${READER_HOST:-127.0.0.1}"
export READER_PORT="${READER_PORT:-8831}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || { echo "[fail] 切目录失败: $SCRIPT_DIR" >&2; exit 1; }

DATA_DIR="$SCRIPT_DIR/data"
BOOKS_DIR="$SCRIPT_DIR/书库"
mkdir -p "$DATA_DIR" "$BOOKS_DIR" || { echo "[fail] 建 data/ 与 书库/ 失败" >&2; exit 1; }

PID_FILE="$DATA_DIR/server.pid"
if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "[skip] 已经在跑 (PID $(cat "$PID_FILE"))，地址 http://$READER_HOST:$READER_PORT/"
  exit 0
fi

LOG_FILE="$DATA_DIR/server.log"
ERR_FILE="$DATA_DIR/server.err.log"
: > "$LOG_FILE"
: > "$ERR_FILE"

nohup python3 reader_server.py >>"$LOG_FILE" 2>>"$ERR_FILE" &
SERVER_PID=$!
echo "$SERVER_PID" > "$PID_FILE"

READY=0
for _ in $(seq 1 30); do
  if (exec 3<>/dev/tcp/"$READER_HOST"/"$READER_PORT") 2>/dev/null; then
    exec 3<&- 3>&- 2>/dev/null
    READY=1
    break
  fi
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "[fail] 进程已退出，看日志：$ERR_FILE" >&2
    rm -f "$PID_FILE"
    exit 1
  fi
  sleep 0.5
done

if [ "$READY" -eq 1 ]; then
  echo "读伴 · v1.0 已起来：http://$READER_HOST:$READER_PORT/   (PID $SERVER_PID)"
  echo "日志：$LOG_FILE / $ERR_FILE"
else
  echo "[fail] 15 秒内端口未就绪，PID=$SERVER_PID，看 $ERR_FILE" >&2
  exit 2
fi
