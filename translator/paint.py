"""Anti-aliased shapes for the Tk canvas (Tk's own canvas shapes are jagged).

Shapes are drawn with Pillow at 4x and scaled down, composited onto the
colour they will sit on (so no alpha is needed), and cached as PhotoImages.
"""
from collections import OrderedDict

from PIL import Image, ImageDraw, ImageFilter, ImageTk

from .theme import hex_rgb

SS = 4  # supersampling factor
_photos: "OrderedDict[tuple, ImageTk.PhotoImage]" = OrderedDict()
_MAX = 800


def _cached(key, make):
    img = _photos.get(key)
    if img is None:
        img = ImageTk.PhotoImage(make())
        _photos[key] = img
        while len(_photos) > _MAX:
            _photos.popitem(last=False)
    else:
        _photos.move_to_end(key)
    return img


def rounded_rgba(w: int, h: int, r: int, fill, border=None, bw: int = 1) -> Image.Image:
    """RGBA rounded rectangle (r is clamped to a pill)."""
    w, h = max(1, int(w)), max(1, int(h))
    r = max(0, min(int(r), w // 2, h // 2))
    big = Image.new("RGBA", (w * SS, h * SS), (0, 0, 0, 0))
    d = ImageDraw.Draw(big)
    box = (0, 0, w * SS - 1, h * SS - 1)
    if border and w > 2 * bw and h > 2 * bw:
        d.rounded_rectangle(box, r * SS, fill=hex_rgb(border) + (255,))
        inset = bw * SS
        d.rounded_rectangle((inset, inset, w * SS - 1 - inset, h * SS - 1 - inset),
                            max(0, (r - bw) * SS), fill=hex_rgb(fill) + (255,))
    else:
        d.rounded_rectangle(box, r * SS, fill=hex_rgb(fill) + (255,))
    return big.resize((w, h), Image.LANCZOS)


def on_bg(img: Image.Image, bg: str) -> Image.Image:
    base = Image.new("RGBA", img.size, hex_rgb(bg) + (255,))
    base.alpha_composite(img)
    return base.convert("RGB")


def pill(w, h, fill, bg, border=None, bw=1, r=None):
    """PhotoImage of a rounded rect / pill sitting on `bg`."""
    r = h // 2 if r is None else r
    key = ("pill", int(w), int(h), int(r), fill, bg, border, bw)
    return _cached(key, lambda: on_bg(rounded_rgba(w, h, r, fill, border, bw), bg))


def circle(d, fill, bg, border=None, bw=1):
    return pill(d, d, fill, bg, border, bw, r=d // 2)


def ring(d, color, bg, width, inner_bg=None):
    """A circle outline of `width` px (inner_bg fills the middle, default bg)."""
    key = ("ring", int(d), color, bg, int(width), inner_bg)

    def make():
        big = Image.new("RGBA", (d * SS, d * SS), (0, 0, 0, 0))
        dr = ImageDraw.Draw(big)
        dr.ellipse((0, 0, d * SS - 1, d * SS - 1), fill=hex_rgb(color) + (255,))
        w = width * SS
        dr.ellipse((w, w, d * SS - 1 - w, d * SS - 1 - w),
                   fill=hex_rgb(inner_bg or bg) + (255,))
        return on_bg(big.resize((d, d), Image.LANCZOS), bg)
    return _cached(key, make)


def circles(w: int, h: int, spec, bg):
    """One image with several filled circles drawn in order (no square
    halos where they overlap). spec: ((cx, cy, d, colour), ...)."""
    key = ("circles", int(w), int(h), tuple((int(a), int(b), int(c), d) for a, b, c, d in spec), bg)

    def make():
        big = Image.new("RGB", (w * SS, h * SS), hex_rgb(bg))
        dr = ImageDraw.Draw(big)
        for cx, cy, d, col in spec:
            r = d * SS / 2
            dr.ellipse((cx * SS - r, cy * SS - r, cx * SS + r - 1, cy * SS + r - 1), fill=hex_rgb(col))
        return big.resize((w, h), Image.LANCZOS)
    return _cached(key, make)


def corners(r, fill, bg, border=None, bw=1):
    """Four corner tiles (tl, tr, bl, br) for a 9-slice rounded box on a canvas."""
    key = ("corners", int(r), fill, bg, border, bw)
    out = []
    for i, name in enumerate(("tl", "tr", "bl", "br")):
        k = key + (name,)

        def make(i=i):
            full = on_bg(rounded_rgba(2 * r + 2, 2 * r + 2, r, fill, border, bw), bg)
            x = 0 if i in (0, 2) else r + 2
            y = 0 if i in (0, 1) else r + 2
            return full.crop((x, y, x + r, y + r))
        out.append(_cached(k, make))
    return out


def gradient(w: int, h: int, a: str, b: str) -> Image.Image:
    """Diagonal two-colour gradient (top-left a -> bottom-right b)."""
    w, h = max(1, int(w)), max(1, int(h))
    small = Image.new("RGB", (2, 2))
    ca, cb = hex_rgb(a), hex_rgb(b)
    mid = tuple((x + y) // 2 for x, y in zip(ca, cb))
    small.putdata([ca, mid, mid, cb])
    return small.resize((w, h), Image.BILINEAR)


def drop_card(base: Image.Image, x, y, w, h, r, fill, border, shadow, shadow_rgb=(0, 0, 0)):
    """Draw a rounded card with a soft drop shadow onto `base` (RGB, in place)."""
    dy, blur, alpha = shadow
    pad = blur * 2 + abs(dy)
    sw, sh = int(w + pad * 2), int(h + pad * 2)
    mask = Image.new("L", (sw, sh), 0)
    ImageDraw.Draw(mask).rounded_rectangle((pad, pad + dy, pad + w, pad + dy + h), r,
                                           fill=int(255 * alpha))
    mask = mask.filter(ImageFilter.GaussianBlur(blur / 2))
    shade = Image.new("RGB", (sw, sh), shadow_rgb)
    base.paste(shade, (int(x - pad), int(y - pad)), mask)
    card = rounded_rgba(w, h, r, fill, border)
    base.paste(card, (int(x), int(y)), card)


def frosted(img: Image.Image, overlay) -> Image.Image:
    """Blurred backdrop: blur a screenshot and tint it with the overlay rgba."""
    w, h = img.size
    small = img.convert("RGB").resize((max(1, w // 6), max(1, h // 6)), Image.BILINEAR)
    small = small.filter(ImageFilter.GaussianBlur(3))
    blurred = small.resize((w, h), Image.BILINEAR)
    r, g, b, a = overlay
    tint = Image.new("RGB", (w, h), (r, g, b))
    return Image.blend(blurred, tint, a)


def to_photo(img: Image.Image) -> ImageTk.PhotoImage:
    return ImageTk.PhotoImage(img)
