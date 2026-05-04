#!/usr/bin/env python3
"""Mask sensitive parts of screenshots before publishing.

Hides:
- Cloudflare Quick Tunnel URL in browser title bar
- LINE official account name in notification screenshot
"""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
SHOTS = ROOT / "doc" / "screenshots"

# Try to find a Japanese-capable font for the overlay label
def find_font(size: int):
    candidates = [
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/fonts-japanese-gothic.ttf",
        "/usr/share/fonts/opentype/ipafont-gothic/ipag.ttf",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]
    for p in candidates:
        if Path(p).exists():
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return ImageFont.load_default()

def mask_rect(img: Image.Image, box: tuple, label: str = ""):
    draw = ImageDraw.Draw(img)
    x1, y1, x2, y2 = box
    draw.rectangle(box, fill=(40, 40, 40))
    if label:
        font = find_font(int((y2 - y1) * 0.55))
        try:
            tw = draw.textlength(label, font=font)
        except AttributeError:
            tw, _ = font.getsize(label)
        tx = x1 + ((x2 - x1) - tw) // 2
        ty = y1 + ((y2 - y1) - int((y2 - y1) * 0.7)) // 2
        draw.text((tx, ty), label, fill=(255, 255, 255), font=font)

# All screenshots are 1280 x 2772
TARGETS = [
    # (filename, [(x1, y1, x2, y2, label), ...])
    ("family_dashboard.jpg", [
        # URL bar with trycloudflare.com link (status bar to just above blue header)
        (0, 110, 1280, 320, "[公開URLマスク済み]"),
    ]),
    ("tablet_clock_alerts.jpg", [
        (0, 180, 1280, 410, "[公開URLマスク済み]"),
    ]),
    ("line_notifications.jpg", [
        # LINE chat header showing official-account name (extend further down to fully cover)
        (0, 100, 1280, 320, "[アカウント名マスク済み]"),
    ]),
]

for name, boxes in TARGETS:
    src = SHOTS / name
    img = Image.open(src).convert("RGB")
    for *box, label in boxes:
        mask_rect(img, tuple(box), label)
    img.save(src, "JPEG", quality=88)
    print(f"masked: {src.name}")
