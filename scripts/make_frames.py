"""Render synthetic camera frames of an appliance panel (deterministic test fixtures)."""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFilter, ImageFont

OUT = Path(__file__).resolve().parents[1] / "assets" / "frames"
OUT.mkdir(parents=True, exist_ok=True)

def font(name, size):
    try:
        return ImageFont.truetype(name, size)
    except OSError:
        return ImageFont.load_default()

def panel(display=None, led=None, dark=False, blur=0, name="frame.png"):
    img = Image.new("RGB", (800, 500), (205, 208, 212))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([60, 60, 740, 440], 30, fill=(235, 236, 238), outline=(150, 150, 155), width=4)
    d.text((100, 95), "WM-3000", fill=(30, 30, 35), font=font("arialbd.ttf", 40))
    d.rectangle([380, 170, 700, 290], fill=(25, 30, 28))
    if display:
        d.text((410, 190), display, fill=(235, 235, 235), font=font("consola.ttf", 72))
    d.ellipse([140, 330, 200, 390], fill=(90, 90, 95))  # knob
    if led:
        d.ellipse([610, 340, 650, 380], fill=led)
    if blur:
        img = img.filter(ImageFilter.GaussianBlur(blur))
    if dark:
        img = img.point(lambda v: int(v * 0.08))
    img.save(OUT / name)

panel(display="E-42", name="wm3000_e42.png")
panel(display="E-20", led=(230, 30, 30), name="wm3000_e20_red.png")
panel(display=None, led=(30, 90, 255), name="wm3000_blue_led.png")
panel(display="E-42", dark=True, name="wm3000_dark.png")
print(sorted(p.name for p in OUT.glob("*.png")))
