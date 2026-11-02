"""Рисует иконку приложения: app/static/icon.ico (окно, ярлык) и icon.png (трей)."""
import os

from PIL import Image, ImageDraw, ImageFilter

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app", "static")


def draw(size=256):
    s = size
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    # фон: фиолетовый градиент в скруглённом квадрате
    grad = Image.new("RGBA", (s, s))
    gd = ImageDraw.Draw(grad)
    for y in range(s):
        t = y / s
        gd.line([(0, y), (s, y)], fill=(int(120 + 60 * t), int(50 - 20 * t), int(230 - 60 * t), 255))
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, s - 1, s - 1], radius=s // 4.5, fill=255)
    img.paste(grad, (0, 0), mask)
    d = ImageDraw.Draw(img)
    # стеклянный блик
    hl = Image.new("L", (s, s), 0)
    ImageDraw.Draw(hl).ellipse([-s * 0.3, -s * 0.75, s * 1.3, s * 0.42], fill=60)
    hl = Image.composite(hl, Image.new("L", (s, s), 0), mask)
    img.paste((255, 255, 255, 255), (0, 0), hl)
    # кубик (изометрия) — «3D»
    cx, cy, r = s / 2, s / 2 + s * 0.03, s * 0.29
    top = [(cx, cy - r), (cx + r * 0.87, cy - r / 2), (cx, cy), (cx - r * 0.87, cy - r / 2)]
    left = [(cx - r * 0.87, cy - r / 2), (cx, cy), (cx, cy + r), (cx - r * 0.87, cy + r / 2)]
    right = [(cx, cy), (cx + r * 0.87, cy - r / 2), (cx + r * 0.87, cy + r / 2), (cx, cy + r)]
    shadow = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).polygon(top[:2] + right[2:] + left[3:], fill=(20, 0, 50, 120))
    img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(s / 40)), (0, int(s * 0.02)))
    d = ImageDraw.Draw(img)
    d.polygon(top, fill=(245, 235, 255, 255))
    d.polygon(left, fill=(205, 170, 255, 255))
    d.polygon(right, fill=(165, 110, 245, 255))
    return img


def main():
    img = draw(256)
    img.save(os.path.join(OUT, "icon.png"))
    img.save(os.path.join(OUT, "icon.ico"), sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("готово:", OUT)


if __name__ == "__main__":
    main()
