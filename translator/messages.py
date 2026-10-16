"""The conversation: a scrolling list of message cards (MessageCard), the live
(interim) card, system notes, and the "jump to latest" control.

Everything is drawn on one canvas, so long meetings stay cheap: new cards
are appended; a width change re-lays out (debounced).

Long sessions (lectures, hours of meeting): every message is kept (as small
dicts) but only the newest MAX_RENDERED are drawn. Older ones are dropped
from the canvas in batches while the reader follows the newest line, and a
"Show earlier" row at the top brings them back on request. The transcript
file always has everything.
"""
import tkinter as tk
from datetime import datetime
from typing import Callable, Dict, List, Optional

from . import paint
from . import theme as th
from .widgets import Button, C, S, draw_box, ellipsize, surface_of

BADGE = {"en": "EN", "th": "TH"}
MAX_RENDERED = 200   # cards + notes drawn at once
EVICT_BATCH = 40     # drop this many at a time (one re-layout per batch)


class MessageList(tk.Frame):
    def __init__(self, master, on_replay: Callable[[int], object]):
        bg = surface_of(master)
        super().__init__(master, bg=bg)
        self.on_replay = on_replay
        self.cv = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0, takefocus=1,
                            yscrollincrement=S(24))
        self.cv.pack(fill="both", expand=True)
        self._bg = bg
        self.items: List[dict] = []
        self._first = 0  # index of the first rendered item
        self.interim: Optional[dict] = None
        self.playing: set = set()
        self.large = False
        self.two_sided = False
        self.stick = True  # follow the newest message
        self.unseen = 0
        self._y = 0
        self._width = 0
        self._relayout_job = None
        self._card_ids: Dict[int, dict] = {}
        self.jump = Button(self, "Jump to latest", icon="arrow_down", variant="primary", size="sm",
                           command=self.scroll_to_end)
        self.cv.bind("<Configure>", self._on_configure)
        for w in (self.cv,):
            w.bind("<MouseWheel>", self._on_wheel)
            w.bind("<Prior>", lambda e: self._scroll(-1, "pages"))
            w.bind("<Next>", lambda e: self._scroll(1, "pages"))
            w.bind("<Up>", lambda e: self._scroll(-1, "units"))
            w.bind("<Down>", lambda e: self._scroll(1, "units"))
            w.bind("<End>", lambda e: self.scroll_to_end())
            w.bind("<FocusIn>", lambda e: self._focus_ring(True))
            w.bind("<FocusOut>", lambda e: self._focus_ring(False))
            w.bind("<Button-1>", lambda e: self.cv.focus_set(), add="+")
        self._menu = tk.Menu(self, tearoff=0)
        # Bound once per tag *type*. Binding per card would register a new Tcl
        # callback for every card ever drawn (Tkinter never frees them when
        # items are deleted), which grows without bound in long sessions.
        cv = self.cv
        cv.tag_bind("replay", "<Enter>", lambda e: self._replay_hover(True))
        cv.tag_bind("replay", "<Leave>", lambda e: self._replay_hover(False))
        cv.tag_bind("replay", "<Button-1>", self._replay_click)
        cv.tag_bind("cardbody", "<Button-3>", self._card_menu)
        cv.tag_bind("earlier_link", "<Button-1>", lambda e: self.show_earlier())
        cv.tag_bind("earlier_link", "<Enter>", lambda e: cv.configure(cursor="hand2"))
        cv.tag_bind("earlier_link", "<Leave>", lambda e: cv.configure(cursor=""))
        self._hover_id = None
        self.bind("<Destroy>", self._on_destroy, add="+")
        self._draw_empty()

    def _on_destroy(self, e):
        if e.widget is self and self._relayout_job is not None:
            try:
                self.after_cancel(self._relayout_job)
            except tk.TclError:
                pass
            self._relayout_job = None

    # ------------------------------------------------------------ public API
    def add_message(self, seg_id: int, source_text: str, translated: str, src: str, tgt: str,
                    ts: float):
        it = {"kind": "msg", "id": seg_id, "src_text": source_text, "tgt_text": translated,
              "src": src, "tgt": tgt, "ts": ts}
        self.items.append(it)
        self.interim = None
        self._append(it)

    def add_note(self, text: str):
        it = {"kind": "note", "text": text}
        self.items.append(it)
        self._append(it)

    def set_interim(self, source_text: str, translated: str, src: str, tgt: str):
        self.interim = {"kind": "live", "src_text": source_text, "tgt_text": translated or "…",
                        "src": src, "tgt": tgt}
        self._draw_interim()
        self._after_content()

    def clear_interim(self):
        if self.interim is not None:
            self.interim = None
            self._draw_interim()

    def set_playing(self, ids):
        ids = set(ids or ())
        if ids == self.playing:
            return
        changed = ids ^ self.playing
        self.playing = ids
        for seg_id in changed:
            it = self._card_ids.get(seg_id)
            if it is not None:
                self._redraw_card(it)

    def set_large(self, on: bool):
        if on != self.large:
            self.large = on
            self.relayout()

    def set_two_sided(self, on: bool):
        if on != self.two_sided:
            self.two_sided = on
            self.relayout()

    def scroll_to_end(self):
        self.cv.yview_moveto(1.0)
        self.stick = True
        self.unseen = 0
        self._update_jump()

    def replay_bbox(self, seg_id: int):
        """Canvas bbox of a card's ▶ button (tests, keyboard focus)."""
        return self.cv.bbox(f"rp{seg_id}")

    # ------------------------------------------------------------- layout
    def _on_configure(self, e):
        if not self.items and self.interim is None:
            self._draw_empty()
        if e.width != self._width:
            self._width = e.width
            if self._relayout_job is not None:
                self.after_cancel(self._relayout_job)
            self._relayout_job = self.after(60, self.relayout)
        self._place_jump()

    @property
    def rendered(self) -> int:
        return len(self.items) - self._first

    def show_earlier(self):
        self._first = max(0, self._first - MAX_RENDERED)
        self.stick = False
        self.relayout()
        self.cv.yview_moveto(0.0)
        self._update_jump()

    def relayout(self):
        self._relayout_job = None
        self.cv.delete("all")
        self._card_ids.clear()
        self._y = S(8)
        if not self.items and self.interim is None:
            self._draw_empty()
            return
        if self._first > 0:
            self._draw_earlier()
        for it in self.items[self._first:]:
            self._draw_item(it)
        self._draw_interim()
        if self.stick:
            self.cv.yview_moveto(1.0)

    def _W(self):
        return max(S(200), self.cv.winfo_width() or self._width or S(500))

    def _append(self, it):
        self.cv.delete("empty")
        if not self.items[:-1] and self.interim is None:
            self._y = S(8)
        self.cv.delete("live")
        self._draw_item(it)
        self._draw_interim()
        if it["kind"] == "msg" and not self.stick:
            self.unseen += 1
        if self.stick and self.rendered > MAX_RENDERED + EVICT_BATCH:
            # Only while following the newest line, so a reader isn't moved.
            self._first = len(self.items) - MAX_RENDERED
            self.relayout()
        self._after_content()

    def _after_content(self):
        self._update_region()
        if self.stick:
            self.cv.yview_moveto(1.0)
        self._update_jump()

    def _update_region(self):
        self.cv.configure(scrollregion=(0, 0, self._W(), self._y + S(8)
                                        + (self._live_h if self.interim else 0)))

    def _draw_empty(self):
        self.cv.delete("empty")
        W = self._W()
        H = max(S(60), self.cv.winfo_height() or S(200))
        d = S(56)
        show_icon = H >= S(150)
        block = (d + S(14) if show_icon else 0) + S(44)
        y = max(S(4), (H - block) // 2)
        if show_icon:
            self._empty_img = paint.circle(d, C("primary_10"), self._bg)
            self.cv.create_image(W // 2, y + d // 2, image=self._empty_img, tags="empty")
            self.cv.create_text(W // 2, y + d // 2, text=th.T.icon("translate"),
                                font=th.T.icon_font(24), fill=C("primary"), tags="empty")
            y += d + S(14)
        self.cv.create_text(W // 2, y, text="Translations appear here", anchor="n",
                            font=th.T.font(15, 600), fill=C("text"), tags="empty")
        self.cv.create_text(W // 2, y + S(24), text="Press the mic, then speak English or Thai.",
                            anchor="n", font=th.T.font(13), fill=C("text_3"), tags="empty",
                            width=W - S(32), justify="center")
        self.cv.configure(scrollregion=(0, 0, W, H))

    def _draw_earlier(self):
        W = self._W()
        n = self._first
        t = self.cv.create_text(W // 2, self._y + S(4), anchor="n", tags=("earlier",),
                                text=f"{n} earlier message{'s' if n != 1 else ''} not shown "
                                     "(all are in the transcript)",
                                font=th.T.font(12, 500), fill=C("text_3"))
        y1 = self.cv.bbox(t)[3]
        link = self.cv.create_text(W // 2, y1 + S(4), anchor="n", text="Show earlier",
                                   font=th.T.font(13, 600), fill=C("primary"),
                                   tags=("earlier", "earlier_link"))
        self._y = self.cv.bbox(link)[3] + S(14)

    def _draw_item(self, it):
        if it["kind"] == "note":
            self._draw_note(it)
        else:
            it["y"] = self._y
            h = self._draw_card(it, self._y)
            it["h"] = h
            self._card_ids[it["id"]] = it
            self._y += h + S(10)

    def _draw_note(self, it):
        W = self._W()
        font = th.T.font(12, 500, th.lang_of(it["text"]))
        t = self.cv.create_text(W // 2, self._y + S(4), text=it["text"], anchor="n", font=font,
                                fill=C("text_3"), width=W - S(48), justify="center")
        x0, y0, x1, y1 = self.cv.bbox(t)
        self._y = y1 + S(12)

    # ---------------------------------------------------------- the card
    def _geometry(self, it):
        W = self._W()
        if self.two_sided:
            cw = int(W * 0.86)
            right = it["src"] == "th"
            x = W - cw - S(2) if right else S(2)
        else:
            cw, x, right = W - S(4), S(2), False
        return x, cw, right

    def _colors(self, it, live=False):
        playing = it.get("id") in self.playing
        if live:
            return C("bg"), C("border"), C("text_3"), C("text_2")
        if playing:
            return C("primary_10"), C("primary_30"), C("text_2"), C("text")
        return C("bg_subtle"), C("border"), C("text_2"), C("text")

    def _draw_card(self, it, y, live=False):
        cv = self.cv
        tag = "live" if live else (f"card{it['id']}", "cardbody")
        x, cw, right = self._geometry(it)
        fill, border, fg_src, fg_tgt = self._colors(it, live)
        pad = S(16)
        inner = cw - 2 * pad
        # header: badge, time, replay
        accent = C("accent_2") if (self.two_sided and it["src"] == "th") else C("primary")
        badge = f"{BADGE.get(it['src'], it['src'].upper())} → {BADGE.get(it['tgt'], it['tgt'].upper())}"
        bfont = th.T.font(11, 700)
        ids = []
        hy = y + pad
        bw, bh = bfont.measure(badge) + S(14), S(22)
        badge_fill = th.mix(accent, fill, 0.12)
        tag = tag if isinstance(tag, tuple) else (tag,)
        ids.append(cv.create_image(x + pad, hy, anchor="nw", tags=tag + ("fg",),
                                   image=paint.pill(bw, bh, badge_fill, fill)))
        ids.append(cv.create_text(x + pad + bw // 2, hy + bh // 2, text=badge, font=bfont,
                                  fill=accent, tags=tag + ("fg",)))
        if live:
            meta = "● Live"
            meta_fill = C("danger")
        else:
            meta = datetime.fromtimestamp(it["ts"]).strftime("%H:%M:%S")
            meta_fill = C("text_3")
            if it["id"] in self.playing:
                meta += "  ·  Speaking"
                meta_fill = C("primary")
        ids.append(cv.create_text(x + pad + bw + S(10), hy + bh // 2, text=meta, anchor="w",
                                  font=th.T.font(12, 500), fill=meta_fill, tags=tag + ("fg",)))
        if not live:
            self._draw_replay(it, x + cw - pad - S(14), hy + bh // 2, fill, tag)
        ty = hy + bh + S(10)
        sfont = th.T.font(15 if self.large else 13, 400, it["src"])
        t1 = cv.create_text(x + pad, ty, text=it["src_text"], anchor="nw", font=sfont,
                            fill=fg_src, width=inner, tags=tag + ("fg",))
        ty = cv.bbox(t1)[3] + S(6)
        tfont = th.T.font(28 if self.large else 17, 500, it["tgt"])
        t2 = cv.create_text(x + pad, ty, text=it["tgt_text"], anchor="nw", font=tfont,
                            fill=fg_tgt if not live else C("text_2"), width=inner, tags=tag + ("fg",))
        bottom = cv.bbox(t2)[3] + pad
        h = bottom - y
        box = draw_box(cv, x, y, cw, h, S(th.RADIUS_CARD), fill, self._bg, border,
                       tags=tag + ("box",))
        for i in box:
            cv.tag_lower(i, ids[0])
        return h

    def _draw_replay(self, it, cx, cy, card_fill, tag):
        cv = self.cv
        seg_id = it["id"]
        rtag = f"rp{seg_id}"
        playing = seg_id in self.playing
        d = S(30)
        img_n = paint.circle(d, C("primary") if playing else C("bg"), card_fill,
                             None if playing else C("border"))
        img_h = paint.circle(d, C("primary_hover") if playing else C("primary_10"), card_fill,
                             None if playing else C("primary_30"))
        ci = cv.create_image(cx, cy, image=img_n, tags=tag + (rtag, "replay", "fg"))
        cv.create_text(cx + (0 if playing else S(1)), cy,
                       text=th.T.icon("speaker" if playing else "play"),
                       font=th.T.icon_font(12),
                       fill=C("on_primary") if playing else C("primary"),
                       tags=tag + (rtag, "replay", "fg"))
        it["rp"] = (ci, img_n, img_h)

    def _id_under_pointer(self, prefix):
        for t in self.cv.gettags("current"):
            if t.startswith(prefix) and t[len(prefix):].isdigit():
                return int(t[len(prefix):])
        return None

    def _replay_hover(self, on):
        seg_id = self._id_under_pointer("rp") if on else self._hover_id
        self._hover_id = seg_id if on else None
        it = self._card_ids.get(seg_id) if seg_id is not None else None
        if it is not None and "rp" in it:
            ci, img_n, img_h = it["rp"]
            self.cv.itemconfigure(ci, image=img_h if on else img_n)
        self.cv.configure(cursor="hand2" if on else "")

    def _replay_click(self, e):
        seg_id = self._id_under_pointer("rp")
        if seg_id is not None:
            self.on_replay(seg_id)
        return "break"

    def _card_menu(self, e):
        seg_id = self._id_under_pointer("card")
        it = self._card_ids.get(seg_id) if seg_id is not None else None
        if it is not None:
            self._context(e, it)

    def _redraw_card(self, it):
        """Repaint one card in place (playing highlight); its height is unchanged."""
        self.cv.delete(f"card{it['id']}")
        self._draw_card(it, it["y"])

    # -------------------------------------------------------- live card
    _live_h = 0

    def _draw_interim(self):
        self.cv.delete("live")
        self._live_h = 0
        if self.interim is not None:
            self.cv.delete("empty")
            self._live_h = self._draw_card(self.interim, self._y, live=True)
        self._update_region()

    # ------------------------------------------------------------ scrolling
    def _on_wheel(self, e):
        self._scroll(-1 if e.delta > 0 else 1, "units", n=max(1, abs(e.delta) // 120) * 2)

    def _scroll(self, d, what, n=1):
        self.cv.yview_scroll(d * n, what)
        self.stick = self.cv.yview()[1] >= 0.999
        if self.stick:
            self.unseen = 0
        self._update_jump()
        return "break"

    def _update_jump(self):
        if self.stick or not self.items:
            self.jump.place_forget()
        else:
            self.jump.configure(text=f"{self.unseen} new · Jump to latest" if self.unseen
                                else "Jump to latest")
            self._place_jump()

    def _place_jump(self):
        if not self.stick and self.items:
            self.jump.place(relx=0.5, rely=1.0, y=-S(10), anchor="s")

    def _focus_ring(self, on):
        self.cv.configure(highlightthickness=0)

    def _context(self, e, it):
        m = self._menu
        m.delete(0, "end")
        m.add_command(label="Copy translation", command=lambda: self._copy(it["tgt_text"]))
        m.add_command(label="Copy original", command=lambda: self._copy(it["src_text"]))
        m.add_command(label="Replay", command=lambda: self.on_replay(it["id"]))
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()

    def _copy(self, text):
        self.clipboard_clear()
        self.clipboard_append(text)
