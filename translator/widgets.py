"""Reusable Tk components drawn on canvases with the design tokens in theme.py.

Button, IconButton, PillTabs, PillSelect (Combobox-compatible), Toggle,
SettingRow, Banner, Chip, Tooltip, Overlay/Popover/Modal and RecordButton.

Every control:
  - takes keyboard focus (Tab) and shows a visible focus ring,
  - activates with Space/Enter as well as the mouse,
  - accepts `configure(state="disabled"|"normal")` like the ttk widgets it
    replaces, so the app logic did not have to change,
  - carries a `label` (its accessible name), shown as a tooltip on icon-only
    controls.
"""
import math
import time
import tkinter as tk
from typing import Callable, List, Optional, Sequence, Tuple

from . import paint
from . import theme as th
from .theme import mix


def C(name: str) -> str:
    return th.T.c[name]


def S(px: float) -> int:
    return th.T.s(px)


def surface_of(widget) -> str:
    """The widget's background as #RRGGBB (Tk may report a system colour name)."""
    try:
        bg = widget.cget("bg")
        if not bg.startswith("#") or len(bg) != 7:
            r, g, b = widget.winfo_rgb(bg)
            bg = "#%02X%02X%02X" % (r >> 8, g >> 8, b >> 8)
        return bg
    except tk.TclError:
        return C("bg")


def ellipsize(text: str, font, max_w: int) -> str:
    if font.measure(text) <= max_w:
        return text
    while text and font.measure(text + "…") > max_w:
        text = text[:-1]
    return text + "…"


