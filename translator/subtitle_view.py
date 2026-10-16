"""SubtitleView: a separate, projector-friendly subtitle window.

Dark background, very large text, bottom-anchored like TV subtitles: the
latest finished sentence (translation large, original smaller), the one
before it dimmed above, and the live (partial) sentence below. Opens as a
normal window so it can be dragged onto the projector screen; F11 (or a
double-click) makes it full screen, Esc leaves full screen / closes.
Rendering is a fixed handful of canvas items, so it costs the same after
five minutes or five hours.
"""
import tkinter as tk
from typing import Callable, Optional

from . import theme as th

BG = "#0B1220"
FG = "#F5F7FB"
FG_DIM = "#8C97AB"
FG_LIVE = "#C9D3E3"
ACCENT = "#5AA2F5"
BTN = "#1C2638"
BTN_HOVER = "#273449"


class SubtitleView:
    SCALES = (0.6, 0.75, 0.9, 1.0, 1.2, 1.45, 1.75)

    def __init__(self, master, on_close: Optional[Callable[[], None]] = None):
        self.on_close = on_close
        self.win = tk.Toplevel(master, bg=BG)
        self.win.title("Subtitles · คำบรรยาย")
        self.win.geometry(f"{th.T.s(1100)}x{th.T.s(560)}")
        self.win.minsize(th.T.s(420), th.T.s(240))
        th.dark_title_bar(self.win, True)
        self.scale_i = 3
        self.show_source = True
        self.fullscreen = False
        self.prev = None   # (src_text, tgt_text, src, tgt)
        self.last = None
        self.live = None
        self.closed = False

        bar = tk.Frame(self.win, bg=BG)
        bar.pack(side="top", fill="x", padx=th.T.s(12), pady=(th.T.s(8), 0))
        self.hint = tk.Label(bar, text="F11 full screen · Esc exit · drag this window to the "
                                       "projector first", bg=BG, fg=FG_DIM,
                             font=th.T.font(12))
        self.hint.pack(side="left")
        for text, cmd, tip in (("✕", self.close, "Close (Esc)"),
                               ("⛶", self.toggle_fullscreen, "Full screen (F11)"),
                               ("A+", lambda: self.zoom(1), "Larger text"),
                               ("A−", lambda: self.zoom(-1), "Smaller text"),
                               ("Original", self.toggle_source, "Show or hide the original text")):
            self._button(bar, text, cmd, tip).pack(side="right", padx=(th.T.s(6), 0))

        self.cv = tk.Canvas(self.win, bg=BG, highlightthickness=0, bd=0)
        self.cv.pack(fill="both", expand=True)
        self.cv.bind("<Configure>", lambda e: self.redraw())
        self.win.bind("<F11>", lambda e: self.toggle_fullscreen())
        self.win.bind("<Escape>", self._escape)
        self.win.bind("<Double-Button-1>", lambda e: self.toggle_fullscreen())
        self.win.bind("<Control-plus>", lambda e: self.zoom(1))
        self.win.bind("<Control-equal>", lambda e: self.zoom(1))
        self.win.bind("<Control-minus>", lambda e: self.zoom(-1))
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        self.redraw()

    def _button(self, parent, text, cmd, tip):
        b = tk.Label(parent, text=text, bg=BTN, fg=FG, font=th.T.font(13, 600),
                     padx=th.T.s(12), pady=th.T.s(4), cursor="hand2", takefocus=1)
        b.bind("<Button-1>", lambda e: cmd())
        b.bind("<Return>", lambda e: cmd())
        b.bind("<space>", lambda e: cmd())
        b.bind("<Enter>", lambda e: b.configure(bg=BTN_HOVER))
        b.bind("<Leave>", lambda e: b.configure(bg=BTN))
        b.bind("<FocusIn>", lambda e: b.configure(highlightthickness=2,
                                                  highlightbackground=ACCENT,
                                                  highlightcolor=ACCENT))
        b.bind("<FocusOut>", lambda e: b.configure(highlightthickness=0))
        from .widgets import Tooltip
        Tooltip(b, tip)
        return b

    # ------------------------------------------------------------- updates
    def show_interim(self, source_text, translated, src, tgt):
        self.live = (source_text, translated, src, tgt)
        self.redraw()

    def show_final(self, source_text, translated, src, tgt):
        self.prev, self.last, self.live = self.last, (source_text, translated, src, tgt), None
        self.redraw()

    # ------------------------------------------------------------ controls
    def zoom(self, d):
        self.scale_i = max(0, min(len(self.SCALES) - 1, self.scale_i + d))
        self.redraw()

    def toggle_source(self):
        self.show_source = not self.show_source
        self.redraw()

    def toggle_fullscreen(self):
        self.fullscreen = not self.fullscreen
        self.win.attributes("-fullscreen", self.fullscreen)
        self.hint.configure(text="Esc to leave full screen" if self.fullscreen else
                            "F11 full screen · Esc exit · drag this window to the projector first")

    def _escape(self, e=None):
        if self.fullscreen:
            self.toggle_fullscreen()
        else:
            self.close()
        return "break"

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.win.destroy()
        except tk.TclError:
            pass
        if self.on_close is not None:
            self.on_close()

    # -------------------------------------------------------------- drawing
    def redraw(self):
        if self.closed:
            return
        cv = self.cv
        cv.delete("all")
        W, H = cv.winfo_width(), cv.winfo_height()
        if W < 50 or H < 50:
            return
        k = self.SCALES[self.scale_i] * max(0.6, min(2.2, W / th.T.s(1100)))
        pad = th.T.s(40)
        wrap = W - 2 * pad
        y = H - th.T.s(28)
        blocks = []  # drawn bottom-up
        if self.live is not None:
            s, t, src, tgt = self.live
            blocks.append((t or "…", tgt or th.lang_of(t), 30 * k, FG_LIVE, 500,
                           s if self.show_source else "", src or th.lang_of(s), 18 * k, FG_DIM))
        if self.last is not None:
            s, t, src, tgt = self.last
            blocks.append((t, tgt, 44 * k, FG, 600,
                           s if self.show_source else "", src, 22 * k, FG_DIM))
        if self.prev is not None and len(blocks) < 2:
            s, t, src, tgt = self.prev
            blocks.append((t, tgt, 28 * k, FG_DIM, 500, "", src, 0, FG_DIM))
        if not blocks:
            cv.create_text(W // 2, H // 2, text="Subtitles appear here when someone speaks\n"
                                                 "คำบรรยายจะแสดงที่นี่เมื่อมีคนพูด",
                           fill=FG_DIM, font=th.T.font(int(24 * k), 500, "th"),
                           justify="center", width=wrap)
            return
        for text, lang, px, fg, weight, sub, sub_lang, sub_px, sub_fg in blocks:
            if sub:
                item = cv.create_text(W // 2, y, text=sub, anchor="s", fill=sub_fg,
                                      font=th.T.font(int(sub_px), 400, sub_lang),
                                      width=wrap, justify="center")
                y = cv.bbox(item)[1] - th.T.s(6)
            item = cv.create_text(W // 2, y, text=text, anchor="s", fill=fg,
                                  font=th.T.font(int(px), weight, lang), width=wrap,
                                  justify="center")
            top = cv.bbox(item)[1]
            if top < th.T.s(4):  # too tall for the window: keep the newest visible
                cv.delete(item)
                break
            y = top - th.T.s(26)
