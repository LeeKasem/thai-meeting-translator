"""Design tokens for the Tk UI: colours, radii, shadows, motion and fonts.

Tk has no CSS, so the tokens live here and every widget reads them from the
module-level `T` (set up once by `init()` after the Tk root exists). Light and
dark palettes follow the Windows app theme; motion follows the Windows
"Animation effects" setting (the equivalent of prefers-reduced-motion).
"""
import ctypes
import sys
from pathlib import Path
from typing import Optional

ASSETS = Path(__file__).resolve().parent / "assets"

LIGHT = {
    "primary": "#1073E6", "primary_hover": "#2283F5", "primary_active": "#0A5EC2",
    "on_primary": "#FFFFFF",
    "text": "#334260", "text_2": "#5A6475", "text_3": "#6B7280",
    "bg": "#FFFFFF", "bg_subtle": "#F9FAFB", "bg_muted": "#EDEFF3",
    "border": "#E5E7EB",
    "danger": "#F25767", "danger_hover": "#F46B79", "danger_active": "#DC4253",
    "success": "#0D8D46", "warning": "#A35F00",
    "overlay": (203, 210, 220, 0.6),
    # page background: very subtle light blue -> light green
    "page_a": "#EEF5FE", "page_b": "#EFF8F3",
    # second speaker accent (two-way / onsite)
    "accent_2": "#0D8D46",
    "shadow": (0, 0, 0),
}

DARK = {
    "primary": "#3D8EF0", "primary_hover": "#5AA2F5", "primary_active": "#2276DC",
    "on_primary": "#FFFFFF",
    "text": "#E4E9F2", "text_2": "#AEB7C6", "text_3": "#949EAE",
    "bg": "#172030", "bg_subtle": "#1C2638", "bg_muted": "#253145",
    "border": "#2E3B50",
    "danger": "#F25767", "danger_hover": "#F46B79", "danger_active": "#DC4253",
    "success": "#3CC57A", "warning": "#F0B45A",
    "overlay": (8, 12, 20, 0.6),
    "page_a": "#0E1522", "page_b": "#0F1A1A",
    "accent_2": "#3CC57A",
    "shadow": (0, 0, 0),
}

RADIUS_PILL = 9999
RADIUS_CARD = 16
# (offset_y, blur, rgba alpha) for the two shadow levels in the spec
SHADOW_POPOVER = (2, 6, 0.12)
SHADOW_MODAL = (8, 26, 0.30)
SHADOW_CARD = (4, 18, 0.06)

# Motion, in ms, and the spec's easing curve.
DUR_FAST, DUR_BASE, DUR_SLOW = 150, 250, 350
EASING = (0.32, 0.72, 0.0, 1.0)

UI_FAMILY = "Plus Jakarta Sans"
THAI_FAMILY = "Noto Sans Thai"
ICON_FAMILIES = ("Segoe Fluent Icons", "Segoe MDL2 Assets")

# Segoe Fluent Icons / MDL2 code points (same in both fonts).
ICONS = {
    "mic": "", "mic_off": "", "speaker": "", "mute": "",
    "swap": "", "settings": "", "play": "", "stop": "",
    "close": "", "search": "", "people": "", "headphones": "",
    "info": "", "download": "", "refresh": "", "font": "",
    "chevron_down": "", "chevron_up": "", "arrow_down": "",
    "check": "", "translate": "", "globe": "", "voice": "",
    "lock": "", "warning": "", "skip": "", "replay": "",
    "person": "",
}
# Plain-Unicode fallbacks when no icon font is installed.
ICON_FALLBACK = {
    "mic": "🎤", "mic_off": "🔇", "speaker": "🔊", "mute": "🔇", "swap": "⇄",
    "settings": "⚙", "play": "▶", "stop": "■", "close": "×", "search": "⌕",
    "people": "👥", "headphones": "🎧", "info": "ⓘ", "download": "⤓", "refresh": "↻",
    "font": "A", "chevron_down": "▾", "chevron_up": "▴", "arrow_down": "↓", "check": "✓",
    "translate": "文", "globe": "🌐", "voice": "🗣", "lock": "🔒", "warning": "⚠",
    "skip": "⏭", "replay": "↻", "person": "👤",
}


def cubic_bezier(p1x, p1y, p2x, p2y):
    """CSS cubic-bezier() as a function of progress 0..1."""
    def bez(t, a, b):
        return 3 * a * (1 - t) ** 2 * t + 3 * b * (1 - t) * t * t + t ** 3

    def ease(x):
        if x <= 0:
            return 0.0
        if x >= 1:
            return 1.0
        lo, hi = 0.0, 1.0
        for _ in range(24):  # bisection on x(t)
            mid = (lo + hi) / 2
            if bez(mid, p1x, p2x) < x:
                lo = mid
            else:
                hi = mid
        return bez((lo + hi) / 2, p1y, p2y)
    return ease


ease = cubic_bezier(*EASING)


def hex_rgb(c: str):
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def rgb_hex(rgb) -> str:
    return "#%02X%02X%02X" % tuple(max(0, min(255, round(v))) for v in rgb[:3])


