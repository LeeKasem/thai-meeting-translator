"""VoicePicker: modal to choose the English and Thai voices.

Search, gender filter chips, one tab per language with counts, a responsive
grid (3 / 2 / 1 columns) of voices with avatar, name and a ▶ preview.
"""
import threading
import tkinter as tk
from typing import Callable, Dict, List, Optional

from . import config, paint
from . import theme as th
from .voices import Voice, get_voices
from .widgets import (C, S, Banner, Button, IconButton, Modal, PillTabs, draw_box,
                      ellipsize, mix, surface_of)

GENDERS = (("all", "All", None), ("Female", "Female", None), ("Male", "Male", None))


class SearchField(tk.Canvas):
    """Pill text input with a search icon and a visible focus ring."""
    H = 40

    def __init__(self, master, variable: tk.StringVar, placeholder="Search"):
        self._bg = surface_of(master)
        super().__init__(master, height=S(self.H) + 6, bg=self._bg, highlightthickness=0, bd=0)
        self.var = variable
        self.placeholder = placeholder
        self.entry = tk.Entry(self, textvariable=variable, bd=0, relief="flat", bg=C("bg"),
                              fg=C("text"), insertbackground=C("text"), font=th.T.font(14),
                              highlightthickness=0)
        self._win = self.create_window(0, 0, anchor="w", window=self.entry)
        self._focused = False
        self.entry.bind("<FocusIn>", lambda e: self._focus(True))
        self.entry.bind("<FocusOut>", lambda e: self._focus(False))
        self.bind("<Configure>", lambda e: self.redraw())
        self.bind("<Button-1>", lambda e: self.entry.focus_set())
        variable.trace_add("write", lambda *a: self.redraw())

    def _focus(self, on):
        self._focused = on
        self.redraw()

    def redraw(self):
        self.delete("deco")
        W, H = self.winfo_width(), S(self.H) + 6
        if W < 20:
            return
        if self._focused:
            self.create_image(0, 0, anchor="nw", tags="deco",
                              image=paint.pill(W, H, C("focus"), self._bg))
            self.create_image(2, 2, anchor="nw", tags="deco",
                              image=paint.pill(W - 4, H - 4, self._bg, self._bg))
        self.create_image(3, 3, anchor="nw", tags="deco",
                          image=paint.pill(W - 6, H - 6, C("bg"), self._bg,
                                           C("primary") if self._focused else C("border")))
        self.create_text(3 + S(18), H // 2, text=th.T.icon("search"), font=th.T.icon_font(15),
                         fill=C("text_3"), tags="deco")
        if not self.var.get() and not self._focused:
            self.create_text(3 + S(40), H // 2, text=self.placeholder, anchor="w",
                             font=th.T.font(14), fill=C("text_3"), tags="deco")
        self.tag_lower("deco")
        self.coords(self._win, 3 + S(38), H // 2)
        self.itemconfigure(self._win, width=W - 6 - S(56))
        if not self.var.get() and not self._focused:
            self.itemconfigure(self._win, state="hidden")
        else:
            self.itemconfigure(self._win, state="normal")

    def focus_set(self):
        self.itemconfigure(self._win, state="normal")
        self.entry.focus_set()


class VoicePicker:
    def __init__(self, master, current: Dict[str, str], on_choose: Callable[[str, str], None],
                 previewer=None, running=False, lang="en", post=None):
        self.post = post  # thread-safe "run this on the Tk thread"
        self.current = dict(current)
        self.on_choose = on_choose
        self.previewer = previewer
        self.running = running
        self.voices: List[Voice] = []
        self.error: Optional[str] = None
        self.loading = True
        self.playing: Optional[str] = None
        self.focus_i = 0
        self.lang_var = tk.StringVar(value=lang)
        self.gender = "all"
        self.query = tk.StringVar()
        self._items = []
        self._width = 0
        sub = ("Your choice is saved on this PC and used from the next Start."
               if running else "Your choice is saved on this PC.")
        self.modal = Modal(master, "Select a voice", self._build, width=760, subtitle=sub,
                           on_close=self._closed)
        self._load(refresh=False)

    # ------------------------------------------------------------- build
    def _build(self, body: tk.Frame):
        bg = body.cget("bg")
        self.search = SearchField(body, self.query, "Search voices by name or accent")
        self.search.pack(fill="x")
        self.query.trace_add("write", lambda *a: self._refilter())

        row = tk.Frame(body, bg=bg)
        row.pack(fill="x", pady=(S(12), 0))
        self.chips = {}
        for value, text, _ in GENDERS:
            b = Button(row, text, variant="soft" if value == "all" else "secondary", size="sm",
                       command=lambda v=value: self._set_gender(v))
            b.pack(side="left", padx=(0, S(6)))
            self.chips[value] = b
        self.refresh_btn = IconButton(row, "refresh", "Load the voice list from Azure again",
                                      command=lambda: self._load(refresh=True), size=32)
        self.refresh_btn.pack(side="right")

        self.tabs = PillTabs(body, self._tab_options(), self.lang_var, command=self._refilter,
                             stretch=True, size="sm", label="Language")
        self.tabs.pack(fill="x", pady=(S(12), S(8)))

        self.banner = Banner(body, "", tone="danger")
        self.banner.pack(fill="x")

        top = body.winfo_toplevel()
        gh = max(S(200), min(S(420), top.winfo_height() - S(360)))
        self.grid = tk.Canvas(body, height=gh, bg=bg, highlightthickness=0, bd=0, takefocus=1)
        self.grid.pack(fill="both", expand=True, pady=(S(4), 0))
        self.grid.bind("<Configure>", self._on_grid_configure)
        self.grid.bind("<MouseWheel>",
                       lambda e: self.grid.yview_scroll(-1 if e.delta > 0 else 1, "units"))
        self.grid.configure(yscrollincrement=S(24))
        for key, d in (("<Left>", -1), ("<Right>", 1), ("<Up>", "up"), ("<Down>", "down")):
            self.grid.bind(key, lambda e, d=d: self._move(d))
        self.grid.bind("<Return>", lambda e: self._select_focused())
        self.grid.bind("<space>", lambda e: self._select_focused())
        self.grid.bind("<KeyPress-p>", lambda e: self._preview_focused())
        self.grid.bind("<FocusIn>", lambda e: self._draw())
        self.grid.bind("<FocusOut>", lambda e: self._draw())
        hint = ("Arrow keys move, Enter selects, P plays a preview." if not self.running else
                "Preview is off while translating, so the mic doesn't hear it.")
        tk.Label(body, text=hint, bg=bg, fg=C("text_3"), font=th.T.font(12),
                 anchor="w").pack(fill="x", pady=(S(8), 0))

    def _tab_options(self):
        out = []
        for code in config.PAIR:
            n = len(self._filtered(code)) if self.voices else 0
            label = config.LANGUAGES[code].name + (f" ({n})" if self.voices else "")
            out.append((code, label, None))
        return out

    # -------------------------------------------------------------- data
    def _load(self, refresh: bool):
        self.loading, self.error = True, None
        self._draw()

        def work():
            try:
                voices, err = get_voices(refresh=refresh), None
            except config.ConfigError as e:
                voices, err = [], f"{e} Voices can't be listed without a working key."
            except ConnectionError as e:
                voices, err = [], str(e)
            except Exception as e:  # never leave the modal spinning
                voices, err = [], f"Loading voices failed: {e}"
            if self.post is not None:
                self.post(self._loaded, voices, err)
        threading.Thread(target=work, daemon=True, name="voice-list").start()

    def _loaded(self, voices, err):
        if self.modal.closed:
            return
        self.loading = False
        self.voices = voices
        self.error = err
        self.banner.configure(text=err or "", tone="danger")
        self._refilter()

    def _filtered(self, lang) -> List[Voice]:
        q = self.query.get().strip().lower()
        out = []
        for v in self.voices:
            if v.lang != lang:
                continue
            if self.gender != "all" and v.gender != self.gender:
                continue
            if q and q not in f"{v.display} {v.name} {v.locale_name} {v.locale}".lower():
                continue
            out.append(v)
        return out

    def _set_gender(self, g):
        self.gender = g
        for value, b in self.chips.items():
            b.configure(variant="soft" if value == g else "secondary")
        self._refilter()

    def _refilter(self):
        self.tabs.options = self._tab_options()
        self.tabs.redraw()
        self._items = self._filtered(self.lang_var.get())
        self.focus_i = min(self.focus_i, max(0, len(self._items) - 1))
        self.grid.yview_moveto(0)
        self._draw()

    # ------------------------------------------------------------ grid
    def _cols(self, W):
        return 3 if W >= S(600) else 2 if W >= S(380) else 1

    def _on_grid_configure(self, e):
        if e.width != self._width:
            self._width = e.width
            self._draw()

    CARD_H = 68

    def _cell(self, i, W):
        cols = self._cols(W)
        gap = S(10)
        cw = (W - gap * (cols - 1)) // cols
        r, c = divmod(i, cols)
        return c * (cw + gap), r * (S(self.CARD_H) + gap), cw, S(self.CARD_H)

    def _draw(self):
        g = self.grid
        g.delete("all")
        W = g.winfo_width()
        H = g.winfo_height()
        if W < 20:
            return
        bg = g.cget("bg")
        if self.loading:
            g.create_text(W // 2, H // 2, text="Loading voices from Azure…", font=th.T.font(14, 500),
                          fill=C("text_2"))
            g.configure(scrollregion=(0, 0, W, H))
            return
        if not self._items:
            msg = "No voices match." if self.voices else "No voices loaded."
            g.create_text(W // 2, H // 2, text=msg, font=th.T.font(14, 500), fill=C("text_2"))
            g.configure(scrollregion=(0, 0, W, H))
            return
        lang = self.lang_var.get()
        focused = g.focus_get() is g
        last_y = 0
        for i, v in enumerate(self._items):
            x, y, w, h = self._cell(i, W)
            last_y = y + h
            selected = self.current.get(lang) == v.name
            fill = C("primary_10") if selected else C("bg")
            border = C("primary") if selected else C("border")
            tag = f"v{i}"
            draw_box(g, x, y, w, h, S(14), fill, bg, border, tags=(tag,))
            if focused and i == self.focus_i:
                g.create_rectangle(x + 1, y + 1, x + w - 2, y + h - 2, outline=C("focus"), width=2,
                                   tags=(tag,))
            # avatar
            ad = S(40)
            female = v.gender == "Female"
            av_fill = mix(C("primary") if female else C("accent_2"), fill, 0.16)
            av_fg = C("primary") if female else C("accent_2")
            ax, ay = x + S(12) + ad // 2, y + h // 2
            g.create_image(ax, ay, image=paint.circle(ad, av_fill, fill), tags=(tag,))
            g.create_text(ax, ay, text=v.initials, font=th.T.font(15, 700), fill=av_fg, tags=(tag,))
            # text
            tx = x + S(12) + ad + S(10)
            tw = w - (tx - x) - S(48)
            name_font = th.T.font(14, 600)
            g.create_text(tx, ay - S(9), text=ellipsize(v.display, name_font, tw), anchor="w",
                          font=name_font, fill=C("primary") if selected else C("text"), tags=(tag,))
            sub = f"{v.locale} · {v.gender}" if v.gender else v.locale
            if v.name == config.DEFAULT_VOICES.get(v.lang):
                sub += " · default"
            g.create_text(tx, ay + S(10), text=ellipsize(sub, th.T.font(12), tw), anchor="w",
                          font=th.T.font(12), fill=C("text_3"), tags=(tag,))
            g.tag_bind(tag, "<Button-1>", lambda e, i=i: self._select(i))
            g.tag_bind(tag, "<Enter>", lambda e: g.configure(cursor="hand2"))
            g.tag_bind(tag, "<Leave>", lambda e: g.configure(cursor=""))
            # preview button
            pd = S(30)
            px, py = x + w - S(12) - pd // 2, ay
            ptag = f"p{i}"
            can = self.previewer is not None and not self.running
            is_playing = self.playing == v.name
            pfill = C("primary") if is_playing else (C("bg") if can else mix(C("bg_muted"), fill, 0.5))
            g.create_image(px, py, image=paint.circle(pd, pfill, fill,
                                                      None if is_playing else C("border")),
                           tags=(ptag,))
            g.create_text(px + (0 if is_playing else S(1)), py,
                          text=th.T.icon("stop" if is_playing else "play"), font=th.T.icon_font(11),
                          fill=C("on_primary") if is_playing else (C("primary") if can else C("text_3")),
                          tags=(ptag,))
            g.tag_bind(ptag, "<Button-1>", lambda e, i=i: (self._preview(i), "break")[1])
            if selected:
                g.create_image(x + w - S(8), y + S(8), image=paint.circle(S(18), C("primary"), bg),
                               tags=(tag,))
                g.create_text(x + w - S(8), y + S(8), text=th.T.icon("check"),
                              font=th.T.icon_font(9), fill=C("on_primary"), tags=(tag,))
        g.configure(scrollregion=(0, 0, W, last_y + S(4)))

    # ---------------------------------------------------------- actions
    def _select(self, i):
        v = self._items[i]
        self.focus_i = i
        self.current[v.lang] = v.name
        self.on_choose(v.lang, v.name)
        self._draw()

    def _preview(self, i):
        if self.previewer is None or self.running:
            return
        v = self._items[i]
        if self.playing == v.name:
            self.previewer.stop()
            self.playing = None
        else:
            self.playing = v.name
            self.previewer.play(v)
        self._draw()

    def preview_state(self, name: Optional[str], error: Optional[str]):
        """Called on the Tk thread by the app when the previewer reports."""
        if self.modal.closed:
            return
        self.playing = name
        if error:
            self.banner.configure(text=error, tone="danger")
        self._draw()

    def _move(self, d):
        if not self._items:
            return "break"
        cols = self._cols(self.grid.winfo_width())
        step = {"up": -cols, "down": cols}.get(d, d)
        self.focus_i = max(0, min(len(self._items) - 1, self.focus_i + step))
        x, y, w, h = self._cell(self.focus_i, self.grid.winfo_width())
        total = float(self.grid.bbox("all")[3] if self.grid.bbox("all") else 1)
        top = self.grid.canvasy(0)
        if y < top or y + h > top + self.grid.winfo_height():
            self.grid.yview_moveto(max(0.0, y - S(8)) / total)
        self._draw()
        return "break"

    def _select_focused(self):
        if self._items:
            self._select(self.focus_i)
        return "break"

    def _preview_focused(self):
        if self._items:
            self._preview(self.focus_i)
        return "break"

    def _closed(self):
        if self.previewer is not None:
            self.previewer.stop()
