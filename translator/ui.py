"""Tkinter UI. All SDK/audio callbacks run on other threads and reach Tk only
through `post()`, which the Tk main loop drains every 50 ms.

Look and feel: design tokens in theme.py, components in widgets.py,
messages.py and voice_picker.py. This module wires them to the session
logic (start/stop, recognition, TTS, onsite turn-taking)."""
import logging
import queue
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Optional

import pyaudiowpatch as pyaudio

from . import config, paint, theme, voices, winmic
from .audio import AudioCapture, AudioPump, Device, Player, list_devices
from .latency import LatencyLog
from .messages import MessageList
from .onsite import EchoFilter, Phase, TurnTaker, phase_of
from .recognition import RecognitionController, Segment
from .session import Cancelled, Session, State, StateMachine
from .subtitle_view import SubtitleView
from .transcript import Transcript
from .tts import POLICY_CATCH_UP, POLICY_IN_ORDER, TtsQueue
from .usage import Usage
from .voice_picker import VoicePicker
from .widgets import (C, S, Banner, Button, Chip, IconButton, Modal, Overlay, PillSelect, PillTabs,
                      Popover, RecordButton, SettingRow, Toggle)

# Status line colour per recognizer/app state (theme token names).
STATUS_TOKENS = {"idle": "text_2", "connecting": "primary", "listening": "success",
                 "reconnecting": "warning", "error": "danger_active", "stopping": "text_2"}

SIDE_LABEL = {"en": "English speaker · ผู้พูดอังกฤษ", "th": "ผู้พูดภาษาไทย · Thai speaker"}
# Onsite floor: Auto (detected language decides; mic open for both) or a
# locked side. "auto" is the Tk-variable value for TurnTaker.side None.
FLOOR_TABS = (("auto", "Auto · อัตโนมัติ", "people"), ("en", "English · อังกฤษ", "mic"),
              ("th", "ไทย · Thai", "mic"))
# Onsite banner per phase: (colour token, text). Bilingual: both people read it.
PHASE_BANNER = {
    Phase.LISTENING: ("success", {"en": "English speaker: please speak   ·   ผู้พูดภาษาอังกฤษ พูดได้เลย",
                                  "th": "ผู้พูดภาษาไทย พูดได้เลย   ·   Thai speaker: please speak",
                                  "auto": "Speak English or Thai   ·   พูดภาษาไทยหรือภาษาอังกฤษได้เลย"}),
    Phase.SWITCHING: ("primary", "Switching turn… you can start speaking   ·   กำลังสลับผู้พูด… เริ่มพูดได้"),
    Phase.TRANSLATING: ("warning", "Translating   ·   กำลังแปล"),
    Phase.SPEAKING: ("danger_active", "Reading the translation: please wait   ·   กำลังอ่านคำแปล กรุณารอ"),
    Phase.TAIL: ("danger_active", "Reading the translation: please wait   ·   กำลังอ่านคำแปล กรุณารอ"),
}
PHASE_IDLE = "Press the mic, then just talk: the language is detected   ·   กดไมค์ แล้วพูดได้เลย"
# Latency rows kept in memory per session (the log file has them all).
LATENCY_ROWS_KEPT = 500


def phase_color(phase: Phase) -> str:
    return C(PHASE_BANNER[phase][0])