def mix(fg: str, bg: str, alpha: float) -> str:
    """`fg` at `alpha` opacity over `bg` (Tk has no alpha, so tints are mixed)."""
    a, b = hex_rgb(fg), hex_rgb(bg)
    return rgb_hex([b[i] + (a[i] - b[i]) * alpha for i in range(3)])


def system_dark() -> bool:
    """Windows 'Choose your app mode: Dark'."""
    if sys.platform != "win32":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
    except OSError:
        return False


def system_reduced_motion() -> bool:
    """Windows 'Animation effects' off -> reduce motion."""
    if sys.platform != "win32":
        return False
    try:
        enabled = ctypes.c_bool(True)
        SPI_GETCLIENTAREAANIMATION = 0x1042
        ctypes.windll.user32.SystemParametersInfoW(SPI_GETCLIENTAREAANIMATION, 0,
                                                    ctypes.byref(enabled), 0)
        return not enabled.value
    except Exception:
        return False


def register_fonts() -> None:
    """Make the bundled fonts visible to this process only (no install)."""
    if sys.platform != "win32":
        return
    FR_PRIVATE = 0x10
    for p in sorted((ASSETS / "fonts").glob("*.ttf")):
        try:
            ctypes.windll.gdi32.AddFontResourceExW(str(p), FR_PRIVATE, 0)
        except Exception:
            pass


class Theme:
    def __init__(self, root, dark: Optional[bool] = None, reduced_motion: Optional[bool] = None):
        from tkinter import font as tkfont
        self.dark = system_dark() if dark is None else dark
        self.reduced_motion = system_reduced_motion() if reduced_motion is None else reduced_motion
        self.c = dict(DARK if self.dark else LIGHT)
        c = self.c
        for pct in (10, 20, 30):  # primary tints at 10/20/30 % over the surface
            c[f"primary_{pct}"] = mix(c["primary"], c["bg"], pct / 100)
            c[f"primary_{pct}_subtle"] = mix(c["primary"], c["bg_subtle"], pct / 100)
        c["danger_10"] = mix(c["danger"], c["bg"], 0.10)
        c["danger_20"] = mix(c["danger"], c["bg"], 0.20)
        c["success_10"] = mix(c["success"], c["bg"], 0.10)
        c["accent_2_10"] = mix(c["accent_2"], c["bg"], 0.10)
        c["warning_10"] = mix(c["warning"], c["bg"], 0.12)
        c["focus"] = mix(c["primary"], c["bg"], 0.45)

        # Layout sizes are given in CSS px at 96 DPI and scaled to the screen.
        self.scale = (root.winfo_fpixels("1i") / 96.0) if root is not None else 1.0
        families = set(tkfont.families(root)) if root is not None else set()
        self.ui = UI_FAMILY if UI_FAMILY in families else "Segoe UI"
        self.thai = THAI_FAMILY if THAI_FAMILY in families else next(
            (f for f in ("Leelawadee UI", "Leelawadee", "Tahoma") if f in families), self.ui)
        self.icon_family = next((f for f in ICON_FAMILIES if f in families), None)
        self._families = families
        self._fonts = {}

    def family(self, lang: str = "en", weight: int = 400) -> str:
        """Static-weight family name (Tk only knows normal/bold)."""
        base = self.thai if lang == "th" else self.ui
        if weight in (500, 600):
            name = f"{base} {'Medium' if weight == 500 else 'SemiBold'}"
            if name in self._families:
                return name
        return base

    def s(self, px: float) -> int:
        """CSS px -> screen pixels."""
        return int(round(px * self.scale))

    def font(self, px: float, weight: int = 400, lang: str = "en"):
        """Cached tkfont.Font. `px` is the CSS size; weight 400/500/600/700."""
        from tkinter import font as tkfont
        key = (px, weight, lang)
        f = self._fonts.get(key)
        if f is None:
            fam = self.family(lang, weight)
            # Points, so Tk scales them with the screen DPI like the layout.
            f = tkfont.Font(family=fam, size=max(1, round(px * 0.75)),
                            weight="bold" if weight >= 700 else "normal")
            self._fonts[key] = f
        return f

    def icon_font(self, px: float):
        from tkinter import font as tkfont
        key = ("icon", px)
        f = self._fonts.get(key)
        if f is None:
            f = tkfont.Font(family=self.icon_family or self.ui, size=max(1, round(px * 0.75)))
            self._fonts[key] = f
        return f

    def icon(self, name: str) -> str:
        return (ICONS if self.icon_family else ICON_FALLBACK).get(name, "?")

    def dur(self, ms: int) -> int:
        return 0 if self.reduced_motion else ms


T: Optional[Theme] = None


def init(root, **kw) -> Theme:
    global T
    T = Theme(root, **kw)
    return T


def lang_of(text: str) -> str:
    """'th' if the text contains Thai script, else 'en' (picks the font)."""
    return "th" if any("฀" <= ch <= "๿" for ch in text or "") else "en"


def dark_title_bar(root, dark: bool) -> None:
    """Match the Windows title bar to the theme (Windows 10 2004+ / 11)."""
    if sys.platform != "win32":
        return
    try:
        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        value = ctypes.c_int(1 if dark else 0)
        for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (new, old)
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(value),
                                                          ctypes.sizeof(value)) == 0:
                break
    except Exception:
        pass
