"""Tkinter UI. All SDK/audio callbacks run on other threads and reach Tk only
through `post()`, which the Tk main loop drains every 50 ms."""
import logging
import queue
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, font as tkfont, messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from typing import Optional

import pyaudiowpatch as pyaudio

from . import config
from .audio import AudioCapture, AudioPump, Device, Player, list_devices
from .latency import LatencyLog
from .onsite import EchoFilter, Phase, TurnTaker, phase_of
from .recognition import RecognitionController, Segment
from .session import Cancelled, Session, State, StateMachine
from .transcript import Transcript
from .tts import POLICY_CATCH_UP, POLICY_IN_ORDER, TtsQueue
from .usage import Usage

STATUS_COLORS = {"idle": "#555555", "connecting": "#1f5fa8", "listening": "#1b7a2e",
                 "reconnecting": "#b36b00", "error": "#b00020", "stopping": "#555555"}

SIDE_LABEL = {"en": "English speaker · ผู้พูดอังกฤษ", "th": "ผู้พูดภาษาไทย · Thai speaker"}
# Onsite banner per phase: (background, text). Bilingual: both people read it.
PHASE_BANNER = {
    Phase.LISTENING: ("#1b7a2e", {"en": "🎤  English speaker: please speak   ·   ผู้พูดภาษาอังกฤษ พูดได้เลย",
                                  "th": "🎤  ผู้พูดภาษาไทย พูดได้เลย   ·   Thai speaker: please speak"}),
    Phase.SWITCHING: ("#1f5fa8", "⏳  Switching turn… you can start speaking   ·   กำลังสลับผู้พูด… เริ่มพูดได้"),
    Phase.TRANSLATING: ("#b36b00", "…  Translating   ·   กำลังแปล"),
    Phase.SPEAKING: ("#b00020", "🔊  Reading the translation: please wait   ·   กำลังอ่านคำแปล กรุณารอ"),
    Phase.TAIL: ("#b00020", "🔊  Reading the translation: please wait   ·   กำลังอ่านคำแปล กรุณารอ"),
}


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
        # Every final sentence shown in History, by id, for replay.
        self._segs: dict = {}
        self._last_seg_id: Optional[int] = None
        self._last_usage_save = time.monotonic()

        self._setup_fonts()
        self._build()
        self.refresh_devices()
        self._update_usage_label()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(50, self._drain)
        root.after(1000, self._tick)
        root.after(100, self._onsite_tick)
        root.bind_all("<F1>", lambda e: self._give_floor("en"))
        root.bind_all("<F2>", lambda e: self._give_floor("th"))
        root.bind_all("<Escape>", lambda e: self._skip_voice())
        root.bind_all("<F3>", lambda e: self._replay_last())

    # ---------------------------------------------------------------- layout
    def _setup_fonts(self):
        families = set(tkfont.families(self.root))
        thai = next((f for f in ("Leelawadee UI", "Leelawadee", "Tahoma") if f in families), "TkDefaultFont")
        # Per language: big caption font, small (source line) font, history font.
        self.f_big = {"th": tkfont.Font(family=thai, size=26),
                      "en": tkfont.Font(family="Segoe UI", size=22)}
        self.f_sub = {"th": tkfont.Font(family=thai, size=14),
                      "en": tkfont.Font(family="Segoe UI", size=13)}
        self.f_hist = {"th": tkfont.Font(family=thai, size=13),
                       "en": tkfont.Font(family="Segoe UI", size=11)}
        self.f_small = tkfont.Font(family="Segoe UI", size=10)

    def _build(self):
        r = self.root
        r.title("Meeting Translator: English ⇄ Thai")
        r.geometry("1000x760")
        r.minsize(760, 560)

        top = ttk.Frame(r, padding=(10, 8, 10, 0))
        top.pack(fill="x")
        self.setting_var = tk.StringVar(value="meeting")
        ttk.Label(top, text="Setting:").pack(side="left")
        self.rb_meeting = ttk.Radiobutton(top, text="Meeting (remote or listening only)",
                                          value="meeting", variable=self.setting_var,
                                          command=self._apply_setting)
        self.rb_onsite = ttk.Radiobutton(top, text="Onsite conversation (two people, one device)",
                                         value="onsite", variable=self.setting_var,
                                         command=self._apply_setting)
        self.rb_meeting.pack(side="left", padx=(28, 0))
        self.rb_onsite.pack(side="left", padx=(12, 0))

        ctl = ttk.Frame(r, padding=(10, 8))
        ctl.pack(fill="x")

        self.source_var = tk.StringVar(value="loopback")
        ttk.Label(ctl, text="Listen to:").grid(row=0, column=0, sticky="w")
        src = ttk.Frame(ctl)
        src.grid(row=0, column=1, columnspan=3, sticky="w")
        self.rb_loop = ttk.Radiobutton(src, text="System audio (Teams/Zoom/Meet)", value="loopback",
                                       variable=self.source_var, command=self._on_source_change)
        self.rb_mic = ttk.Radiobutton(src, text="Microphone (in-room)", value="mic",
                                      variable=self.source_var, command=self._on_source_change)
        self.rb_loop.pack(side="left")
        self.rb_mic.pack(side="left", padx=(12, 0))

        ttk.Label(ctl, text="Input device:").grid(row=1, column=0, sticky="w", pady=2)
        self.in_combo = ttk.Combobox(ctl, state="readonly", width=60)
        self.in_combo.grid(row=1, column=1, sticky="we", pady=2)
        self.in_combo.bind("<<ComboboxSelected>>", lambda e: self._update_feedback_warning())

        ttk.Label(ctl, text="Voice output:").grid(row=2, column=0, sticky="w", pady=2)
        self.out_combo = ttk.Combobox(ctl, state="readonly", width=60)
        self.out_combo.grid(row=2, column=1, sticky="we", pady=2)
        self.out_combo.bind("<<ComboboxSelected>>", lambda e: self._update_feedback_warning())
        self.refresh_btn = ttk.Button(ctl, text="Refresh devices", command=self.refresh_devices)
        self.refresh_btn.grid(row=1, column=2, rowspan=2, padx=8)
        ctl.columnconfigure(1, weight=1)

        # Direction can be changed while running (the recognizer reopens).
        self.mode_var = tk.StringVar(value=config.DEFAULT_MODE)
        ttk.Label(ctl, text="Direction:").grid(row=3, column=0, sticky="w", pady=2)
        modes = ttk.Frame(ctl)
        modes.grid(row=3, column=1, columnspan=3, sticky="w", pady=2)
        self.mode_rbs = []
        for m in config.MODES:
            text = config.mode_label(m) + (" (experimental)" if m == config.MODE_AUTO else "")
            rb = ttk.Radiobutton(modes, text=text, value=m, variable=self.mode_var,
                                 command=self._on_mode_change)
            rb.pack(side="left", padx=(0, 12))
            self.mode_rbs.append(rb)

        btns = ttk.Frame(ctl)
        btns.grid(row=4, column=0, columnspan=4, sticky="we", pady=(6, 0))
        self.start_btn = ttk.Button(btns, text="▶ Start", command=self.on_start)
        self.stop_btn = ttk.Button(btns, text="■ Stop", command=self.on_stop, state="disabled")
        self.start_btn.pack(side="left")
        self.stop_btn.pack(side="left", padx=(6, 16))
        self.mute_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(btns, text="Mute voice (script only)", variable=self.mute_var,
                        command=self._on_mute).pack(side="left")
        self.pause_var = tk.BooleanVar(value=False)
        self.pause_cb = ttk.Checkbutton(btns, text="Pause listening while voice speaks",
                                        variable=self.pause_var, command=self._on_pause_opt)
        self.pause_cb.pack(side="left", padx=(12, 0))
        self.replay_btn = ttk.Button(btns, text="↻ Replay last [F3]", command=self._replay_last)
        self.replay_btn.pack(side="left", padx=(12, 0))
        ttk.Button(btns, text="Export .srt", command=lambda: self.export(".srt")).pack(side="right")
        ttk.Button(btns, text="Export .txt", command=lambda: self.export(".txt")).pack(side="right", padx=6)

        self.warn_label = tk.Label(r, text="", fg="#b00020", font=self.f_small, anchor="w",
                                   justify="left")
        self.warn_label.pack(fill="x", padx=10)

        self._build_turn_panel(r)

        cap = tk.Frame(r, bg="white", highlightthickness=1, highlightbackground="#cccccc")
        cap.pack(fill="x", padx=10, pady=6)
        self.cap_frame = cap
        # main_label: the translation (big); sub_label: what was said.
        self.main_label = tk.Label(cap, text="กด Start เพื่อเริ่มแปล", font=self.f_big["th"],
                                   bg="white", fg="#999999", anchor="w", justify="left", height=3)
        self.main_label.pack(fill="x", padx=12, pady=(8, 0))
        self.sub_label = tk.Label(cap, text="Press Start to begin.", font=self.f_sub["en"],
                                  bg="white", fg="#999999", anchor="w", justify="left", height=2)
        self.sub_label.pack(fill="x", padx=12, pady=(0, 8))
        cap.bind("<Configure>", self._on_caption_resize)

        ttk.Label(r, text="History", font=self.f_small).pack(anchor="w", padx=10)
        self.history = ScrolledText(r, wrap="word", height=12, state="disabled", padx=8, pady=6)
        self.history.pack(fill="both", expand=True, padx=10)
        self.history.tag_configure("time", foreground="#888888", font=self.f_small)
        self.history.tag_configure("replay", foreground="#1f5fa8", font=self.f_small)
        self.history.tag_bind("replay", "<Button-1>", self._on_history_click)
        self.history.tag_bind("replay", "<Enter>", lambda e: self.history.configure(cursor="hand2"))
        self.history.tag_bind("replay", "<Leave>", lambda e: self.history.configure(cursor=""))
        for code, f in self.f_hist.items():
            self.history.tag_configure(code, font=f)  # font by language
        self.history.tag_configure("src", foreground="#444444")  # colour by role
        self.history.tag_configure("tgt", foreground="#000000")
        self.history.tag_configure("arrow", foreground="#888888")
        self.history.tag_configure("note", font=self.f_small, foreground="#1f5fa8")

        bar = ttk.Frame(r, padding=(10, 4))
        bar.pack(fill="x")
        self.status_label = tk.Label(bar, text="Ready", fg=STATUS_COLORS["idle"], font=self.f_small,
                                     anchor="w", justify="left")
        self.status_label.pack(side="left", fill="x", expand=True)
        self.queue_label = tk.Label(bar, text="Voice queue: 0", font=self.f_small)
        self.queue_label.pack(side="right", padx=(12, 0))
        self.usage_label = tk.Label(r, text="", font=self.f_small, anchor="w", padx=10)
        self.usage_label.pack(fill="x", pady=(0, 6))

    def _build_turn_panel(self, r):
        """Onsite only: who has the floor, and what the device is doing."""
        f = self.turn_frame = tk.Frame(r, padx=10, pady=4)
        row = tk.Frame(f)
        row.pack(fill="x")
        big = tkfont.Font(family="Leelawadee UI", size=15, weight="bold")
        self.floor_btns = {}
        for side, key in (("en", "F1"), ("th", "F2")):
            b = tk.Button(row, text=f"🎤  {SIDE_LABEL[side]}   [{key}]", font=big, relief="raised",
                          padx=14, pady=8, command=lambda s=side: self._give_floor(s))
            b.pack(side="left", padx=(0, 10))
            self.floor_btns[side] = b
        side_col = tk.Frame(row)
        side_col.pack(side="right", fill="y")
        tk.Button(side_col, text="↻  Replay / เล่นซ้ำ  [F3]", font=self.f_small, padx=8, pady=1,
                  command=self._replay_last).pack(side="top", fill="x")
        self.skip_btn = tk.Button(side_col, text="⏭  Skip voice / ข้าม  [Esc]", font=self.f_small,
                                  padx=8, pady=1, command=self._skip_voice)
        self.skip_btn.pack(side="top", fill="x", pady=(2, 0))
        self.auto_var = tk.BooleanVar(value=config.ONSITE_AUTO_ALTERNATE)
        ttk.Checkbutton(side_col, text="Auto-switch turns",
                        variable=self.auto_var, command=self._on_auto_alternate
                        ).pack(side="top", anchor="w", pady=(4, 0))
        self.phase_label = tk.Label(f, text="", font=tkfont.Font(family="Leelawadee UI", size=14),
                                    fg="white", bg="#555555", anchor="w", padx=12, pady=8)
        self.phase_label.pack(fill="x", pady=(6, 0))
        self._show_floor()

    def _on_caption_resize(self, event):
        w = max(200, event.width - 30)
        self.main_label.configure(wraplength=w)
        self.sub_label.configure(wraplength=w)
        self.warn_label.configure(wraplength=w)

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
                       "(e.g. headphones), tick 'Pause listening while voice speaks', or Mute voice.")
        elif in_dev.kind == "mic" and self.setting_var.get() == "onsite":
            msg = ("Onsite: the mic is muted while the voice speaks and for "
                   f"{config.ONSITE_TAIL_S:.1f} s after. Speech during that time is not heard; "
                   "wait for the green banner.")
        elif in_dev.kind == "mic" and not self.pause_var.get():
            msg = ("Mic mode: if the voice plays on speakers in the room, the mic will pick it "
                   "up. Use headphones, or tick 'Pause listening while voice speaks'.")
        self.warn_label.configure(text=msg, fg="#b00020" if "⚠" in msg else "#8a5a00")

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

    def on_start(self):
        if self.state is not State.IDLE:
            return
        in_dev = self._selected(self.in_combo, self._input_choices())
        out_dev = self._selected(self.out_combo, self.devices.outputs)
        if in_dev is None or out_dev is None:
            self.set_status("error", "Choose an input device and a voice output device first.")
            return
        if not self.sm.transition(State.STARTING):
            return
        self.start_btn.configure(state="disabled")  # immediately: no double start
        render_dev = next((d for d in self.devices.outputs if d.name == in_dev.render_name), None)
        self._set_device_widgets("disabled")
        self.stop_btn.configure(state="normal")
        self._error_shown = False
        self.set_status("connecting", "Checking Azure key and region…")
        muted = self.mute_var.get()  # read Tk vars on the Tk thread only
        onsite = self.setting_var.get() == "onsite"
        if onsite:
            self.mode_var.set(self.turn.mode)  # the floor decides the direction
        mode = self.mode_var.get()
        self._onsite_live = onsite
        self.echo = EchoFilter() if onsite else None
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
            voices = config.load_voices()
            config.segmentation_silence_ms()  # validate now: a bad value is a ConfigError
            config.preflight_check(creds)
            sess.check()
            self.usage.new_session()
            sess.transcript = Transcript(config.TRANSCRIPT_DIR, time.time())
            post(self._set_transcript, sess.transcript)
            # Registered first, so it is closed last (after TTS reported its outcomes).
            lat = LatencyLog(config.LOG_DIR / f"latency-{datetime.now():%Y%m%d-%H%M%S}.jsonl",
                             on_row=lambda row: post(self._show_latency, row))
            sess.latency = sess.own("latency", lat, lambda l: l.close())

            pump = AudioPump(is_paused=self._is_listening_paused,
                             buffer_s=config.SWITCH_BUFFER_S)
            sess.pump = sess.own("pump", pump, lambda p: p.stop())
            pump.start()

            tts = TtsQueue(
                creds,
                on_queue=lambda n, sk: post(self._show_queue, n, sk),
                on_spoken_chars=self._add_chars,
                on_error=lambda m: post(self.set_status, None, m),
                voices=voices,
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
            )
            # Registered before start(): stopping it is what aborts a pending connect.
            sess.controller = sess.own("controller", controller, lambda c: c.stop(),
                                       interrupt=controller.stop)
            # Direction may have changed while starting; set_mode is a no-op if not.
            post(self._sync_mode, sess)

            sess.check()
            capture = AudioCapture(self.pa)
            capture.start(in_dev, pump.feed, render_dev)
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
            post(self._fail, sess, f"Audio device error: {e}. Try another device or Refresh devices.")
            return
        except Exception as e:
            if sess.cancelled.is_set():
                return  # e.g. an SDK error caused by the concurrent stop
            post(self._fail, sess, f"Could not start: {e}")
            return
        post(self._started, sess)

    def _started(self, sess: Session):
        self.sm.transition(State.RUNNING)  # refused if Stop already happened

    def on_stop(self):
        self._begin_stop()

    def _begin_stop(self) -> bool:
        sess = self.session
        if sess is None or not self.sm.transition(State.STOPPING):
            return False  # idle, or already stopping: double Stop is a no-op
        sess.cancelled.set()
        self.stop_btn.configure(state="disabled")
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
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        if not self._error_shown:
            self.set_status("idle", "Stopped.")
        self._update_usage_label()
        self._last_delay = None
        self.queue_label.configure(text="Voice queue: 0", fg="#000000")

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
            self.turn_frame.pack(fill="x", before=self.cap_frame)
        else:
            if self.state is State.IDLE:
                self.rb_loop.configure(state="normal")
                self.pause_cb.configure(state="normal")
                for rb in self.mode_rbs:
                    rb.configure(state="normal")
            self.turn_frame.pack_forget()
        self._on_pause_opt()

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
        color = STATUS_COLORS.get(state, "#8a5a00")
        if state is not None and self._error_shown and state != "error":
            return  # keep the error visible until the next Start
        self.status_label.configure(text=message, fg=color)

    def _caption_fonts(self, src: str, tgt: str):
        return self.f_big.get(tgt, self.f_big["en"]), self.f_sub.get(src, self.f_sub["en"])

    def _show_interim(self, source_text: str, translated: str, src: str, tgt: str):
        big, sub = self._caption_fonts(src, tgt)
        self.main_label.configure(text=translated or "…", fg="#9a9a9a", font=big)
        self.sub_label.configure(text=source_text, fg="#9a9a9a", font=sub)

    def _show_final(self, seg: Segment):
        shown = time.monotonic()
        lat = self.latency
        echo = self.echo
        if echo is not None and echo.is_echo(seg.source_text, seg.source_lang):
            self._note(f"Ignored (sounded like the app's own voice): {seg.source_text}")
            if lat is not None:
                lat.ignored(seg, "echo")
            return
        big, sub = self._caption_fonts(seg.source_lang, seg.target_lang)
        self.main_label.configure(text=seg.translated_text, fg="#000000", font=big)
        self.sub_label.configure(text=seg.source_text, fg="#444444", font=sub)
        seg_id = len(self._segs) + 1
        self._segs[seg_id] = seg
        self._last_seg_id = seg_id
        h = self.history
        at_bottom = h.yview()[1] >= 0.999
        h.configure(state="normal")
        h.insert("end", "▶ ", ("replay", f"seg{seg_id}"))
        h.insert("end", f"[{datetime.fromtimestamp(seg.start):%H:%M:%S}] "
                        f"{seg.source_lang.upper()}→{seg.target_lang.upper()} ", "time")
        h.insert("end", seg.source_text, (seg.source_lang, "src"))
        h.insert("end", "  →  ", "arrow")
        h.insert("end", seg.translated_text + "\n", (seg.target_lang, "tgt"))
        h.configure(state="disabled")
        if at_bottom:
            h.see("end")
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
        self.history.configure(state="normal")
        self.history.insert("end", text + "\n", "note")
        self.history.configure(state="disabled")
        self.history.see("end")

    def _show_queue(self, waiting: int, skipped: int):
        text = f"Voice queue: {waiting}"
        if skipped:
            text += f"  (skipped {skipped} to keep up; ▶ in History replays them)"
        tts = self.tts
        behind = tts.behind_seconds() if tts is not None else 0.0
        warn = behind >= config.TTS_BEHIND_WARN_S
        if warn:
            text += f"  ⚠ voice is ~{behind:.0f} s behind"
        text += self._delay_text()
        self.queue_label.configure(text=text, fg="#b00020" if warn else "#000000")

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

    # ---------------------------------------------------------------- replay
    def _replay(self, seg_id: int) -> bool:
        seg = self._segs.get(seg_id)
        if seg is None:
            return False
        tts = self.tts
        if tts is None or self.state is not State.RUNNING:
            self.set_status(None, "Replay works while the translator is running.")
            return False
        return tts.replay(seg.translated_text, seg.target_lang, seg_id)

    def _replay_last(self):
        if self._last_seg_id is not None:
            self._replay(self._last_seg_id)

    def _on_history_click(self, event):
        idx = self.history.index(f"@{event.x},{event.y}")
        for tag in self.history.tag_names(idx):
            if tag.startswith("seg") and tag[3:].isdigit():
                self._replay(int(tag[3:]))
                return "break"
        return None

    def _add_chars(self, n: int):
        self.usage.session_chars += n  # int += is fine across threads here

    def _is_listening_paused(self) -> bool:
        """Feedback gate, checked per captured chunk. Always on in onsite mode."""
        tts = self.tts
        if tts is None or not (self._pause_while_speaking or self._onsite_live):
            return False
        return tts.is_busy()

    def _on_voice_playing(self, utt):
        """TTS thread: remember what is about to be said, for the echo filter."""
        echo = self.echo
        if echo is not None:
            echo.spoken(utt.text, utt.lang)

    # ---------------------------------------------------------------- onsite
    def _give_floor(self, side: str):
        if self.setting_var.get() != "onsite":
            return
        mode = self.turn.give_floor(side)
        self._show_floor()
        if mode is not None:
            self.mode_var.set(mode)
            self._on_mode_change()

    def _skip_voice(self):
        tts = self.tts
        if tts is not None:
            tts.skip()

    def _on_auto_alternate(self):
        self.turn.auto_alternate = self.auto_var.get()

    def _show_floor(self):
        for side, b in self.floor_btns.items():
            on = side == self.turn.side
            b.configure(bg="#1b7a2e" if on else "#e6e6e6", fg="white" if on else "#333333",
                        activebackground="#22943a" if on else "#d6d6d6",
                        relief="sunken" if on else "raised")

    def _onsite_tick(self):
        try:
            self._update_onsite()
        finally:
            self.root.after(100, self._onsite_tick)

    def _update_onsite(self):
        if self.setting_var.get() != "onsite":
            return
        sess = self.session
        tts, ctl = self.tts, (sess.controller if sess is not None else None)
        if self.state is not State.RUNNING or tts is None or ctl is None:
            self.phase_label.configure(text="Press Start, then the person speaking presses "
                                            "their button   ·   กด Start แล้วกดปุ่มของผู้พูด",
                                       bg="#555555")
            return
        tts_state = tts.state()
        mode = self.turn.poll(tts_state, ctl.is_switching)
        if mode is not None:  # auto-alternate passed the floor
            self._show_floor()
            self.mode_var.set(mode)
            self._on_mode_change()
        phase = phase_of(tts_state, ctl.is_switching)
        bg, text = PHASE_BANNER[phase]
        if isinstance(text, dict):
            text = text[self.turn.side]
        self.phase_label.configure(text=text, bg=bg)

    def _on_mute(self):
        if self.tts:
            self.tts.set_muted(self.mute_var.get())

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

    def _update_usage_label(self):
        u = self.usage
        text = (f"This session: {u.session_audio_s / 60:.1f} audio min, {u.session_chars:,} TTS chars   |   "
                f"This month (this app): {u.month_audio_s / 3600:.2f} / {config.F0_TRANSLATION_AUDIO_HOURS} h audio, "
                f"{u.month_chars:,} / {config.F0_NEURAL_TTS_CHARS:,} TTS chars (F0 free)")
        warns = u.warnings()
        if warns:
            text += "   ⚠ " + "; ".join(warns)
        self.usage_label.configure(text=text, fg="#b00020" if warns else "#555555")

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
    root = tk.Tk()
    App(root)
    root.mainloop()