# Language row: the source picker includes auto-detect; the target follows.
SRC_LABELS = {config.MODE_AUTO: "Auto-detect (beta)", "en": "English", "th": "Thai"}
TGT_AUTO_LABEL = "The other language"
SOURCE_TABS = (("loopback", "System audio", "headphones"), ("mic", "Microphone", "mic"))


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.pa = pyaudio.PyAudio()
        self.usage = Usage()
        self._ui_q: "queue.Queue" = queue.Queue()
        self.sm = StateMachine()
        self._error_shown = False
        # The live session owns capture/pump/tts/controller; see session.py.
        self.session: Optional[Session] = None
        self.transcript: Optional[Transcript] = None  # kept after Stop for export
        # Plain mirrors of Tk vars, readable from worker threads
        self._pause_while_speaking = False
        self._last_delay: Optional[dict] = None  # latest latency row, for the status bar
        # Onsite (fixed for a session at Start)
        self.turn = TurnTaker()
        self.echo: Optional[EchoFilter] = None
        self._onsite_live = False
        # Every final sentence shown in the conversation, by id, for replay.
        self._segs: dict = {}
        self._last_seg_id: Optional[int] = None
        self._last_usage_save = time.monotonic()
        # Live-state inputs for the record button (written by other threads)
        self._level = 0.0
        self._playing_ids: tuple = ()
        self._status_state = "idle"
        self._interim_at = 0.0
        self._voice_choice = voices.chosen()
        self._picker: Optional[VoicePicker] = None
        self.subtitle_view: Optional[SubtitleView] = None
        self._options_open_before = True

        self._build()
        self.refresh_devices()
        self._apply_setting()
        self._update_usage_label()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(50, self._drain)
        root.after(1000, self._tick)
        root.after(100, self._onsite_tick)
        root.bind_all("<F1>", lambda e: self._give_floor("en"))
        root.bind_all("<F2>", lambda e: self._give_floor("th"))
        root.bind_all("<Escape>", self._on_escape)
        root.bind_all("<F3>", lambda e: self._replay_last())
        root.bind_all("<F4>", lambda e: self._give_floor("auto"))
        root.bind_all("<Control-m>", lambda e: self._toggle_mute())
        root.bind_all("<F11>", lambda e: self._open_subtitles())

    # ---------------------------------------------------------------- layout
    def _build(self):
        r = self.root
        r.title("Meeting Translator: English ⇄ Thai")
        sw, sh = r.winfo_screenwidth(), r.winfo_screenheight()
        w, h = min(S(860), sw - S(40)), min(S(960), sh - S(140))  # clear of the taskbar
        r.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{S(20)}")
        r.minsize(S(360), S(560))
        r.configure(bg=C("page_a"))
        theme.dark_title_bar(r, theme.T.dark)

        # Page: gradient + card shadows are one image; frames sit on top.
        self.page = tk.Canvas(r, highlightthickness=0, bd=0, bg=C("page_a"))
        self.page.place(x=0, y=0, relwidth=1, relheight=1)
        self._page_img = self.page.create_image(0, 0, anchor="nw")
        self.nav = tk.Frame(self.page, bg=C("bg"))
        self.card = tk.Frame(self.page, bg=C("bg"))
        self._nav_win = self.page.create_window(0, 0, anchor="nw", window=self.nav)
        self._card_win = self.page.create_window(0, 0, anchor="nw", window=self.card)
        self._page_job = None
        self._page_size = (0, 0)
        self.page.bind("<Configure>", self._on_page_resize)

        self._build_nav(self.nav)
        self._build_card(self.card)

    def _build_nav(self, nav):
        bg = C("bg")
        left = tk.Frame(nav, bg=bg)
        left.pack(side="left", padx=(S(12), 0), pady=S(8))
        logo = tk.Canvas(left, width=S(36), height=S(36), bg=bg, highlightthickness=0, bd=0)
        logo.pack(side="left")
        logo.create_image(S(18), S(18), image=paint.circle(S(36), C("primary_10"), bg))
        logo.create_text(S(18), S(18), text=theme.T.icon("translate"),
                         font=theme.T.icon_font(16), fill=C("primary"))
        self.nav_title = tk.Label(left, text="Meeting Translator", bg=bg, fg=C("text"),
                                  font=theme.T.font(16, 700))
        self.nav_title.pack(side="left", padx=(S(10), 0))
        self.nav_pair = Chip(left, "EN ⇄ TH", C("bg_muted"), C("text_2"), fpx=12)
        self.nav_pair.pack(side="left", padx=(S(10), 0))

        right = tk.Frame(nav, bg=bg)
        right.pack(side="right", padx=(0, S(8)), pady=S(6))
        self.large_var = tk.BooleanVar(value=bool(voices.load_settings().get("large_text")))
        self.large_btn = IconButton(right, "font", "Large text (for showing the screen to the "
                                    "other person)", command=self._toggle_large, size=38,
                                    toggled=self.large_var.get())
        self.export_btn = IconButton(right, "download", "Export transcript (.txt or .srt)",
                                     command=self._export_menu, size=38)
        self.settings_btn = IconButton(right, "settings", "Settings, shortcuts and usage",
                                       command=self._open_settings, size=38)
        self.subs_btn = IconButton(right, "fullscreen", "Subtitle window for a projector (F11)",
                                   command=self._open_subtitles, size=38)
        for w in (self.large_btn, self.subs_btn, self.export_btn, self.settings_btn):
            w.pack(side="left", padx=S(1))

    def _build_card(self, card):
        bg = C("bg")
        col = self.col = tk.Frame(card, bg=bg)
        col.pack(fill="both", expand=True, padx=S(32), pady=(S(24), S(12)))
        col.columnconfigure(0, weight=1)

        head = self.head = tk.Frame(col, bg=bg)
        head.grid(row=0, column=0, sticky="we")
        tk.Label(head, text="Live translation", bg=bg, fg=C("text"), font=theme.T.font(28, 500),
                 anchor="w").pack(fill="x")
        self.subtitle = tk.Label(head, text="Speak English or Thai and hear the other language, live.",
                                 bg=bg, fg=C("text_2"), font=theme.T.font(14), anchor="w",
                                 justify="left")
        self.subtitle.pack(fill="x", pady=(S(2), 0))

        # Setting: meeting / onsite
        self.setting_var = tk.StringVar(value="meeting")
        self.setting_tabs = PillTabs(col, (("meeting", "Meeting", "headphones"),
                                           ("onsite", "Onsite", "people")),
                                     self.setting_var, command=self._apply_setting, stretch=True,
                                     label="Setting")
        self.setting_tabs.grid(row=1, column=0, sticky="we", pady=(S(18), 0))
        self.rb_meeting = self.setting_tabs.option("meeting")
        self.rb_onsite = self.setting_tabs.option("onsite")
        self.setting_hint = tk.Label(col, text="", bg=bg, fg=C("text_3"),
                                     font=theme.T.font(12), anchor="w", justify="left")
        self.setting_hint.grid(row=2, column=0, sticky="we", pady=(S(4), 0))

        # Language row (meeting) / floor (onsite) share row 3.
        self.mode_var = tk.StringVar(value=config.DEFAULT_MODE)
        self.lang_row = tk.Frame(col, bg=bg)
        self.lang_row.grid(row=3, column=0, sticky="we", pady=(S(10), 0))
        self.lang_row.columnconfigure(0, weight=1, uniform="lang")
        self.lang_row.columnconfigure(2, weight=1, uniform="lang")
        self.src_select = PillSelect(self.lang_row, list(SRC_LABELS.values()), icon="globe",
                                     command=self._on_src_pick, label="Spoken language")
        self.src_select.grid(row=0, column=0, sticky="we")
        self.swap_btn = IconButton(self.lang_row, "swap", "Swap languages", command=self._swap,
                                   size=40, variant="secondary")
        self.swap_btn.grid(row=0, column=1, padx=S(6))
        self.tgt_select = PillSelect(self.lang_row, [config.LANGUAGES[c].name for c in config.PAIR],
                                     icon="speaker", command=self._on_tgt_pick,
                                     label="Translate into")
        self.tgt_select.grid(row=0, column=2, sticky="we")
        self.mode_rbs = [self.src_select, self.swap_btn, self.tgt_select]
        self.mode_var.trace_add("write", lambda *a: self._sync_lang_row())

        self.turn_frame = tk.Frame(col, bg=bg)
        self._build_turn_panel(self.turn_frame)

        self.warn_label = Banner(col, "", tone="warning")
        self.warn_label.grid(row=4, column=0, sticky="we", pady=(S(10), 0))

        self._build_options(col)

        self.messages = MessageList(col, on_replay=self._replay)
        self.messages.grid(row=6, column=0, sticky="nsew", pady=(S(12), 0))
        self._head_hidden = False
        self._fit_job = None
        self.messages.bind("<Configure>", lambda e: self._schedule_fit(), add="+")
        col.rowconfigure(6, weight=1)
        self.messages.set_large(self.large_var.get())

        rec = tk.Frame(col, bg=bg)
        rec.grid(row=7, column=0, sticky="we", pady=(S(6), 0))
        row = tk.Frame(rec, bg=bg)
        row.pack()
        side_l = tk.Frame(row, bg=bg, width=S(96))
        side_l.pack(side="left", fill="y")
        self.mute_btn = IconButton(side_l, "speaker", "Mute the spoken translation (Ctrl+M)",
                                   command=self._toggle_mute, size=48, variant="secondary")
        self.mute_btn.pack(side="bottom", pady=(0, S(4)))
        self.mute_caption = tk.Label(side_l, text="Voice on", bg=bg, fg=C("text_3"),
                                     font=theme.T.font(11, 600))
        self.mute_caption.pack(side="bottom")
        self.record_btn = RecordButton(row, command=self._on_record)
        self.record_btn.pack(side="left")
        side_r = tk.Frame(row, bg=bg, width=S(96))
        side_r.pack(side="left", fill="y")
        self.subs_btn2 = IconButton(side_r, "fullscreen", "Subtitle window for a projector (F11)",
                                    command=self._open_subtitles, size=48, variant="secondary")
        self.subs_btn2.pack(side="bottom", pady=(0, S(4)))
        tk.Label(side_r, text="Subtitles", bg=bg, fg=C("text_3"),
                 font=theme.T.font(11, 600)).pack(side="bottom")
        self.status_label = tk.Label(rec, text="Ready", bg=bg, fg=C("text_2"),
                                     font=theme.T.font(13, 500), justify="center")
        self.status_label.pack(fill="x")
        self.queue_label = tk.Label(rec, text="Voice queue: 0", bg=bg, fg=C("text_3"),
                                    font=theme.T.font(12), justify="center")
        self.queue_label.pack(fill="x", pady=(S(2), 0))
        self.usage_label = tk.Label(rec, text="", bg=bg, fg=C("text_3"), font=theme.T.font(11),
                                    justify="center")
        self.usage_label.pack(fill="x", pady=(S(2), 0))
        col.bind("<Configure>", self._on_col_resize)
        self._sync_lang_row()
        # Collapsed by default so the conversation and the mic always fit;
        # the summary line shows the current choices.
        self._set_options_open(False)
        self._options_open_before = False

    def _build_options(self, col):
        bg = C("bg")
        box = self.options = tk.Frame(col, bg=bg)
        box.grid(row=5, column=0, sticky="we", pady=(S(8), 0))
        box.columnconfigure(0, weight=1)
        self.options_btn = Button(box, "Audio and voice", icon="settings",
                                  icon_right="chevron_up", variant="ghost", size="sm",
                                  command=self._toggle_options)
        self.options_btn.grid(row=0, column=0, sticky="w")
        self.options_summary = tk.Label(box, text="", bg=bg, fg=C("text_3"),
                                        font=theme.T.font(12), anchor="e")
        self.options_summary.grid(row=0, column=1, sticky="e")
        body = self.options_body = tk.Frame(box, bg=bg)
        body.grid(row=1, column=0, columnspan=2, sticky="we")
        body.columnconfigure(0, weight=1)
        self.options_open = True

        self.source_var = tk.StringVar(value="loopback")
        row = SettingRow(body, "Listen to", info="System audio captures what your PC plays "
                         "(Teams, Zoom, Meet). Microphone listens to the room.")
        row.grid(row=0, column=0, sticky="we")
        self.source_tabs = PillTabs(row.right, SOURCE_TABS, self.source_var,
                                    command=self._on_source_change, size="sm", label="Listen to")
        self.source_tabs.pack()
        self.rb_loop = self.source_tabs.option("loopback")
        self.rb_mic = self.source_tabs.option("mic")

        dev = tk.Frame(body, bg=bg)
        dev.grid(row=1, column=0, sticky="we", pady=(S(4), 0))
        dev.columnconfigure(0, weight=1, uniform="dev")
        dev.columnconfigure(1, weight=1, uniform="dev")
        self.in_combo = PillSelect(dev, icon="mic", label="Input device",
                                   command=lambda v: self._update_feedback_warning())
        self.in_combo.grid(row=0, column=0, sticky="we", padx=(0, S(4)))
        self.out_combo = PillSelect(dev, icon="speaker", label="Voice output device",
                                    command=lambda v: self._update_feedback_warning())
        self.out_combo.grid(row=0, column=1, sticky="we", padx=(S(4), 0))
        self.refresh_btn = IconButton(dev, "refresh", "Refresh devices", command=self.refresh_devices,
                                      size=40, variant="secondary")
        self.refresh_btn.grid(row=0, column=2, padx=(S(6), 0))

        self.mute_var = tk.BooleanVar(value=False)
        row = SettingRow(body, "Mute voice", sub="Captions only. Nothing is spoken.")
        row.grid(row=2, column=0, sticky="we", pady=(S(4), 0))
        Toggle(row.right, self.mute_var, command=self._on_mute, label="Mute voice").pack()
        self.mute_var.trace_add("write", lambda *a: self._show_mute())

        self.pause_var = tk.BooleanVar(value=False)
        row = SettingRow(body, "Pause listening while the voice speaks",
                         info="Stops the app hearing and re-translating its own voice when it "
                              "plays where it is listening. Always on in Onsite.")
        row.grid(row=3, column=0, sticky="we")
        self.pause_cb = Toggle(row.right, self.pause_var, command=self._on_pause_opt,
                               label="Pause listening while the voice speaks")
        self.pause_cb.pack()

        row = SettingRow(body, "Select a voice")
        row.grid(row=4, column=0, sticky="we")
        self.voice_btn = Button(row.right, self._voice_summary(), icon="voice",
                                icon_right="chevron_down", variant="soft", size="sm",
                                command=self._open_voice_picker, max_text_w=220,
                                tooltip="Choose the English and Thai voices")
        self.voice_btn.pack()

    def _build_turn_panel(self, f):
        """Onsite only: who has the floor, and what the device is doing."""
        bg = C("bg")
        f.columnconfigure(0, weight=1)
        self.floor_var = tk.StringVar(value=self.turn.side)
        self.floor_tabs = PillTabs(f, FLOOR_TABS, self.floor_var, stretch=True,
                                   command=lambda: self._give_floor(self.floor_var.get()),
                                   label="Who is speaking (F1 / F2)")
        self.floor_tabs.grid(row=0, column=0, sticky="we")
        self.phase_label = Banner(f, "", solid=True, fpx=16, weight=600)
        self.phase_label.grid(row=1, column=0, sticky="we", pady=(S(8), 0))
        tools = tk.Frame(f, bg=bg)
        tools.grid(row=2, column=0, sticky="we", pady=(S(6), 0))
        Button(tools, "Replay  F3", icon="replay", variant="ghost", size="sm",
               command=self._replay_last, tooltip="Replay the last translation (F3)",
               lang="en").pack(side="left")
        self.skip_btn = Button(tools, "Skip  Esc", icon="skip", variant="ghost", size="sm",
                               command=self._skip_voice, tooltip="Skip the voice (Esc)", lang="en")
        self.skip_btn.pack(side="left", padx=(S(4), 0))
        self.auto_var = tk.BooleanVar(value=config.ONSITE_AUTO_ALTERNATE)
        self.auto_cb = Toggle(tools, self.auto_var, command=self._on_auto_alternate,
                              label="Auto-switch turns (when a side is chosen)")
        self.auto_cb.pack(side="right")
        tk.Label(tools, text="Auto-switch turns", bg=bg, fg=C("text_2"),
                 font=theme.T.font(13, 500)).pack(side="right", padx=(0, S(4)))
        self._show_floor()

    # ------------------------------------------------------------ page layout
    def _on_page_resize(self, e):
        W, H = e.width, e.height
        narrow = W < S(640)
        g = S(16) if narrow else S(24)
        cw = int(min(W - 2 * g, S(720)))
        x = (W - cw) // 2
        nav_h = S(60)
        card_y = g + nav_h + S(12)
        card_h = max(S(200), H - card_y - g)
        inset = S(5)
        self._layout = (W, H, x, g, cw, nav_h, card_y, card_h)
        self.page.coords(self._nav_win, x + inset, g + inset)
        self.page.itemconfigure(self._nav_win, width=cw - 2 * inset, height=nav_h - 2 * inset)
        self.page.coords(self._card_win, x + inset, card_y + inset)
        self.page.itemconfigure(self._card_win, width=cw - 2 * inset, height=card_h - 2 * inset)
        pad = S(16) if narrow else S(32)
        self.col.pack_configure(padx=pad - inset, pady=(S(16) if narrow else S(22), S(8)))
        # Narrow: drop the pair chip, then the app name (the logo stays).
        self.nav_pair.pack_forget()
        self.nav_title.pack_forget()
        if W >= S(400):
            self.nav_title.pack(side="left", padx=(S(10), 0))
        if W >= S(480):
            self.nav_pair.pack(side="left", padx=(S(10), 0))
        if self._page_job is not None:
            self.root.after_cancel(self._page_job)
        self._page_job = self.root.after(40, self._paint_page)
        self._schedule_fit()

    def _schedule_fit(self):
        if getattr(self, "_fit_job", None) is None and hasattr(self, "messages"):
            self._fit_job = self.root.after(60, self._fit_height)

    def _paint_page(self):
        self._page_job = None
        W, H, x, g, cw, nav_h, card_y, card_h = self._layout
        img = paint.gradient(W, H, C("page_a"), C("page_b"))
        r = S(theme.RADIUS_CARD)
        from .widgets import _scaled
        paint.drop_card(img, x, g, cw, nav_h, r, C("bg"), None, _scaled(theme.SHADOW_CARD))
        paint.drop_card(img, x, card_y, cw, card_h, r, C("bg"), None, _scaled(theme.SHADOW_CARD))
        self._page_photo = paint.to_photo(img)
        self.page.itemconfigure(self._page_img, image=self._page_photo)

    def _on_col_resize(self, e):
        w = max(S(200), e.width)
        self.subtitle.configure(wraplength=w)
        self.status_label.configure(wraplength=w)
        self.queue_label.configure(wraplength=w)
        self.usage_label.configure(wraplength=w)
        self.setting_hint.configure(wraplength=w)
        self.options_summary.configure(wraplength=max(S(80), w - S(200)))

    def _fit_height(self):
        """Short window: when the conversation would be squeezed to nothing,
        drop the title block (it comes back once there is room again)."""
        self._fit_job = None
        if getattr(self, "_compact", False) or self.card.winfo_height() <= 1:
            return  # while live the title block is hidden anyway
        h = self.messages.winfo_height() if self.messages.winfo_ismapped() else 0
        head_h = self.head.winfo_reqheight() + self.setting_hint.winfo_reqheight() + S(22)
        if not self._head_hidden and h < S(56):
            self._head_hidden = True
            self.head.grid_remove()
            self.setting_hint.grid_remove()
            self.setting_tabs.grid_configure(pady=(0, 0))
        elif self._head_hidden and h - head_h > S(80):  # room again, with margin
            self._head_hidden = False
            self.setting_tabs.grid_configure(pady=(S(18), 0))
            self.head.grid()
            self.setting_hint.grid()

    # --------------------------------------------------------------- devices
    def refresh_devices(self):
        try:
            self.pa.terminate()
        except Exception:
            pass
        self.pa = pyaudio.PyAudio()  # re-init so newly plugged devices appear
        self.devices = list_devices(self.pa)
        out_names = [d.name for d in self.devices.outputs]
        self.out_combo["values"] = out_names
        if out_names and self.out_combo.get() not in out_names:
            # Prefer something that isn't the default speakers (often headphones).
            pick = next((d for d in self.devices.outputs if "headphone" in d.name.lower()
                         or "headset" in d.name.lower()), None)
            pick = pick or next((d for d in self.devices.outputs
                                 if d.index == self.devices.default_output), self.devices.outputs[0])
            self.out_combo.set(pick.name)
        self._on_source_change()

    def _input_choices(self):
        return self.devices.loopbacks if self.source_var.get() == "loopback" else self.devices.mics

    def _on_source_change(self):
        mic = self.source_var.get() == "mic"
        if mic != getattr(self, "_source_was_mic", False):
            self._source_was_mic = mic
            if mic and not self.pause_var.get():
                # Always-on mic + spoken translation: protect against the app
                # hearing its own voice by default (the user can turn it off).
                self.pause_var.set(True)
                self._pause_while_speaking = True
        devs = self._input_choices()
        names = [d.name for d in devs]
        self.in_combo["values"] = names
        if self.in_combo.get() not in names:
            default = (self.devices.default_loopback if self.source_var.get() == "loopback"
                       else self.devices.default_mic)
            pick = next((d for d in devs if d.index == default), devs[0] if devs else None)
            self.in_combo.set(pick.name if pick else "")
        self._update_feedback_warning()

    def _selected(self, combo, devs) -> Optional[Device]:
        return next((d for d in devs if d.name == combo.get()), None)

    def _update_feedback_warning(self):
        in_dev = self._selected(self.in_combo, self._input_choices())
        out_dev = self._selected(self.out_combo, self.devices.outputs)
        msg = ""
        if in_dev is None:
            msg = "No input device found for this source."
        elif out_dev is None:
            msg = "No output device found for the voice."
        elif in_dev.kind == "loopback" and in_dev.render_name == out_dev.name:
            if self.pause_var.get():
                msg = ("⚠ The voice plays on the device being captured. Listening pauses "
                       "while it speaks, so meeting audio during that time is missed. "
                       "Headphones on a separate output are better.")
            else:
                msg = ("⚠ FEEDBACK RISK: the voice plays on the same device being captured, "
                       "so the app will hear and re-translate its own voice. Pick another output "
                       "(e.g. headphones), turn on 'Pause listening while the voice speaks', "
                       "or Mute voice.")
        elif in_dev.kind == "mic" and self.setting_var.get() == "onsite":
            msg = ("Onsite: the mic is muted while the voice speaks and for "
                   f"{config.ONSITE_TAIL_S:.1f} s after. Speech during that time is not heard; "
                   "wait for the green banner.")
        elif in_dev.kind == "mic" and not self.pause_var.get():
            msg = ("Mic mode: if the voice plays on speakers in the room, the mic will pick it "
                   "up. Use headphones, or turn on 'Pause listening while the voice speaks'.")
        if self.mute_var.get() and in_dev is not None and out_dev is not None:
            msg = ""  # subtitles only: nothing is spoken, so nothing to feed back
        danger = "⚠" in msg
        if getattr(self, "_compact", False) and not danger:
            msg = ""  # while live, only real risks stay on screen
        self.warn_label.configure(text=msg.replace("⚠ ", ""), tone="danger" if danger else "warning")
        self._schedule_fit()
        self._update_options_summary()

    # ----------------------------------------------------------- start/stop
    @property
    def state(self) -> State:
        return self.sm.state

    @property
    def tts(self) -> Optional[TtsQueue]:
        sess = self.session
        return sess.tts if sess is not None else None

    @property
    def latency(self) -> Optional[LatencyLog]:
        sess = self.session
        return sess.latency if sess is not None else None

    def _on_record(self):
        """The big button: start when idle, stop when running."""
        if self.state is State.IDLE:
            self.on_start()
        elif self.state is State.RUNNING:
            self.on_stop()

    def on_start(self):
        if self.state is not State.IDLE:
            return
        in_dev = self._selected(self.in_combo, self._input_choices())
        out_dev = self._selected(self.out_combo, self.devices.outputs)
        if in_dev is None or out_dev is None:
            self.set_status("error", "Choose an input device and a voice output device first.")
            return
        if in_dev.kind == "mic" and winmic.mic_blocked():
            self._show_mic_blocked()
            return
        if not self.sm.transition(State.STARTING):
            return
        self._refresh_live()  # immediately: the button is disabled, no double start
        render_dev = next((d for d in self.devices.outputs if d.name == in_dev.render_name), None)
        self._set_device_widgets("disabled")
        self._error_shown = False
        self.set_status("connecting", "Checking Azure key and region…")
        muted = self.mute_var.get()  # read Tk vars on the Tk thread only
        onsite = self.setting_var.get() == "onsite"
        if onsite:
            self.mode_var.set(self.turn.mode)  # the floor decides the direction
        mode = self.mode_var.get()
        self._onsite_live = onsite
        # Text echo guard in every setting: drops a result that repeats what the
        # voice just said (inert while muted, since nothing is said).
        self.echo = EchoFilter()
        self._voice_choice = voices.chosen()
        self.session = sess = Session()
        sess.run_start(self._start_worker, in_dev, out_dev, render_dev, muted, mode, onsite)

    def _start_worker(self, sess: Session, in_dev: Device, out_dev: Device,
                      render_dev: Optional[Device], muted: bool, mode: str,
                      onsite: bool = False):
        """Acquires resources in order, registering each with the session only
        once it is fully open. Never tears down: on error it reports, and the
        stop path (the single teardown owner) closes what was registered."""
        def post(fn, *args):
            self.post_for(sess, fn, *args)

        try:
            creds = config.load_credentials()
            voice_names = config.load_voices(self._voice_choice)
            config.segmentation_silence_ms()  # validate now: a bad value is a ConfigError
            config.preflight_check(creds)
            sess.check()
            self.usage.new_session()
            sess.transcript = Transcript(config.TRANSCRIPT_DIR, time.time())
            post(self._set_transcript, sess.transcript)
            # Registered first, so it is closed last (after TTS reported its outcomes).
            lat = LatencyLog(config.LOG_DIR / f"latency-{datetime.now():%Y%m%d-%H%M%S}.jsonl",
                             on_row=lambda row: post(self._show_latency, row),
                             keep_rows=LATENCY_ROWS_KEPT)
            sess.latency = sess.own("latency", lat, lambda l: l.close())

            pump = AudioPump(is_paused=self._is_listening_paused,
                             buffer_s=config.RECONNECT_BUFFER_S)
            sess.pump = sess.own("pump", pump, lambda p: p.stop())
            pump.start()

            tts = TtsQueue(
                creds,
                on_queue=lambda n, sk: post(self._show_queue, n, sk),
                on_spoken_chars=self._add_chars,
                on_error=lambda m: post(self.set_status, None, m),
                voices=voice_names,
                on_playing=self._on_voice_playing,
                tail_s=config.ONSITE_TAIL_S if onsite else config.PAUSE_TAIL_S,
                # Conversation: read everything, in order. Meeting: stay current.
                policy=POLICY_IN_ORDER if onsite else POLICY_CATCH_UP,
                on_timing=lat.tts,
            )
            tts.set_muted(muted)
            tts.start(Player(self.pa, out_dev))  # tts owns the player from here
            sess.tts = sess.own("tts", tts, lambda t: t.stop(), interrupt=tts.interrupt)

            controller = RecognitionController(
                creds, pump,
                on_interim=lambda *a: post(self._show_interim, *a),
                on_final=lambda seg: post(self._show_final, seg),
                on_status=lambda st, m: post(self.set_status, st, m),
                on_fatal=lambda m: post(self._fail, sess, m),
                mode=mode,
                on_gap=lambda at, secs: post(self._note_gap, at, secs),
            )
            # Registered before start(): stopping it is what aborts a pending connect.
            sess.controller = sess.own("controller", controller, lambda c: c.stop(),
                                       interrupt=controller.stop)
            # Direction may have changed while starting; set_mode is a no-op if not.
            post(self._sync_mode, sess)

            sess.check()
            capture = AudioCapture(self.pa)
            capture.start(in_dev, self._metered(pump.feed), render_dev)
            sess.own("capture", capture, lambda c: c.stop())

            post(self._note, f"Session started. Auto-saving to {sess.transcript.txt_path}")
            controller.start()
            sess.check()
        except Cancelled:
            return  # Stop is already tearing down; nothing to report
        except config.ConfigError as e:
            post(self._fail, sess, str(e))
            return
        except ConnectionError as e:
            post(self._fail, sess, f"{e} Check your internet connection.")
            return
        except OSError as e:
            if sess.cancelled.is_set():
                return
            if in_dev.kind == "mic" and winmic.mic_blocked():
                post(self._show_mic_blocked)
            post(self._fail, sess, f"Audio device error: {e}. Try another device or Refresh devices.")
            return
        except Exception as e:
            if sess.cancelled.is_set():
                return  # e.g. an SDK error caused by the concurrent stop
            post(self._fail, sess, f"Could not start: {e}")
            return
        post(self._started, sess)

    def _metered(self, feed):
        """Wraps the pump's feed to keep an input level for the meter. Runs in
        the capture callback, so it only does one cheap RMS."""
        import numpy as np

        def metered(chunk: bytes):
            try:
                a = np.frombuffer(chunk, dtype=np.int16)
                if a.size:
                    rms = float(np.sqrt(np.mean(a.astype(np.float32) ** 2)))
                    db = 20 * np.log10(max(rms, 1.0) / 32768.0)
                    self._level = max(0.0, min(1.0, (db + 55) / 45))
            except Exception:
                pass
            feed(chunk)
        return metered

    def _started(self, sess: Session):
        self.sm.transition(State.RUNNING)  # refused if Stop already happened
        self._refresh_live()

    def on_stop(self):
        self._begin_stop()

    def _begin_stop(self) -> bool:
        sess = self.session
        if sess is None or not self.sm.transition(State.STOPPING):
            return False  # idle, or already stopping: double Stop is a no-op
        sess.cancelled.set()
        self._refresh_live()
        if not self._error_shown:
            self.set_status("stopping", "Stopping…")
        threading.Thread(target=self._stop_worker, args=(sess,),
                         name=f"stop-{sess.id}", daemon=True).start()
        return True

    def _stop_worker(self, sess: Session):
        sess.stop()
        if sess.pump is not None:
            self.usage.session_audio_s = sess.pump.seconds_sent
        try:
            self.usage.save()
        except OSError:
            pass
        self.post_for(sess, self._finish_stop, sess)

    def _finish_stop(self, sess: Session):
        if not self.sm.transition(State.IDLE):
            return
        self.session = None
        self._set_device_widgets("normal")
        if not self._error_shown:
            self.set_status("idle", "Stopped.")
        self._update_usage_label()
        self._last_delay = None
        self._playing_ids = ()
        self.messages.clear_interim()
        self.messages.set_playing(())
        self.queue_label.configure(text="Voice queue: 0", fg=C("text_3"))
        self._refresh_live()

    def _fail(self, sess: Session, message: str):
        """Fatal error: show it, then tear down. Never reconnects."""
        self._error_shown = True
        self.set_status("error", message)
        self._begin_stop()

    def _set_transcript(self, transcript: Transcript):
        self.transcript = transcript

    def _set_device_widgets(self, state):
        combo_state = "readonly" if state == "normal" else "disabled"
        self.in_combo.configure(state=combo_state)
        self.out_combo.configure(state=combo_state)
        for w in (self.rb_loop, self.rb_mic, self.refresh_btn, self.rb_meeting, self.rb_onsite):
            w.configure(state=state)
        if state == "normal":
            self._apply_setting()  # re-apply onsite restrictions
            if self._options_open_before:
                self._set_options_open(True)
        else:
            self._options_open_before = self.options_open
            self._set_options_open(False)  # more room for the conversation while live
        self._set_compact(state != "normal")

    def _set_compact(self, on: bool):
        """While live, hide what can't be changed anyway (title, setting tabs,
        info notes) so the conversation gets the space."""
        self._compact = on
        for w in (self.setting_tabs,):
            w.grid_remove() if on else w.grid()
        for w in (self.head, self.setting_hint):
            w.grid_remove() if on or self._head_hidden else w.grid()
        if on and not self.usage.warnings():
            self.usage_label.pack_forget()
        elif not on:
            self.usage_label.pack(fill="x", pady=(S(2), 0))
        self._update_feedback_warning()

    def _apply_setting(self):
        """Onsite: mic only, mic gating always on, direction set by the floor."""
        onsite = self.setting_var.get() == "onsite"
        if onsite:
            if self.source_var.get() != "mic":
                self.source_var.set("mic")
                self._on_source_change()
            self.rb_loop.configure(state="disabled")
            self.pause_var.set(True)
            self.pause_cb.configure(state="disabled")
            self.mode_var.set(self.turn.mode)
            for rb in self.mode_rbs:
                rb.configure(state="disabled")
            self.lang_row.grid_remove()
            self.turn_frame.grid(row=3, column=0, sticky="we", pady=(S(10), 0))
            self.setting_hint.configure(text="Two people, one device. Just talk: the language of "
                                             "each sentence is detected. The mic pauses only while "
                                             "the translation is read out.")
        else:
            if self.state is State.IDLE:
                self.rb_loop.configure(state="normal")
                self.pause_cb.configure(state="normal")
                for rb in self.mode_rbs:
                    rb.configure(state="normal")
            self.turn_frame.grid_remove()
            self.lang_row.grid()
            self.setting_hint.configure(text="Remote meeting, or listening only. Captions and "
                                             "voice follow the speaker.")
            self._sync_lang_row()
        self.messages.set_two_sided(onsite or self.mode_var.get() == config.MODE_AUTO)
        self._on_pause_opt()

    # ---------------------------------------------------------- language row
    def _sync_lang_row(self):
        """Show mode_var in the source/target pickers."""
        mode = self.mode_var.get()
        if mode == config.MODE_AUTO:
            self.src_select.set(SRC_LABELS[config.MODE_AUTO])
            self.tgt_select.set(TGT_AUTO_LABEL)
            auto = True
        else:
            src, tgt = config.direction(mode)
            self.src_select.set(SRC_LABELS[src])
            self.tgt_select.set(config.LANGUAGES[tgt].name)
            auto = False
        if self.setting_var.get() != "onsite":
            self.swap_btn.configure(state="disabled" if auto else "normal")
            self.tgt_select.configure(state="disabled" if auto else "normal")
        self.messages.set_two_sided(self.setting_var.get() == "onsite" or auto)

    def _set_mode(self, mode: str):
        if mode != self.mode_var.get():
            self.mode_var.set(mode)
            self._on_mode_change()

    def _on_src_pick(self, label: str):
        code = next(k for k, v in SRC_LABELS.items() if v == label)
        self._set_mode(code if code == config.MODE_AUTO else f"{code}>{config.other(code)}")

    def _on_tgt_pick(self, label: str):
        code = next(c for c in config.PAIR if config.LANGUAGES[c].name == label)
        self._set_mode(f"{config.other(code)}>{code}")

    def _swap(self):
        mode = self.mode_var.get()
        if mode != config.MODE_AUTO:
            src, tgt = config.direction(mode)
            self._set_mode(f"{tgt}>{src}")

    # -------------------------------------------------------- live updates
    def post(self, fn, *args):
        self._ui_q.put((None, fn, args))

    def post_for(self, sess: Session, fn, *args):
        """Like post(), but dropped if `sess` is no longer the live session, so a
        late callback from an old session can't touch the next one."""
        self._ui_q.put((sess, fn, args))

    def _drain(self):
        self._drain_once()
        self.root.after(50, self._drain)

    def _drain_once(self):
        try:
            while True:
                sess, fn, args = self._ui_q.get_nowait()
                if sess is not None and sess is not self.session:
                    continue
                fn(*args)
        except queue.Empty:
            pass

    def set_status(self, state: Optional[str], message: str):
        color = C(STATUS_TOKENS.get(state, "warning"))
        if state is not None and self._error_shown and state != "error":
            return  # keep the error visible until the next Start
        if state is not None:
            self._status_state = state
        self.status_label.configure(text=message, fg=color)

    def _show_interim(self, source_text: str, translated: str, src: str, tgt: str):
        self._interim_at = time.monotonic()
        self.messages.set_interim(source_text, translated, src, tgt)
        view = self.subtitle_view
        if view is not None:
            view.show_interim(source_text, translated, src, tgt)

    def _show_final(self, seg: Segment):
        shown = time.monotonic()
        lat = self.latency
        echo = self.echo
        if echo is not None and echo.is_echo(seg.source_text, seg.source_lang):
            self._note(f"Ignored (sounded like the app's own voice): {seg.source_text}")
            if lat is not None:
                lat.ignored(seg, "echo")
            return
        self._interim_at = 0.0
        seg_id = len(self._segs) + 1
        self._segs[seg_id] = seg
        self._last_seg_id = seg_id
        self.messages.add_message(seg_id, seg.source_text, seg.translated_text,
                                  seg.source_lang, seg.target_lang, seg.start)
        view = self.subtitle_view
        if view is not None:
            view.show_final(seg.source_text, seg.translated_text, seg.source_lang,
                            seg.target_lang)
        if self.transcript:
            try:
                self.transcript.add(seg)
            except OSError as e:
                self.set_status(None, f"Auto-save failed: {e}")
        tts = self.tts
        voiced = tts is not None and self.state is State.RUNNING
        if lat is not None:
            # Before enqueue: the TTS thread may report on this segment at once.
            lat.recognized(seg_id, seg, shown, enqueued=voiced)
        if voiced:
            if not tts.enqueue(seg.translated_text, seg.target_lang, seg_id) and lat is not None:
                lat.unvoiced(seg_id, "muted")
            if self._onsite_live:
                self.turn.on_final(seg.source_lang)

    def _note(self, text: str):
        self.messages.add_note(text)

    def _show_queue(self, waiting: int, skipped: int):
        text = f"Voice queue: {waiting}"
        if skipped:
            text += f"  (skipped {skipped} to keep up; ▶ on a message replays it)"
        tts = self.tts
        behind = tts.behind_seconds() if tts is not None else 0.0
        warn = behind >= config.TTS_BEHIND_WARN_S
        if warn:
            text += f"  ⚠ voice is ~{behind:.0f} s behind"
        text += self._delay_text()
        self.queue_label.configure(text=text, fg=C("danger_active") if warn else C("text_3"))

    def _delay_text(self) -> str:
        row = self._last_delay
        if not row:
            return ""
        parts = []
        if row.get("caption_s") is not None:
            parts.append(f"text {row['caption_s']:.1f} s")
        if row.get("total_s") is not None:
            parts.append(f"voice {row['total_s']:.1f} s")
        return f"   ·   last delay after speech: {', '.join(parts)}" if parts else ""

    def _show_latency(self, row: dict):
        """A latency row is complete (see translator.latency); show the latest."""
        if row.get("kind") != "speech":
            return
        self._last_delay = row
        tts = self.tts
        self._show_queue(tts.waiting if tts is not None else 0,
                         tts.skipped if tts is not None else 0)

    def _live_state(self):
        """(record-button state, mic muted) from the session state."""
        st = self.state
        if st is State.STARTING:
            return "starting", False
        if st is State.STOPPING:
            return "stopping", False
        if st is not State.RUNNING:
            return ("error" if self._error_shown else "idle"), False
        if self._status_state == "reconnecting":
            return "reconnecting", False
        tts = self.tts
        tts_state = tts.state() if tts is not None else "idle"
        gated = self._pause_while_speaking or self._onsite_live
        if tts_state in ("speaking", "tail"):
            return "speaking", gated
        if tts_state == "pending" or time.monotonic() - self._interim_at < 1.2:
            return "translating", False
        return "listening", False

    def _refresh_live(self):
        state, muted = self._live_state()
        hint = None
        if state in ("listening", "translating") and self.mute_var.get():
            hint = "Listening · voice off, subtitles only"
        self.record_btn.set_state(state, level=self._level, mic_muted=muted, hint=hint)
        tts = self.tts
        speaking = tts is not None and tts.state() == "speaking"
        self.messages.set_playing(self._playing_ids if speaking else ())

    # ---------------------------------------------------------------- replay
    def _replay(self, seg_id: int) -> bool:
        seg = self._segs.get(seg_id)
        if seg is None:
            return False
        tts = self.tts
        if tts is None or self.state is not State.RUNNING:
            self.set_status(None, "Replay works while the translator is running.")
            return False
        if self.mute_var.get():
            self.set_status(None, "The voice is muted (subtitles only). Unmute to replay.")
            return False
        return tts.replay(seg.translated_text, seg.target_lang, seg_id)

    def _replay_last(self):
        if self._last_seg_id is not None:
            self._replay(self._last_seg_id)

    def _add_chars(self, n: int):
        self.usage.session_chars += n  # int += is fine across threads here

    def _is_listening_paused(self) -> bool:
        """Feedback gate, checked per captured chunk. Always on in onsite mode,
        never while muted (TtsQueue.is_busy is False then): the mic captures
        100 % of the time in subtitles-only mode."""
        tts = self.tts
        if tts is None or not (self._pause_while_speaking or self._onsite_live):
            return False
        return tts.is_busy()

    def _on_voice_playing(self, utt):
        """TTS thread: remember what is about to be said, for the echo filter
        and the 'speaking' highlight on its message cards."""
        self._playing_ids = tuple(getattr(utt, "seg_ids", ()) or ())
        echo = self.echo
        if echo is not None:
            echo.spoken(utt.text, utt.lang)

    # ---------------------------------------------------------------- onsite
    def _give_floor(self, side):
        if self.setting_var.get() != "onsite":
            return
        mode = self.turn.give_floor(None if side == "auto" else side)
        self._show_floor()
        if mode is not None:
            self.mode_var.set(mode)
            self._on_mode_change()

    def _skip_voice(self):
        tts = self.tts
        if tts is not None:
            tts.skip()

    def _on_escape(self, event=None):
        top = Overlay.topmost()
        if top is not None:
            top.close()
            return "break"
        self._skip_voice()
        return None

    def _on_auto_alternate(self):
        self.turn.auto_alternate = self.auto_var.get()

    def _show_floor(self):
        value = self.turn.side or "auto"
        if self.floor_var.get() != value:
            self.floor_var.set(value)
        cb = getattr(self, "auto_cb", None)
        if cb is not None:  # passing the turn only means something for a chosen side
            cb.configure(state="disabled" if self.turn.side is None else "normal")

    def _onsite_tick(self):
        try:
            self._update_onsite()
            self._refresh_live()
        finally:
            self.root.after(100, self._onsite_tick)

    def _update_onsite(self):
        if self.setting_var.get() != "onsite":
            return
        sess = self.session
        tts, ctl = self.tts, (sess.controller if sess is not None else None)
        if self.state is not State.RUNNING or tts is None or ctl is None:
            self.phase_label.configure(text=PHASE_IDLE, bg=C("text_2"))
            return
        tts_state = tts.state()
        mode = self.turn.poll(tts_state, ctl.is_switching)
        if mode is not None:  # auto-alternate passed the floor
            self._show_floor()
            self.mode_var.set(mode)
            self._on_mode_change()
        phase = phase_of(tts_state, ctl.is_switching)
        token, text = PHASE_BANNER[phase]
        if isinstance(text, dict):
            text = text[self.turn.side or "auto"]
        self.phase_label.configure(text=text, bg=C(token))

    def _on_mute(self):
        """Subtitles-only toggle. Never touches the mic or the recognizer."""
        muted = self.mute_var.get()
        if self.tts:
            self.tts.set_muted(muted)  # stops the current sentence, clears the queue
        if muted:
            self._playing_ids = ()
        self._update_options_summary()
        self._update_feedback_warning()
        self._show_mute()

    def _toggle_mute(self):
        self.mute_var.set(not self.mute_var.get())
        self._on_mute()

    def _show_mute(self):
        muted = self.mute_var.get()
        self.mute_btn.configure(icon="mute" if muted else "speaker")
        self.mute_btn.set_toggled(muted)
        self.mute_btn.label = ("Unmute the spoken translation (Ctrl+M)" if muted
                               else "Mute the spoken translation (Ctrl+M)")
        self.mute_caption.configure(text="Voice off" if muted else "Voice on",
                                    fg=C("primary") if muted else C("text_3"))

    def _note_gap(self, lost_at: float, seconds: float):
        """A connection outage is over; say so where the missing speech would be."""
        when = datetime.fromtimestamp(lost_at).strftime("%H:%M:%S")
        kept = min(seconds, config.RECONNECT_BUFFER_S)
        if seconds <= config.RECONNECT_BUFFER_S:
            text = (f"Connection dropped at {when} and came back after {seconds:.0f} s. "
                    "Speech in that time was buffered and translated.")
        else:
            text = (f"Connection dropped at {when} for {seconds:.0f} s. Only the last "
                    f"{kept:.0f} s were buffered; speech before that was not translated.")
        self._note(text)

    # ------------------------------------------------------- subtitle view
    def _open_subtitles(self):
        if self.subtitle_view is not None and not self.subtitle_view.closed:
            self.subtitle_view.win.lift()
            self.subtitle_view.win.focus_force()
            return

        def closed():
            self.subtitle_view = None
        self.subtitle_view = SubtitleView(self.root, on_close=closed)
        seg = self._segs.get(self._last_seg_id) if self._last_seg_id else None
        if seg is not None:
            self.subtitle_view.show_final(seg.source_text, seg.translated_text,
                                          seg.source_lang, seg.target_lang)

    def _on_mode_change(self):
        sess = self.session
        if sess is not None and sess.controller is not None:
            self._sync_mode(sess)

    def _sync_mode(self, sess: Session):
        mode = self.mode_var.get()
        ctl = sess.controller
        if ctl is not None and ctl.mode != mode:
            ctl.set_mode(mode)
            self._note(f"Direction: {config.mode_label(mode)}")

    def _on_pause_opt(self):
        self._pause_while_speaking = self.pause_var.get()
        self._update_feedback_warning()

    def _tick(self):
        sess = self.session
        if sess is not None and sess.pump is not None:
            self.usage.session_audio_s = sess.pump.seconds_sent
        self._update_usage_label()
        if time.monotonic() - self._last_usage_save > 30 and self.state is State.RUNNING:
            self._last_usage_save = time.monotonic()
            try:
                self.usage.save()
            except OSError:
                pass
        self.root.after(1000, self._tick)

    def _usage_text(self) -> str:
        u = self.usage
        return (f"This session: {u.session_audio_s / 60:.1f} audio min, {u.session_chars:,} TTS chars"
                f"   ·   This month (this app): {u.month_audio_s / 3600:.2f} / "
                f"{config.F0_TRANSLATION_AUDIO_HOURS} h audio, {u.month_chars:,} / "
                f"{config.F0_NEURAL_TTS_CHARS:,} TTS chars (F0 free)")

    def _update_usage_label(self):
        text = self._usage_text()
        warns = self.usage.warnings()
        if warns:
            text += "   ⚠ " + "; ".join(warns)
        self.usage_label.configure(text=text, fg=C("danger_active") if warns else C("text_3"))

    # -------------------------------------------------------- options / voice
    def _toggle_options(self):
        self._set_options_open(not self.options_open)

    def _set_options_open(self, on: bool):
        self.options_open = on
        if on:
            self.options_body.grid()
        else:
            self.options_body.grid_remove()
        self.options_btn.configure(icon_right="chevron_up" if on else "chevron_down")
        self._update_options_summary()
        self._schedule_fit()

    def _update_options_summary(self):
        if self.options_open:
            self.options_summary.configure(text="")
            return
        src = "System audio" if self.source_var.get() == "loopback" else "Microphone"
        voice = "muted" if self.mute_var.get() else self._voice_summary()
        self.options_summary.configure(text=f"{src} · voice: {voice}")

    def _voice_summary(self) -> str:
        try:
            names = config.load_voices(self._voice_choice)
        except config.ConfigError:
            names = dict(config.DEFAULT_VOICES)
        return " · ".join(voices.short_label(names[c]) for c in config.PAIR)

    def _open_voice_picker(self):
        try:
            current = config.load_voices(self._voice_choice)
        except config.ConfigError:
            current = dict(config.DEFAULT_VOICES)
        out_dev = self._selected(self.out_combo, self.devices.outputs)
        previewer = None
        if out_dev is not None:
            previewer = voices.Previewer(
                lambda: Player(self.pa, out_dev),
                lambda name, err: self.post(self._preview_state, name, err))
        self._picker = VoicePicker(self.root, current, self._choose_voice, previewer=previewer,
                                   running=self.state is not State.IDLE, post=self.post)

    def _preview_state(self, name, err):
        if self._picker is not None:
            self._picker.preview_state(name, err)

    def _choose_voice(self, lang: str, name: str):
        voices.save_choice(lang, name)
        self._voice_choice = voices.chosen()
        self.voice_btn.configure(text=self._voice_summary())
        self._update_options_summary()

    # --------------------------------------------------- navbar / modals
    def _toggle_large(self):
        on = not self.large_var.get()
        self.large_var.set(on)
        self.large_btn.set_toggled(on)
        self.messages.set_large(on)
        data = voices.load_settings()
        data["large_text"] = on
        voices.save_settings(data)

    def _export_menu(self):
        items = ["Transcript (.txt)", "Subtitles (.srt)"]
        Popover(self.export_btn, items, None,
                lambda item: self.export(".srt" if "srt" in item else ".txt"), width=200)

    def _open_settings(self):
        def build(body):
            bg = body.cget("bg")

            def section(title):
                tk.Label(body, text=title, bg=bg, fg=C("text"), font=theme.T.font(15, 600),
                         anchor="w").pack(fill="x", pady=(S(8), S(4)))

            def line(text, fg="text_2"):
                tk.Label(body, text=text, bg=bg, fg=C(fg), font=theme.T.font(13),
                         anchor="w", justify="left", wraplength=S(440)).pack(fill="x")
            section("Keyboard shortcuts")
            for keys, what in (("F1 / F2", "Onsite: give the floor to the English / Thai speaker"),
                               ("F3", "Replay the last translation"),
                               ("Esc", "Skip the voice, or close a dialog"),
                               ("Tab, Space", "Move between controls and press them")):
                row = tk.Frame(body, bg=bg)
                row.pack(fill="x", pady=S(1))
                tk.Label(row, text=keys, bg=C("bg_subtle"), fg=C("text"), font=theme.T.font(12, 600),
                         padx=S(8), pady=S(1), width=10).pack(side="left")
                tk.Label(row, text=what, bg=bg, fg=C("text_2"), font=theme.T.font(13),
                         anchor="w").pack(side="left", padx=(S(8), 0))
            section("Usage (Azure free tier)")
            line(self._usage_text())
            warns = self.usage.warnings()
            if warns:
                line("⚠ " + "; ".join(warns), "danger_active")
            section("Appearance")
            line("Light or dark follows your Windows app mode. Animations follow Windows "
                 "'Animation effects'. Large text: the Aa button in the top bar.")
            section("Files")
            line(f"Transcripts, logs and settings are kept in {config.APP_DIR}.")
        Modal(self.root, "Settings", build, width=540)

    def _show_mic_blocked(self):
        def build(body):
            bg = body.cget("bg")
            ic = tk.Canvas(body, width=S(56), height=S(56), bg=bg, highlightthickness=0)
            ic.pack(anchor="w")
            ic.create_image(S(28), S(28), image=paint.circle(S(56), C("danger_10"), bg))
            ic.create_text(S(28), S(28), text=theme.T.icon("mic_off"), font=theme.T.icon_font(22),
                           fill=C("danger_active"))
            steps = ("Open Windows Settings → Privacy & security → Microphone.",
                     "Turn on “Microphone access”.",
                     "Turn on “Let desktop apps access your microphone”.",
                     "Come back here and press the mic again.")
            for i, s in enumerate(steps, 1):
                row = tk.Frame(body, bg=bg)
                row.pack(fill="x", pady=(S(8) if i == 1 else S(4), 0))
                tk.Label(row, text=str(i), bg=C("primary_10"), fg=C("primary"),
                         font=theme.T.font(12, 700), width=2).pack(side="left", anchor="n")
                tk.Label(row, text=s, bg=bg, fg=C("text"), font=theme.T.font(14), anchor="w",
                         justify="left", wraplength=S(380)).pack(side="left", padx=(S(10), 0))
            btns = tk.Frame(body, bg=bg)
            btns.pack(fill="x", pady=(S(18), 0))
            Button(btns, "Open microphone settings", icon="settings", variant="primary",
                   command=winmic.open_settings).pack(side="left")
            Button(btns, "Use system audio instead", variant="secondary",
                   command=lambda: (self.source_var.set("loopback"), self._on_source_change(),
                                    Overlay.topmost() and Overlay.topmost().close())
                   ).pack(side="left", padx=(S(8), 0))
        Modal(self.root, "Microphone access is off", build, width=520,
              subtitle="Windows is blocking desktop apps from using the microphone, so the "
                       "translator would hear only silence.")

    # ------------------------------------------------------------- export
    def export(self, suffix: str):
        if not self.transcript or not self.transcript.segments:
            messagebox.showinfo("Export", "Nothing to export yet.")
            return
        default = Path(self.transcript.txt_path).with_suffix(suffix).name
        path = filedialog.asksaveasfilename(
            defaultextension=suffix, initialfile=default,
            filetypes=[("Subtitles" if suffix == ".srt" else "Text", f"*{suffix}")])
        if not path:
            return
        try:
            self.transcript.export(Path(path))
            self._note(f"Exported {path}")
        except OSError as e:
            messagebox.showerror("Export failed", str(e))

    def on_close(self):
        sess = self.session
        if sess is not None:
            sess.stop(join_timeout=3)  # idempotent if a stop worker is already on it
        try:
            self.usage.save()
        except OSError:
            pass
        self.pa.terminate()
        self.root.destroy()


def _setup_logging():
    """INFO log (incl. one latency line per utterance) to logs/translator.log."""
    root = logging.getLogger()
    if root.handlers:
        return
    try:
        config.LOG_DIR.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(config.LOG_DIR / "translator.log", encoding="utf-8")
    except OSError:
        return
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)
    root.setLevel(logging.INFO)


def run():
    _setup_logging()
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # crisp text on high-DPI screens
    except Exception:
        pass
    theme.register_fonts()  # before Tk enumerates fonts
    root = tk.Tk()
    theme.init(root)
    App(root)
    root.mainloop()
