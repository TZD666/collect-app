#!/bin/bash
# 收集App 一键启动器（macOS 双击运行）
# 功能：自动准备环境 → 起服务（开放局域网）→ 打开浏览器
cd "$(dirname "$0")" || exit 1

PORT="${PORT:-8770}"

# 已在跑（端口被本目录的 run.py 占用）→ 直接开浏览器
if lsof -ti:"$PORT" >/dev/null 2>&1; then
  echo "✓ 服务已在运行（端口 $PORT），直接打开浏览器"
  open "http://127.0.0.1:$PORT"
  exit 0
fi

# 首次运行：自动建虚拟环境 + 装依赖
if [ ! -x ".venv/bin/python" ]; then
  echo "首次运行：创建虚拟环境并安装依赖（约 1 分钟）…"
  python3 -m venv .venv || { echo "✗ 需要先安装 Python 3"; read -r; exit 1; }
  .venv/bin/pip install -q -r requirements.txt || { echo "✗ 依赖安装失败"; read -r; exit 1; }
fi

echo "✓ 启动收集App（端口 $PORT，已开放局域网访问）…"
HOST=0.0.0.0 PORT="$PORT" .venv/bin/python run.py &
sleep 2
open "http://127.0.0.1:$PORT"
echo "─────────────────────────────────────"
echo " 关闭本窗口不会停止服务；要停止请执行："
echo "   lsof -ti:$PORT | xargs kill"
echo "─────────────────────────────────────"
wait