def draw_box(cv: tk.Canvas, x, y, w, h, r, fill, bg, border=None, tags=()):
    """Rounded box of any size on a canvas: anti-aliased corner tiles plus
    plain rectangles and 1px border lines (9-slice)."""
    x, y, w, h, r = int(x), int(y), int(w), int(h), int(min(r, w // 2, h // 2))
    ids = []
    if r > 0:
        tl, tr, bl, br = paint.corners(r, fill, bg, border)
        ids += [cv.create_image(x, y, image=tl, anchor="nw", tags=tags),
                cv.create_image(x + w - r, y, image=tr, anchor="nw", tags=tags),
                cv.create_image(x, y + h - r, image=bl, anchor="nw", tags=tags),
                cv.create_image(x + w - r, y + h - r, image=br, anchor="nw", tags=tags)]
    ids.append(cv.create_rectangle(x + r, y, x + w - r, y + h, fill=fill, outline="", tags=tags))
    if h - 2 * r > 0:
        ids.append(cv.create_rectangle(x, y + r, x + r, y + h - r, fill=fill, outline="", tags=tags))
        ids.append(cv.create_rectangle(x + w - r, y + r, x + w, y + h - r, fill=fill, outline="",
                                       tags=tags))
    if border:
        ids += [cv.create_line(x + r, y, x + w - r, y, fill=border, tags=tags),
                cv.create_line(x + r, y + h - 1, x + w - r, y + h - 1, fill=border, tags=tags),
                cv.create_line(x, y + r, x, y + h - r, fill=border, tags=tags),
                cv.create_line(x + w - 1, y + r, x + w - 1, y + h - r, fill=border, tags=tags)]
    return ids


class Animator:
    """Tweens a value with the theme easing; instant under reduced motion."""

    def __init__(self, widget: tk.Misc, on_value: Callable[[float], None]):
        self.widget = widget
        self.on_value = on_value
        self.value = 0.0
        self._job = None
        widget.bind("<Destroy>", lambda e: self._cancel(), add="+")

    def _cancel(self):
        if self._job is not None:
            try:
                self.widget.after_cancel(self._job)
            except tk.TclError:
                pass
            self._job = None

    def to(self, target: float, ms: int = th.DUR_FAST, start: Optional[float] = None):
        if start is not None:
            self.value = start
        if self._job is not None:
            self.widget.after_cancel(self._job)
            self._job = None
        ms = th.T.dur(ms)
        if ms <= 0 or abs(target - self.value) < 1e-6:
            self.value = target
            self.on_value(target)
            return
        a, t0 = self.value, time.monotonic()

        def step():
            p = min(1.0, (time.monotonic() - t0) * 1000 / ms)
            self.value = a + (target - a) * th.ease(p)
            try:
                self.on_value(self.value)
            except tk.TclError:
                return
            if p < 1.0:
                self._job = self.widget.after(16, step)
            else:
                self._job = None
        step()


# --------------------------------------------------------------------- tooltip
class Tooltip:
    DELAY_MS = 450

    def __init__(self, widget: tk.Misc, text: str):
        self.widget, self.text = widget, text
        self._job = None
        self._tip = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")
        widget.bind("<FocusIn>", self._schedule, add="+")
        widget.bind("<FocusOut>", self._hide, add="+")
        widget.bind("<Destroy>", self._hide, add="+")

    def _schedule(self, _e=None):
        self._cancel()
        if self.text:
            self._job = self.widget.after(self.DELAY_MS, self._show)

    def _cancel(self):
        if self._job is not None:
            try:
                self.widget.after_cancel(self._job)
            except tk.TclError:
                pass
            self._job = None

    def _show(self):
        self._job = None
        if self._tip is not None or not self.widget.winfo_viewable():
            return
        tip = self._tip = tk.Toplevel(self.widget)
        tip.wm_overrideredirect(True)
        tip.attributes("-topmost", True)
        bg = C("text") if not th.T.dark else C("bg_muted")
        tk.Label(tip, text=self.text, bg=bg, fg=C("bg") if not th.T.dark else C("text"),
                 font=th.T.font(12, 500), padx=S(10), pady=S(5), wraplength=S(280),
                 justify="left").pack()
        tip.update_idletasks()
        x = self.widget.winfo_rootx() + self.widget.winfo_width() // 2 - tip.winfo_width() // 2
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + S(6)
        sw = self.widget.winfo_screenwidth()
        tip.geometry(f"+{max(0, min(x, sw - tip.winfo_width()))}+{y}")

    def _hide(self, _e=None):
        self._cancel()
        if self._tip is not None:
            try:
                self._tip.destroy()
            except tk.TclError:
                pass
            self._tip = None


# ------------------------------------------------------------------ controls
class _Control(tk.Canvas):
    """Canvas-drawn control with hover/press/focus/disabled states."""

    def __init__(self, master, width, height, command=None, label=None, tooltip=None,
                 takefocus=True, bg=None):
        self._bg = bg or surface_of(master)
        super().__init__(master, width=width, height=height, bg=self._bg, bd=0,
                         highlightthickness=0, takefocus=1 if takefocus else 0, cursor="hand2")
        self.command = command
        self.label = label  # accessible name
        self.hover = self.pressed = self.focused = False
        self.enabled = True
        self.bind("<Enter>", lambda e: self._set(hover=True))
        self.bind("<Leave>", lambda e: self._set(hover=False, pressed=False))
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<FocusIn>", lambda e: self._set(focused=True))
        self.bind("<FocusOut>", lambda e: self._set(focused=False))
        self.bind("<KeyPress-space>", lambda e: self._key_activate())
        self.bind("<KeyPress-Return>", lambda e: self._key_activate())
        self.bind("<Configure>", lambda e: self.redraw())
        if tooltip or (label and tooltip is not False):
            self.tooltip = Tooltip(self, tooltip or label)

    def _set(self, **kw):
        changed = any(getattr(self, k) != v for k, v in kw.items())
        for k, v in kw.items():
            setattr(self, k, v)
        if changed:
            self.redraw()

    def _on_press(self, e):
        if self.enabled:
            if self.cget("takefocus"):
                self.focus_set()
            self._set(pressed=True)

    def _on_release(self, e):
        was = self.pressed
        self._set(pressed=False)
        if was and self.enabled and 0 <= e.x < self.winfo_width() and 0 <= e.y < self.winfo_height():
            self.activate(e)

    def _key_activate(self):
        if self.enabled:
            self.activate(None)
        return "break"

    def activate(self, event):
        if self.command is not None:
            self.command()

    def configure(self, cnf=None, **kw):
        if isinstance(cnf, dict):
            kw = {**cnf, **kw}
            cnf = None
        if "state" in kw:
            self.enabled = kw.pop("state") != "disabled"
            super().configure(cursor="hand2" if self.enabled else "arrow",
                              takefocus=1 if self.enabled else 0)
            self.redraw()
        if "command" in kw:
            self.command = kw.pop("command")
        if not kw and cnf is None:
            return None
        return super().configure(cnf, **kw)

    config = configure

    def __setitem__(self, key, value):
        self.configure(**{key: value})

    def cget(self, key):
        if key == "state":
            return "normal" if self.enabled else "disabled"
        return super().cget(key)

    def redraw(self):
        raise NotImplementedError

    def focus_ring(self, x, y, w, h, r=None):
        """2px ring in the focus colour, 1px off the shape (shape inset 3px)."""
        if self.focused and self.enabled:
            self.create_image(x, y, anchor="nw", image=paint.pill(w, h, C("focus"), self._bg, r=r))
            self.create_image(x + 2, y + 2, anchor="nw",
                              image=paint.pill(w - 4, h - 4, self._bg, self._bg,
                                               r=None if r is None else max(0, r - 2)))


BUTTON_SIZES = {"sm": (32, 14, 13, 500), "md": (40, 18, 14, 600), "lg": (48, 22, 15, 600)}


def _variant_colors(variant: str, bg: str, hover: bool, pressed: bool, enabled: bool):
    """(fill, border, text) for a button variant and state."""
    if variant == "primary":
        fill = C("primary_active") if pressed else C("primary_hover") if hover else C("primary")
        out = (fill, None, C("on_primary"))
    elif variant == "danger":
        fill = C("danger_active") if pressed else C("danger_hover") if hover else C("danger")
        out = (fill, None, "#FFFFFF")
    elif variant == "soft":
        fill = C("primary_20") if pressed else mix(C("primary"), bg, 0.14) if hover else mix(C("primary"), bg, 0.08)
        out = (fill, None, C("primary"))
    elif variant == "ghost":
        fill = C("bg_muted") if pressed else C("bg_subtle") if hover else bg
        out = (fill, None, C("text_2"))
    else:  # secondary / outline
        fill = C("bg_muted") if pressed else C("bg_subtle") if hover else C("bg")
        out = (fill, C("border"), C("text"))
    if not enabled:
        f, b, t = out
        out = (mix(f, bg, 0.45), b and mix(b, bg, 0.5), mix(t, bg, 0.55))
    return out


class Button(_Control):
    """Pill button with optional leading icon and/or trailing icon."""

    def __init__(self, master, text="", icon=None, icon_right=None, variant="secondary",
                 size="md", command=None, tooltip=None, width=None, max_text_w=None, lang=None):
        self.text, self.icon, self.icon_right = text, icon, icon_right
        self.variant, self.size = variant, size
        self.h, self.pad, self.fpx, self.weight = BUTTON_SIZES[size]
        self.lang = lang
        self.max_text_w = max_text_w
        self.fixed_w = width
        super().__init__(master, 10, S(self.h) + 6, command=command,
                         label=text or tooltip, tooltip=tooltip or False)
        self._resize()

    def _font(self):
        return th.T.font(self.fpx, self.weight, self.lang or th.lang_of(self.text))

    def _content_w(self):
        w = 0
        if self.text:
            tw = self._font().measure(self.text)
            if self.max_text_w:
                tw = min(tw, S(self.max_text_w))
            w += tw
        for ic in (self.icon, self.icon_right):
            if ic:
                w += S(self.fpx + 4) + (S(8) if self.text else 0)
        return w

    def _resize(self):
        w = S(self.fixed_w) if self.fixed_w else self._content_w() + 2 * S(self.pad)
        if not self.text and self.icon:
            w = S(self.h)
        super().configure(width=w + 6, height=S(self.h) + 6)
        self.redraw()

    def configure(self, cnf=None, **kw):
        relayout = False
        for k in ("text", "icon", "icon_right", "variant"):
            if k in kw:
                setattr(self, k, kw.pop(k))
                relayout = True
        out = super().configure(cnf, **kw)
        if relayout:
            self._resize()
        return out

    config = configure

    def _draw_w(self):
        return int(self.cget("width"))

    def redraw(self):
        self.delete("all")
        W, H = self._draw_w(), int(self.cget("height"))
        fill, border, fg = _variant_colors(self.variant, self._bg, self.hover and self.enabled,
                                           self.pressed and self.enabled, self.enabled)
        self.focus_ring(0, 0, W, H)
        self.create_image(3, 3, anchor="nw", image=paint.pill(W - 6, H - 6, fill, self._bg, border))
        font = self._font()
        icon_px = self.fpx + 4
        x = 3 + (W - 6 - self._content_w()) // 2 if not self.fixed_w else 3 + S(self.pad)
        cy = H // 2
        if self.icon:
            if not self.text:
                x = W // 2 - S(icon_px) // 2
            self.create_text(x + S(icon_px) // 2, cy, text=th.T.icon(self.icon),
                             font=th.T.icon_font(icon_px - 2), fill=fg)
            x += S(icon_px) + (S(8) if self.text else 0)
        if self.text:
            avail = W - 6 - 2 * S(self.pad) - (S(icon_px + 8) if self.icon else 0) \
                - (S(icon_px + 8) if self.icon_right else 0)
            txt = ellipsize(self.text, font, max(10, avail))
            self.create_text(x, cy, text=txt, font=font, fill=fg, anchor="w")
            x += font.measure(txt) + S(8)
        if self.icon_right:
            rx = W - 3 - S(self.pad) - S(icon_px) // 2 if self.fixed_w else x + S(icon_px) // 2
            self.create_text(rx, cy, text=th.T.icon(self.icon_right),
                             font=th.T.icon_font(icon_px - 4), fill=fg if self.variant != "secondary" else C("text_3"))


class IconButton(_Control):
    """Circular icon-only button. `label` is required: it is the accessible
    name and the tooltip."""

    def __init__(self, master, icon: str, label: str, command=None, size=36, variant="ghost",
                 toggled=False):
        self.icon, self.d, self.variant = icon, size, variant
        self.toggled = toggled
        super().__init__(master, S(size) + 6, S(size) + 6, command=command, label=label)

    def set_toggled(self, on: bool):
        self.toggled = on
        self.redraw()

    def configure(self, cnf=None, **kw):
        if "icon" in kw:
            self.icon = kw.pop("icon")
            self.redraw()
        return super().configure(cnf, **kw)

    config = configure

    def redraw(self):
        self.delete("all")
        D = int(self.cget("width"))
        variant = "soft" if self.toggled else self.variant
        fill, border, fg = _variant_colors(variant, self._bg, self.hover and self.enabled,
                                           self.pressed and self.enabled, self.enabled)
        if variant == "ghost" and not self.toggled:
            fg = C("text_2") if self.enabled else mix(C("text_2"), self._bg, 0.5)
        self.focus_ring(0, 0, D, D)
        self.create_image(3, 3, anchor="nw", image=paint.circle(D - 6, fill, self._bg, border))
        self.create_text(D // 2, D // 2, text=th.T.icon(self.icon),
                         font=th.T.icon_font(max(12, self.d * 0.42)), fill=fg)


class Toggle(_Control):
    """Switch: on = primary track, white knob; off = white track, grey knob."""
    W, H = 44, 24

    def __init__(self, master, variable: tk.BooleanVar, command=None, label=None):
        self.var = variable
        self.anim = None
        super().__init__(master, S(self.W) + 6, S(self.H) + 6, command=command, label=label,
                         tooltip=False)
        self.anim = Animator(self, lambda v: self.redraw())
        self.anim.value = 1.0 if variable.get() else 0.0
        self._trace = variable.trace_add("write", lambda *a: self._sync())
        self.bind("<Destroy>", lambda e: self._untrace(), add="+")
        self.redraw()

    def _untrace(self):
        try:
            self.var.trace_remove("write", self._trace)
        except (tk.TclError, ValueError):
            pass

    def _sync(self):
        target = 1.0 if self.var.get() else 0.0
        if self.anim is not None and abs(self.anim.value - target) > 1e-6:
            self.anim.to(target, th.DUR_FAST)

    def activate(self, event):
        self.var.set(not self.var.get())
        if self.command is not None:
            self.command()

    def redraw(self):
        if self.anim is None:
            return
        self.delete("all")
        W, H = S(self.W) + 6, S(self.H) + 6
        v = self.anim.value
        on = v >= 0.5
        track = mix(C("primary_hover") if self.hover else C("primary"), C("bg"), v) if v > 0 \
            else (C("bg_subtle") if self.hover else C("bg"))
        if not on and v > 0:
            track = mix(C("primary"), C("bg"), v)
        border = None if v >= 0.99 else C("border")
        if not self.enabled:
            track = mix(track, self._bg, 0.5)
        self.focus_ring(0, 0, W, H)
        self.create_image(3, 3, anchor="nw", image=paint.pill(W - 6, H - 6, track, self._bg, border))
        kd = S(self.H) - 6
        x0 = 3 + 3 + v * (S(self.W) - S(self.H))
        knob = mix("#FFFFFF", "#9CA3AF", v) if not th.T.dark else mix("#FFFFFF", "#7B8698", v)
        if not self.enabled:
            knob = mix(knob, track, 0.4)
        self.create_image(int(x0), 6, anchor="nw", image=paint.circle(kd, knob, track))


class _OptionProxy:
    """Lets the app keep calling `.configure(state=...)` on one tab option."""

    def __init__(self, owner, value):
        self.owner, self.value = owner, value

    def configure(self, state="normal", **_):
        self.owner.set_option_state(self.value, state)

    config = configure


class PillTabs(_Control):
    """Segmented pill control bound to a StringVar. Options: (value, label, icon)."""

    def __init__(self, master, options: Sequence[Tuple[str, str, Optional[str]]],
                 variable: tk.StringVar, command=None, stretch=False, size="md", label=None):
        self.options = list(options)
        self.var = variable
        self.stretch = stretch
        self.h = 44 if size == "md" else 36
        self.fpx = 14 if size == "md" else 13
        self.disabled = set()
        self._hover_i = None
        self._x = []  # segment x ranges
        self.anim = None
        super().__init__(master, 10, S(self.h) + 6, command=command, label=label, tooltip=False)
        self.anim = Animator(self, lambda v: self.redraw())
        self._trace = variable.trace_add("write", lambda *a: self._sync())
        self.bind("<Motion>", self._motion)
        self.bind("<KeyPress-Left>", lambda e: self._step(-1))
        self.bind("<KeyPress-Right>", lambda e: self._step(1))
        self.bind("<Destroy>", lambda e: self._untrace(), add="+")
        if not stretch:
            super().configure(width=self._natural_w() + 6)
        self.anim.value = float(self._index())
        self.redraw()

    def _untrace(self):
        try:
            self.var.trace_remove("write", self._trace)
        except (tk.TclError, ValueError):
            pass

    def option(self, value) -> _OptionProxy:
        return _OptionProxy(self, value)

    def set_option_state(self, value, state):
        (self.disabled.discard if state != "disabled" else self.disabled.add)(value)
        self.redraw()

    def _font(self, i):
        return th.T.font(self.fpx, 600, th.lang_of(self.options[i][1]))

    def _seg_w(self, i):
        _, text, icon = self.options[i]
        return self._font(i).measure(text) + (S(self.fpx + 10) if icon else 0) + 2 * S(16)

    def _natural_w(self):
        return sum(self._seg_w(i) for i in range(len(self.options))) + 2 * S(4)

    def _index(self):
        v = self.var.get()
        return next((i for i, o in enumerate(self.options) if o[0] == v), 0)

    def _sync(self):
        if self.anim is not None:
            self.anim.to(float(self._index()), th.DUR_BASE)

    def _layout(self, W):
        inner = W - 6 - 2 * S(4)
        if self.stretch:
            n = len(self.options)
            seg = [inner / n] * n
        else:
            seg = [self._seg_w(i) for i in range(len(self.options))]
            scale = inner / max(1, sum(seg))
            seg = [s * scale for s in seg]
        xs, x = [], 3 + S(4)
        for s in seg:
            xs.append((x, x + s))
            x += s
        self._x = xs
        return xs

    def _hit(self, x):
        for i, (a, b) in enumerate(self._x):
            if a <= x < b:
                return i
        return None

    def _motion(self, e):
        i = self._hit(e.x)
        if i != self._hover_i:
            self._hover_i = i
            ok = i is not None and self.options[i][0] not in self.disabled and self.enabled
            super().configure(cursor="hand2" if ok else "arrow")
            self.redraw()

    def _pick(self, i):
        if i is None or not self.enabled or self.options[i][0] in self.disabled:
            return
        if self.var.get() != self.options[i][0]:
            self.var.set(self.options[i][0])
            if self.command is not None:
                self.command()

    def _step(self, d):
        i = self._index()
        for _ in range(len(self.options)):
            i = (i + d) % len(self.options)
            if self.options[i][0] not in self.disabled:
                self._pick(i)
                break
        return "break"

    def activate(self, event):
        if event is not None:
            self._pick(self._hit(event.x))

    def _key_activate(self):
        return "break"

    def redraw(self):
        if self.anim is None:
            return
        self.delete("all")
        W, H = self.winfo_width(), int(self.cget("height"))
        if W <= 10:
            W = int(self.cget("width"))
        if W < S(40):
            return
        xs = self._layout(W)
        self.focus_ring(0, 0, W, H)
        self.create_image(3, 3, anchor="nw",
                          image=paint.pill(W - 6, H - 6, C("bg_subtle"), self._bg, C("border")))
        # sliding indicator (white segment) between neighbouring indices
        v = max(0.0, min(len(xs) - 1, self.anim.value))
        lo, hi = int(math.floor(v)), int(math.ceil(v))
        f = v - lo
        a = xs[lo][0] + (xs[hi][0] - xs[lo][0]) * f
        b = xs[lo][1] + (xs[hi][1] - xs[lo][1]) * f
        ih = H - 6 - 2 * S(4)
        self.create_image(int(a), 3 + S(4), anchor="nw",
                          image=paint.pill(int(b - a), ih, C("bg"), C("bg_subtle"), C("border")))
        cur = self._index()
        for i, (value, text, icon) in enumerate(self.options):
            x0, x1 = xs[i]
            dis = value in self.disabled or not self.enabled
            if i == cur:
                fg = C("primary")
            elif self._hover_i == i and not dis:
                fg = C("text")
            else:
                fg = C("text_2")
            if dis and i != cur:
                fg = mix(fg, C("bg_subtle"), 0.5)
            font = self._font(i)
            tw = font.measure(text) + (S(self.fpx + 10) if icon else 0)
            avail = int(x1 - x0) - S(12)
            cx = (x0 + x1) / 2
            x = cx - min(tw, avail) / 2
            if icon:
                self.create_text(x + S(self.fpx) / 2, H / 2, text=th.T.icon(icon),
                                 font=th.T.icon_font(self.fpx + 1), fill=fg)
                x += S(self.fpx + 10)
                avail -= S(self.fpx + 10)
            self.create_text(x, H / 2, text=ellipsize(text, font, max(8, avail)), font=font,
                             fill=fg, anchor="w")


# -------------------------------------------------------------------- overlays
class Overlay:
    """A full-window layer over the app: a canvas showing a snapshot of the
    window (optionally blurred and tinted), with content drawn on top.

    Tk can't blend widgets, so this is how popovers get soft shadows and
    modals get a blurred backdrop. Only one stack per toplevel."""
    stack: List["Overlay"] = []

    def __init__(self, top: tk.Misc, frosted=False):
        self.top = top.winfo_toplevel()
        self.frosted = frosted
        self.closed = False
        self.on_close: Optional[Callable[[], None]] = None
        self._shot = self._snapshot()
        self.cv = tk.Canvas(self.top, highlightthickness=0, bd=0, bg=C("bg_muted"))
        self.cv.place(x=0, y=0, relwidth=1, relheight=1)
        self._bg_item = self.cv.create_image(0, 0, anchor="nw")
        self._render_backdrop()
        self.cv.bind("<Configure>", lambda e: self._on_resize(), add="+")
        Overlay.stack.append(self)

    def _snapshot(self):
        try:
            from PIL import ImageGrab
            self.top.update_idletasks()
            x, y = self.top.winfo_rootx(), self.top.winfo_rooty()
            w, h = self.top.winfo_width(), self.top.winfo_height()
            if w < 2 or h < 2:
                return None
            return ImageGrab.grab(bbox=(x, y, x + w, y + h), all_screens=True)
        except Exception:
            return None

    def _backdrop_image(self, w, h):
        from PIL import Image
        if self._shot is None:
            img = Image.new("RGB", (w, h), th.hex_rgb(C("bg_muted")))
            if not self.frosted:
                return img
        else:
            img = self._shot if self._shot.size == (w, h) else self._shot.resize((w, h))
            img = img.convert("RGB")
        if self.frosted:
            img = paint.frosted(img, C("overlay"))
        return img

    def _render_backdrop(self):
        w, h = max(2, self.top.winfo_width()), max(2, self.top.winfo_height())
        self._base = self._backdrop_image(w, h)
        self.compose(self._base.copy())

    def compose(self, img):
        """Subclasses draw cards/shadows onto `img`; this shows it."""
        self._photo = paint.to_photo(img)
        self.cv.itemconfigure(self._bg_item, image=self._photo)

    def _on_resize(self):
        pass

    def close(self, _e=None):
        if self.closed:
            return
        self.closed = True
        if self in Overlay.stack:
            Overlay.stack.remove(self)
        try:
            self.cv.destroy()
        except tk.TclError:
            pass
        if self.on_close is not None:
            self.on_close()

    @classmethod
    def topmost(cls) -> Optional["Overlay"]:
        return cls.stack[-1] if cls.stack else None


class Popover(Overlay):
    """Option list under an anchor widget (used by PillSelect)."""
    ROW = 36

    def __init__(self, anchor: tk.Misc, items: Sequence[str], selected: Optional[str],
                 on_pick: Callable[[str], None], width=None):
        self.anchor, self.items, self.selected, self.on_pick = anchor, list(items), selected, on_pick
        super().__init__(anchor)
        top = self.top
        ax = anchor.winfo_rootx() - top.winfo_rootx() + 3
        ay = anchor.winfo_rooty() - top.winfo_rooty()
        aw, ah = anchor.winfo_width() - 6, anchor.winfo_height()
        tw, th_ = top.winfo_width(), top.winfo_height()
        font = th.T.font(14, 500)
        need = max((font.measure(t) for t in self.items), default=100) + S(56)
        w = int(min(max(aw, need, S(width or 0)), tw - S(32)))
        rows = len(self.items)
        h = min(rows * S(self.ROW) + S(12), S(320))
        below = th_ - (ay + ah) - S(16)
        y = ay + ah + S(2) if below >= h or below >= ay else ay - h - S(2)
        x = max(S(16), min(ax, tw - w - S(16)))
        self.box = (x, y, w, h)
        img = self._base.copy()
        paint.drop_card(img, x, y, w, h, S(16), C("bg"), C("border"), _scaled(th.SHADOW_POPOVER))
        self.compose(img)
        self.list = tk.Canvas(self.cv, width=w - S(12), height=h - S(12), bg=C("bg"),
                              highlightthickness=0, bd=0, takefocus=1)
        self.cv.create_window(x + S(6), y + S(6), anchor="nw", window=self.list)
        self.active = self.items.index(selected) if selected in self.items else 0
        self.list.configure(scrollregion=(0, 0, w - S(12), rows * S(self.ROW)))
        self._draw()
        self._see(self.active)
        self.cv.bind("<Button-1>", self._outside)
        self.list.bind("<Motion>", self._motion)
        self.list.bind("<Button-1>", self._click)
        self.list.bind("<MouseWheel>", lambda e: self.list.yview_scroll(-e.delta // 120, "units"))
        for k, d in (("<Up>", -1), ("<Down>", 1)):
            self.list.bind(k, lambda e, d=d: self._move(d))
        self.list.bind("<Return>", lambda e: self._choose(self.active))
        self.list.bind("<space>", lambda e: self._choose(self.active))
        self.list.bind("<Escape>", lambda e: (self.close(), "break")[1])
        self.list.bind("<Tab>", lambda e: (self.close(), "break")[1])
        self.list.focus_set()
        self.top.bind("<Configure>", self._top_moved, add="+")

    def _top_moved(self, e):
        if e.widget is self.top and not self.closed:
            self.close()

    def _draw(self):
        cv = self.list
        cv.delete("all")
        W = int(cv.cget("width"))
        R = S(self.ROW)
        for i, text in enumerate(self.items):
            y = i * R
            if i == self.active:
                cv.create_image(0, y + 2, anchor="nw",
                                image=paint.pill(W, R - 4, C("bg_subtle"), C("bg"), r=S(10)))
            sel = text == self.selected
            font = th.T.font(14, 600 if sel else 500, th.lang_of(text))
            cv.create_text(S(14), y + R / 2, text=ellipsize(text, font, W - S(48)), anchor="w",
                           font=font, fill=C("primary") if sel else C("text"))
            if sel:
                cv.create_text(W - S(18), y + R / 2, text=th.T.icon("check"),
                               font=th.T.icon_font(13), fill=C("primary"))

    def _see(self, i):
        R = S(self.ROW)
        total = max(1, len(self.items) * R)
        h = int(self.list.cget("height"))
        top = self.list.canvasy(0)
        if i * R < top:
            self.list.yview_moveto(i * R / total)
        elif (i + 1) * R > top + h:
            self.list.yview_moveto(((i + 1) * R - h) / total)

    def _row_at(self, y):
        i = int(self.list.canvasy(y) // S(self.ROW))
        return i if 0 <= i < len(self.items) else None

    def _motion(self, e):
        i = self._row_at(e.y)
        if i is not None and i != self.active:
            self.active = i
            self._draw()

    def _move(self, d):
        self.active = max(0, min(len(self.items) - 1, self.active + d))
        self._draw()
        self._see(self.active)
        return "break"

    def _click(self, e):
        i = self._row_at(e.y)
        if i is not None:
            self._choose(i)

    def _choose(self, i):
        text = self.items[i]
        anchor = self.anchor
        self.close()
        self.on_pick(text)
        try:
            anchor.focus_set()
        except tk.TclError:
            pass
        return "break"

    def _outside(self, e):
        x, y, w, h = self.box
        if not (x <= e.x < x + w and y <= e.y < y + h):
            self.close()

    def close(self, _e=None):
        try:
            self.anchor.focus_set()
        except tk.TclError:
            pass
        super().close()


def _scaled(shadow):
    dy, blur, a = shadow
    return (S(dy), S(blur), a)


class PillSelect(Button):
    """Pill dropdown, API-compatible with the ttk.Combobox calls the app makes:
    `get()`, `set()`, `["values"] = [...]`, `configure(state=...)`, and it
    fires <<ComboboxSelected>>."""

    def __init__(self, master, values=(), width=None, icon=None, command=None, label=None,
                 placeholder="Choose…"):
        self.values: List[str] = list(values)
        self.value = ""
        self.placeholder = placeholder
        self.on_change = command
        self._icon0 = icon
        super().__init__(master, text=placeholder, icon=icon, icon_right="chevron_down",
                         variant="secondary", size="md", width=width, tooltip=label)
        self.label = label
        self.stretch = width is None
        if self.stretch:
            super(Button, self).configure(width=S(120))
        self.bind("<KeyPress-Down>", lambda e: self._open())

    # Combobox compatibility
    def get(self) -> str:
        return self.value

    def set(self, value: str):
        self.value = value or ""
        self.text = self.value or self.placeholder
        self.redraw()

    def __setitem__(self, key, value):
        if key == "values":
            self.values = list(value)
        else:
            super().__setitem__(key, value)

    def __getitem__(self, key):
        if key == "values":
            return tuple(self.values)
        return self.cget(key)

    def configure(self, cnf=None, **kw):
        if "state" in kw and kw["state"] == "readonly":
            kw["state"] = "normal"
        if "values" in kw:
            self.values = list(kw.pop("values"))
        return super().configure(cnf, **kw)

    config = configure

    def _content_w(self):
        return super()._content_w()

    def _resize(self):
        if getattr(self, "stretch", False):
            super(Button, self).configure(height=S(self.h) + 6)
            self.redraw()
        else:
            super()._resize()

    def _draw_w(self):
        if getattr(self, "stretch", False) and self.winfo_width() > 10:
            return self.winfo_width()
        return int(self.cget("width"))

    def redraw(self):
        if getattr(self, "stretch", False):
            w = self.winfo_width()
            if w > 10:
                self.fixed_w = max(60, (w - 6) / th.T.scale)
            else:
                self.fixed_w = 120
            # narrow: the text matters more than the leading icon
            self.icon = None if 10 < w < S(170) else self._icon0
        super().redraw()
        if not self.value:  # placeholder in tertiary text colour
            for item in self.find_all():
                if self.type(item) == "text" and self.itemcget(item, "text") == self.placeholder:
                    self.itemconfigure(item, fill=C("text_3"))

    def activate(self, event):
        self._open()

    def _open(self):
        if not self.enabled or not self.values:
            return "break"
        self._set(pressed=False, hover=False)
        Popover(self, self.values, self.value, self._picked)
        return "break"

    def _picked(self, value):
        changed = value != self.value
        self.set(value)
        if changed:
            self.event_generate("<<ComboboxSelected>>")
            if self.on_change is not None:
                self.on_change(value)


class Modal(Overlay):
    """Centered rounded card over a blurred, tinted snapshot of the window.
    Closes with ×, Esc, or a click on the backdrop. `build(body)` fills it."""

    def __init__(self, master, title: str, build: Callable[[tk.Frame], None],
                 width=520, subtitle=None, on_close=None, closable=True):
        super().__init__(master, frosted=True)
        self.on_close = on_close
        self.max_w = width
        self.card = tk.Frame(self.cv, bg=C("bg"))
        head = tk.Frame(self.card, bg=C("bg"))
        head.pack(fill="x", padx=S(24), pady=(S(20), S(4)))
        tk.Label(head, text=title, bg=C("bg"), fg=C("text"), font=th.T.font(20, 600, th.lang_of(title)),
                 anchor="w").pack(side="left", fill="x", expand=True)
        if closable:
            self.close_btn = IconButton(head, "close", "Close (Esc)", command=self.close, size=32)
            self.close_btn.pack(side="right")
        if subtitle:
            tk.Label(self.card, text=subtitle, bg=C("bg"), fg=C("text_2"),
                     font=th.T.font(13, 400, th.lang_of(subtitle)), anchor="w", justify="left",
                     wraplength=S(width - 48)).pack(fill="x", padx=S(24))
        self.body = tk.Frame(self.card, bg=C("bg"))
        self.body.pack(fill="both", expand=True, padx=S(24), pady=(S(12), S(24)))
        build(self.body)
        self._req_h = None  # measured once: the card is clipped, not re-measured, on resize
        self._win = self.cv.create_window(0, 0, anchor="nw", window=self.card)
        self.cv.bind("<Button-1>", self._backdrop_click)
        self.layout()
        self.card.focus_set()  # keyboard focus inside the dialog; Tab reaches ×

    def layout(self):
        self.cv.update_idletasks()
        tw, th_ = max(2, self.top.winfo_width()), max(2, self.top.winfo_height())
        w = int(min(S(self.max_w), tw - 2 * S(16)))
        if self._req_h is None:
            self.card.update_idletasks()
            self._req_h = self.card.winfo_reqheight() + 2 * S(6)
        h = int(min(self._req_h, th_ - 2 * S(24)))
        x, y = (tw - w) // 2, max(S(24), (th_ - h) // 2)
        self.box = (x, y, w, h)
        img = self._backdrop_image(tw, th_)
        paint.drop_card(img, x, y, w, h, S(th.RADIUS_CARD), C("bg"), None, _scaled(th.SHADOW_MODAL))
        self.compose(img)
        inset = S(6)
        self.cv.coords(self._win, x + inset, y + inset)
        self.cv.itemconfigure(self._win, width=w - 2 * inset, height=h - 2 * inset)

    def _on_resize(self):
        if not self.closed:
            self.layout()

    def _backdrop_click(self, e):
        x, y, w, h = self.box
        if not (x <= e.x < x + w and y <= e.y < y + h):
            self.close()


# ----------------------------------------------------------- display pieces
class Banner(tk.Canvas):
    """Rounded message strip whose height follows its wrapped text. Hidden
    (zero height) when empty. `configure(text=, tone=|bg=)`."""

    TONES = {"warning": ("warning_10", "warning", "warning"),
             "danger": ("danger_10", "danger_active", "warning"),
             "info": ("primary_10", "primary", "info")}

    def __init__(self, master, text="", tone="warning", fpx=13, solid=False, weight=500,
                 icon=True, bilingual=False):
        self._bg = surface_of(master)
        super().__init__(master, height=1, bg=self._bg, highlightthickness=0, bd=0)
        self.text, self.tone, self.fpx, self.solid, self.weight = text, tone, fpx, solid, weight
        self.fill_override = None
        self.show_icon = icon
        self.bind("<Configure>", lambda e: self.redraw())
        self.redraw()

    def configure(self, cnf=None, **kw):
        changed = False
        for k in ("text", "tone"):
            if k in kw:
                changed |= getattr(self, k) != kw[k]
                setattr(self, k, kw.pop(k))
        if "fg" in kw:  # legacy colour argument: map to a tone
            kw.pop("fg")
        if "bg" in kw and self.solid:
            self.fill_override = kw.pop("bg")
            changed = True
        if changed:
            self.redraw()
        if kw or cnf:
            return super().configure(cnf, **kw)

    config = configure

    def redraw(self):
        self.delete("all")
        W = self.winfo_width()
        if not self.text or W < 20:
            super().configure(height=1)
            return
        if self.solid:
            fill = self.fill_override or C("text_2")
            fg = "#FFFFFF"
        else:
            fill_n, fg_n, _ = self.TONES.get(self.tone, self.TONES["warning"])
            fill, fg = C(fill_n), C(fg_n)
        pad_x, pad_y = S(14), S(10)
        font = th.T.font(self.fpx, self.weight, th.lang_of(self.text))
        x = pad_x
        icon_w = 0
        if self.show_icon and not self.solid:
            icon_w = S(self.fpx + 10)
        t = self.create_text(x + icon_w, pad_y, text=self.text, anchor="nw", font=font, fill=fg,
                             width=W - 2 * pad_x - icon_w)
        x0, y0, x1, y1 = self.bbox(t)
        h = (y1 - y0) + 2 * pad_y
        super().configure(height=h)
        draw_box(self, 0, 0, W, h, min(S(14), h // 2), fill, self._bg, tags=("bgbox",))
        self.tag_lower("bgbox")
        if icon_w:
            _, _, ic = self.TONES.get(self.tone, self.TONES["warning"])
            self.create_text(x + S(self.fpx) // 2, pad_y + font.metrics("linespace") // 2,
                             text=th.T.icon(ic), font=th.T.icon_font(self.fpx), fill=fg)


class Chip(tk.Canvas):
    """Small pill label, e.g. a language badge 'EN → TH'."""

    def __init__(self, master, text, fill, fg, fpx=11, weight=600, bg=None):
        self._bg = bg or surface_of(master)
        font = th.T.font(fpx, weight, th.lang_of(text))
        w = font.measure(text) + S(16)
        h = font.metrics("linespace") + S(4)
        super().__init__(master, width=w, height=h, bg=self._bg, highlightthickness=0, bd=0)
        self.create_image(0, 0, anchor="nw", image=paint.pill(w, h, fill, self._bg))
        self.create_text(w // 2, h // 2, text=text, font=font, fill=fg)


class SettingRow(tk.Frame):
    """Label (with optional ⓘ tooltip) on the left, a control on the right."""

    def __init__(self, master, text: str, info: Optional[str] = None, sub: Optional[str] = None):
        bg = surface_of(master)
        super().__init__(master, bg=bg)
        left = tk.Frame(self, bg=bg)
        left.pack(side="left", fill="x", expand=True, pady=S(6))
        line = tk.Frame(left, bg=bg)
        line.pack(anchor="w")
        self.label = tk.Label(line, text=text, bg=bg, fg=C("text"),
                              font=th.T.font(14, 500, th.lang_of(text)))
        self.label.pack(side="left")
        if info:
            ib = IconButton(line, "info", info, size=24)
            ib.command = lambda: ib.tooltip._show()
            ib.pack(side="left", padx=(S(2), 0))
        if sub:
            tk.Label(left, text=sub, bg=bg, fg=C("text_3"), font=th.T.font(12, 400, th.lang_of(sub)),
                     anchor="w", justify="left").pack(anchor="w")
        self.right = tk.Frame(self, bg=bg)
        self.right.pack(side="right", padx=(S(12), 0))


# ---------------------------------------------------------------- record button
REC_STATES = ("idle", "error", "starting", "listening", "translating", "speaking",
              "reconnecting", "stopping")
REC_HINTS = {
    "idle": "Tap the mic to start translating",
    "error": "Couldn't start. See the message below",
    "starting": "Connecting…",
    "listening": "Listening · tap to stop",
    "translating": "Translating…",
    "speaking": "Speaking the translation",
    "reconnecting": "Reconnecting…",
    "stopping": "Stopping…",
}


class RecordButton(_Control):
    """The big round start/stop button with its hint bubble and live states.

    set_state(state, level=0..1, mic_muted=False). Disabled while starting
    and stopping, so a second click can't start twice."""
    D = 80

    def __init__(self, master, command=None):
        self.state = "idle"
        self.level = 0.0
        self.shown_level = 0.0
        self.mic_muted = False
        self.hint_override = None
        self._t0 = time.monotonic()
        self._job = None
        super().__init__(master, S(340), S(156), command=command, label="Start translating")
        self.bind("<Destroy>", self._cancel_anim, add="+")
        self.redraw()

    def _cancel_anim(self, _e=None):
        if self._job is not None:
            try:
                self.after_cancel(self._job)
            except tk.TclError:
                pass
            self._job = None

    def set_state(self, state: str, level: float = 0.0, mic_muted: bool = False, hint=None):
        changed = (state, mic_muted, hint) != (self.state, self.mic_muted, self.hint_override)
        self.state, self.level, self.mic_muted, self.hint_override = state, level, mic_muted, hint
        self.enabled = state not in ("starting", "stopping")
        self.label = "Start translating" if state in ("idle", "error") else "Stop translating"
        super().configure(cursor="hand2" if self.enabled else "watch")
        if changed:
            self.redraw()
        self._ensure_anim()

    def _animated(self):
        if self.state in ("starting", "stopping", "reconnecting", "listening", "translating",
                          "speaking"):
            return True
        return self.state == "idle" and not th.T.reduced_motion

    def _ensure_anim(self):
        if self._job is None and self._animated():
            self._job = self.after(33, self._tick)

    def _tick(self):
        self._job = None
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return
        # smooth the meter: fast attack, slower release
        target = self.level if self.state in ("listening", "translating") and not self.mic_muted else 0.0
        k = 0.6 if target > self.shown_level else 0.15
        self.shown_level += (target - self.shown_level) * k
        self.redraw()
        self._ensure_anim()

    def redraw(self):
        self.delete("all")
        W, H = int(self.cget("width")), int(self.cget("height"))
        t = time.monotonic() - self._t0
        reduced = th.T.reduced_motion
        st = self.state
        D = S(self.D)
        cx, cy = W // 2, S(94)

        # rings, button and muted badge are one image (Tk can't blend images)
        spec = []
        if st in ("listening", "translating") and not self.mic_muted:
            grow = self.shown_level * S(22)
            if not reduced:
                grow += S(3) * (0.5 + 0.5 * math.sin(t * 3.2))
            d2 = int(D + S(12) + grow) // 2 * 2
            spec += [(0, 0, d2 + S(14), C("danger_10")), (0, 0, d2, C("danger_20"))]
        elif st == "speaking":
            pulse = 0 if reduced else S(4) * (0.5 + 0.5 * math.sin(t * 4.0))
            d2 = int(D + S(14) + pulse) // 2 * 2
            spec.append((0, 0, d2, C("primary_20")))
        if self.focused and self.enabled:
            spec += [(0, 0, D + S(10), C("focus")), (0, 0, D + S(6), self._bg)]
        busy = st in ("starting", "stopping")
        if st == "speaking":
            fill = C("primary_hover") if self.hover else C("primary")
        else:
            fill = C("danger_active") if self.pressed else C("danger_hover") if self.hover else C("danger")
        if busy:
            fill = mix(fill, self._bg, 0.35)
        spec.append((0, 0, D, fill))
        badge = st == "speaking" and self.mic_muted
        bdx = D // 2 - S(8)
        if badge:
            spec += [(bdx, bdx, S(34), self._bg), (bdx, bdx, S(30), C("text"))]
        size = D + S(80)
        half = size // 2
        self.create_image(cx, cy, image=paint.circles(
            size, size, tuple((half + x, half + y, d, c) for x, y, d, c in spec), self._bg))
        icon = {"idle": "mic", "error": "mic", "starting": "mic", "speaking": "speaker",
                "stopping": "stop"}.get(st, "stop")
        self.create_text(cx, cy, text=th.T.icon(icon), font=th.T.icon_font(28), fill="#FFFFFF")
        if badge:
            self.create_text(cx + bdx, cy + bdx, text=th.T.icon("mic_off"),
                             font=th.T.icon_font(13), fill=C("bg"))

        # spinner (starting / stopping / reconnecting)
        if st in ("starting", "stopping", "reconnecting"):
            r = D // 2 + S(7)
            a = (t * 300) % 360 if not reduced else 90
            col = C("warning") if st == "reconnecting" else C("primary")
            self.create_arc(cx - r, cy - r, cx + r, cy + r, start=-a, extent=100, style="arc",
                            outline=col, width=S(3))

        # hint bubble
        text = self.hint_override or REC_HINTS.get(st, "")
        if st == "speaking" and self.mic_muted and not self.hint_override:
            text = "Speaking · mic paused so it won't hear itself"
        if text:
            font = th.T.font(13, 600, th.lang_of(text))
            bw = min(W - 6, font.measure(text) + S(28))
            bh = S(32)
            bob = 0 if (reduced or st != "idle") else S(2) * math.sin(t * 2.4)
            by0 = S(4) + bob
            if st == "idle" and not reduced:
                p = 0.5 + 0.5 * math.sin(t * 2.4)
                bfill = mix(C("primary"), self._bg, 0.10 + 0.06 * p)
            elif st == "error":
                bfill = C("danger_10")
            elif st == "speaking":
                bfill = C("primary_10")
            else:
                bfill = C("bg_subtle")
            fg = C("danger_active") if st == "error" else C("primary") if st in ("idle", "speaking") \
                else C("text_2")
            self.create_image(cx, by0, anchor="n", image=paint.pill(int(bw), bh, bfill, self._bg))
            # little pointer under the bubble
            py = by0 + bh
            self.create_polygon(cx - S(6), py - 1, cx + S(6), py - 1, cx, py + S(6), fill=bfill,
                                outline=bfill)
            self.create_text(cx, by0 + bh / 2, text=ellipsize(text, font, int(bw) - S(20)),
                             font=font, fill=fg)
