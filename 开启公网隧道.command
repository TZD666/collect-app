#!/bin/bash
# 收集App 公网隧道（macOS 双击运行）：用 ngrok 把本机服务暴露为免费公网 HTTPS 地址
# 当前公网地址实时写在项目根的 ngrok_url.txt（免费版每次重启地址会变）
cd "$(dirname "$0")" || exit 1
PORT="${PORT:-8780}"

# 服务没在跑就先拉起（沿用本机 .env 配置）
if ! lsof -ti:"$PORT" >/dev/null 2>&1; then
  echo "本地服务未运行，先启动…"
  [ -x .venv/bin/python ] || { python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt; }
  nohup .venv/bin/python run.py >/dev/null 2>&1 &
  sleep 2
fi

PORT="$PORT" .venv/bin/python scripts/ngrok_wrapper.py
