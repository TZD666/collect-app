#!/usr/bin/env python3
"""App 图标生成器（构建期工具，运行时不依赖）。

设计：圆角方形 + 品牌渐变（#4f5bd5 → #8a6cf0，取自 web/index.html 的 CSS 变量
--accent 与 logo 渐变）+ 中央白色五角星（呼应核心交互「打星」）。

依赖 Pillow（pip install pillow，已列入 requirements-dev.txt）。
产物（提交入库，普通用户无需重跑本脚本）：
  web/static/icon-192.png        PWA manifest 图标
  web/static/icon-512.png        PWA manifest 图标（大）
  web/static/apple-touch-icon.png iOS 添加到主屏幕（180×180，满幅不透明，iOS 自行圆角）
用法：.venv/bin/python scripts/make_icons.py
"""
import math
import os

from PIL import Image, ImageDraw

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(BASE, "web", "static")

C1 = (79, 91, 213)    # #4f5bd5
C2 = (138, 108, 240)  # #8a6cf0
S = 2048              # 超采样画布（4x 抗锯齿，最后缩到目标尺寸）


def gradient_bg(size):
    """对角线性渐变背景（左上 C1 → 右下 C2）。"""
    img = Image.new("RGB", (size, size))
    px = img.load()
    span = 2 * (size - 1)
    for y in range(size):
        for x in range(0, size, 4):          # 每 4 列取样一次再横向填充，提速
            t = (x + y) / span
            c = tuple(round(a + (b - a) * t) for a, b in zip(C1, C2))
            for dx in range(min(4, size - x)):
                px[x + dx, y] = c
    return img


def star_points(cx, cy, r_out, size):
    """五角星顶点（顶角朝上，内外半径黄金比例交替）。"""
    r_in = r_out * 0.42
    pts = []
    for i in range(10):
        ang = math.radians(-90 + i * 36)
        r = r_out if i % 2 == 0 else r_in
        pts.append((cx + r * math.cos(ang), cy + r * math.sin(ang)))
    return pts


def make_icon(rounded=True):
    """生成超采样图标；rounded=False 时满幅方形（apple-touch 用）。"""
    img = gradient_bg(S).convert("RGBA")
    draw = ImageDraw.Draw(img)
    # 白色五角星（视觉重心略上，圆心下移 2%）
    draw.polygon(star_points(S / 2, S * 0.52, S * 0.30, S), fill=(255, 255, 255, 255))
    if rounded:
        mask = Image.new("L", (S, S), 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1], radius=int(S * 0.22), fill=255)
        img.putalpha(mask)
    return img


def main():
    os.makedirs(OUT, exist_ok=True)
    rounded = make_icon(rounded=True)
    square = make_icon(rounded=False).convert("RGB")  # iOS 要不透明满幅
    jobs = [
        (rounded, 512, "icon-512.png"),
        (rounded, 192, "icon-192.png"),
        (square, 180, "apple-touch-icon.png"),
    ]
    for src, size, name in jobs:
        p = os.path.join(OUT, name)
        src.resize((size, size), Image.LANCZOS).save(p)
        print(f"✓ {name}  {size}×{size}  {os.path.getsize(p)} 字节")


if __name__ == "__main__":
    main()
