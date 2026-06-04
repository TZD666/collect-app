#!/bin/bash
# 安装收集模块定时抓取（launchd）。由你手动运行——不会自动执行。
# 装上后，系统每小时唤醒一次 crawler.py，按各监控源的频率增量抓取。
set -e

PLIST="com.bijitai.collect.crawl.plist"
SRC_DIR="/Users/edy/Desktop/笔记台/收集"
DEST="$HOME/Library/LaunchAgents/$PLIST"

case "$1" in
  install)
    mkdir -p "$HOME/Library/LaunchAgents"
    cp "$SRC_DIR/$PLIST" "$DEST"
    launchctl unload "$DEST" 2>/dev/null || true
    launchctl load "$DEST"
    echo "✓ 已安装并加载定时抓取：$PLIST"
    echo "  日志：$SRC_DIR/data/crawl.out.log"
    ;;
  uninstall)
    launchctl unload "$DEST" 2>/dev/null || true
    rm -f "$DEST"
    echo "✓ 已卸载定时抓取"
    ;;
  status)
    launchctl list | grep collect.crawl || echo "（未加载）"
    ;;
  *)
    echo "用法：bash install-crawl.sh [install|uninstall|status]"
    ;;
esac
