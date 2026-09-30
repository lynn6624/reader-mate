#!/bin/bash
# 读伴保活：确保 reader_server.py 在 127.0.0.1:8831 上活着。
# 幂等 —— 已经在跑就直接返回；不在才拉起。
BASE="$(cd "$(dirname "$0")" && pwd)"
cd "$BASE" || exit 1
# 已经在监听就直接走
if command -v curl >/dev/null 2>&1; then
  curl -s -m 2 "http://127.0.0.1:8831/api/ping" >/dev/null 2>&1 && exit 0
fi
mkdir -p data
setsid python3 reader_server.py > data/server.log 2>&1 < /dev/null &
sleep 1
exit 0
