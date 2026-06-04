#!/usr/bin/env python3
"""ngrok 隧道守护：把本机收集App 暴露为免费公网 HTTPS 地址。

接法复刻自舒尔特方格项目的 ngrok_wrapper.py：
  1. 等本地服务就绪 → 2. ngrok 已在跑则附着，否则拉起 → 3. 轮询 4040 本地 API，
  把当前公网地址实时写进项目根的 ngrok_url.txt（免费版地址每次重启会变，以此文件为准）。

用法：.venv/bin/python scripts/ngrok_wrapper.py   （或双击 开启公网隧道.command）
前置：brew install ngrok && ngrok config add-authtoken <你的token>（免费注册）
"""
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time
import urllib.request

BASE = pathlib.Path(__file__).resolve().parent.parent
URL_FILE = BASE / "ngrok_url.txt"
LOG_FILE = BASE / "data" / "ngrok.log"
PORT = int(os.environ.get("PORT", "8780"))
NGROK = shutil.which("ngrok") or "/opt/homebrew/bin/ngrok"
API_URL = "http://127.0.0.1:4040/api/tunnels"   # ngrok 本地状态接口


def get_tunnel_url():
    """从 ngrok 本地 API 取「指向本应用端口」的 https 公网地址；未起返回 None。
    必须按端口匹配——同一 agent 可能还挂着其他应用的隧道，抓第一个会拿错。"""
    try:
        with urllib.request.urlopen(API_URL, timeout=2) as r:
            data = json.load(r)
    except Exception:
        return None
    for t in data.get("tunnels", []):
        url = t.get("public_url", "")
        addr = t.get("config", {}).get("addr", "")
        if url.startswith("https://") and addr.endswith(f":{PORT}"):
            return url
    return None


def write_url(url):
    URL_FILE.write_text(
        f"{url}\nUpdated: {time.strftime('%Y-%m-%d %H:%M:%S')}\n", encoding="utf-8")
    print(f"[隧道地址] {url}（已写入 {URL_FILE.name}）", flush=True)


def main():
    if not os.path.exists(NGROK):
        sys.exit("✗ 未找到 ngrok：brew install ngrok 后 ngrok config add-authtoken <token>")
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    print(f"[1] 等待本地服务 localhost:{PORT} …", flush=True)
    for _ in range(15):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/info", timeout=2)
            break
        except Exception:
            time.sleep(2)
    else:
        sys.exit(f"✗ 本地服务未就绪（先启动收集App，端口 {PORT}）")

    proc = None
    if get_tunnel_url() is None:
        # ngrok 配置文件里定义了 endpoints/tunnels 时用 start --all（多应用共用一个 agent，
        # 免费版只允许一个 agent 会话）；否则临时开单条随机地址隧道
        cfg = pathlib.Path.home() / "Library" / "Application Support" / "ngrok" / "ngrok.yml"
        if cfg.exists() and ("endpoints:" in cfg.read_text() or "tunnels:" in cfg.read_text()):
            cmd = [NGROK, "start", "--all", "--log", "stdout"]
        else:
            cmd = [NGROK, "http", str(PORT), "--log", "stdout"]
        print(f"[2] 拉起 ngrok 隧道（{' '.join(cmd[1:3])}）…", flush=True)
        proc = subprocess.Popen(
            cmd,
            stdout=open(LOG_FILE, "a", encoding="utf-8"),
            stderr=subprocess.STDOUT)
    else:
        print("[2] ngrok 已在运行，直接附着", flush=True)

    last = None
    while True:
        if proc and proc.poll() is not None:
            sys.exit(f"✗ ngrok 进程退出（详见 {LOG_FILE}）")
        url = get_tunnel_url()
        if url and url != last:
            write_url(url)
            last = url
        time.sleep(5)


if __name__ == "__main__":
    main()
