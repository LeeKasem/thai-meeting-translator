"""Tkinter UI. All SDK/audio callbacks run on other threads and reach Tk only
through `post()`, which the Tk main loop drains every 50 ms."""
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
from .recognition import RecognitionController, Segment
from .transcript import Transcript
from .tts import TtsQueue
from .usage import Usage

STATUS_COLORS = {"idle": "#555555", "connecting": "#1f5fa8", "listening": "#1b7a2e",
                 "reconnecting": "#b36b00", "error": "#b00020", "stopping": "#555555"}


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.pa = pyaudio.PyAudio()
        self.usage = Usage()
        self._ui_q: "queue.Queue" = queue.Queue()
        self.state = "idle"  # idle | starting | running | stopping
        self._stop_requested = threading.Event()
        self._teardown_lock = threading.Lock()
        self._error_shown = False
        # Session objects (owned by worker threads while starting/stopping)
        self.capture: Optional[AudioCapture] = None
        self.pump: Optional[AudioPump] = None
        self.tts: Optional[TtsQueue] = None
        self.controller: Optional[RecognitionController] = None
        self.transcript: Optional[Transcript] = None
        # Plain mirrors of Tk vars, readable from worker threads
        self._pause_while_speaking = False
        self._last_usage_save = time.monotonic()

        self._setup_fonts()
        self._build()
        self.refresh_devices()
        self._update_usage_label()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(50, self._drain)
        root.after(1000, self._tick)

    # ---------------------------------------------------------------- layout
    def _setup_fonts(self):
        families = set(tkfont.families(self.root))
        thai = next((f for f in ("Leelawadee UI", "Leelawadee", "Tahoma") if f in families), "TkDefaultFont")
        self.f_thai_big = tkfont.Font(family=thai, size=26)
        self.f_en = tkfont.Font(family="Segoe UI", size=13)
        self.f_hist_th = tkfont.Font(family=thai, size=13)
        self.f_hist_en = tkfont.Font(family="Segoe UI", size=11)
        self.f_small = tkfont.Font(family="Segoe UI", size=10)

    def _build(self):
        r = self.root
        r.title("Meeting Translator: English → Thai")
        r.geometry("1000x760")
        r.minsize(760, 560)

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

        ttk.Label(ctl, text="Thai voice output:").grid(row=2, column=0, sticky="w", pady=2)
        self.out_combo = ttk.Combobox(ctl, state="readonly", width=60)
        self.out_combo.grid(row=2, column=1, sticky="we", pady=2)
        self.out_combo.bind("<<ComboboxSelected>>", lambda e: self._update_feedback_warning())
        self.refresh_btn = ttk.Button(ctl, text="Refresh devices", command=self.refresh_devices)
        self.refresh_btn.grid(row=1, column=2, rowspan=2, padx=8)
        ctl.columnconfigure(1, weight=1)

        btns = ttk.Frame(ctl)
        btns.grid(row=3, column=0, columnspan=4, sticky="we", pady=(6, 0))
        self.start_btn = ttk.Button(btns, text="▶ Start", command=self.on_start)
        self.stop_btn = ttk.Button(btns, text="■ Stop", command=self.on_stop, state="disabled")
        self.start_btn.pack(side="left")
        self.stop_btn.pack(side="left", padx=(6, 16))
        self.mute_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(btns, text="Mute voice (script only)", variable=self.mute_var,
                        command=self._on_mute).pack(side="left")
        self.pause_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(btns, text="Pause listening while voice speaks", variable=self.pause_var,
                        command=self._on_pause_opt).pack(side="left", padx=(12, 0))
        ttk.Button(btns, text="Export .srt", command=lambda: self.export(".srt")).pack(side="right")
        ttk.Button(btns, text="Export .txt", command=lambda: self.export(".txt")).pack(side="right", padx=6)

        self.warn_label = tk.Label(r, text="", fg="#b00020", font=self.f_small, anchor="w",
                                   justify="left")
        self.warn_label.pack(fill="x", padx=10)

        cap = tk.Frame(r, bg="white", highlightthickness=1, highlightbackground="#cccccc")
        cap.pack(fill="x", padx=10, pady=6)
        self.th_label = tk.Label(cap, text="กด Start เพื่อเริ่มแปล", font=self.f_thai_big, bg="white",
                                 fg="#999999", anchor="w", justify="left", height=3)
        self.th_label.pack(fill="x", padx=12, pady=(8, 0))
        self.en_label = tk.Label(cap, text="Press Start to begin.", font=self.f_en, bg="white",
                                 fg="#999999", anchor="w", justify="left", height=2)
        self.en_label.pack(fill="x", padx=12, pady=(0, 8))
        cap.bind("<Configure>", self._on_caption_resize)

        ttk.Label(r, text="History", font=self.f_small).pack(anchor="w", padx=10)
        self.history = ScrolledText(r, wrap="word", height=12, state="disabled", padx=8, pady=6)
        self.history.pack(fill="both", expand=True, padx=10)
        self.history.tag_configure("time", foreground="#888888", font=self.f_small)
        self.history.tag_configure("en", font=self.f_hist_en, foreground="#333333")
        self.history.tag_configure("arrow", foreground="#888888")
        self.history.tag_configure("th", font=self.f_hist_th, foreground="#000000")
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

    def _on_caption_resize(self, event):
        w = max(200, event.width - 30)
        self.th_label.configure(wraplength=w)
        self.en_label.configure(wraplength=w)

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
            msg = "No output device found for the Thai voice."
        elif in_dev.kind == "loopback" and in_dev.render_name == out_dev.name:
            if self.pause_var.get():
                msg = ("⚠ The Thai voice plays on the device being captured. Listening pauses "
                       "while it speaks, so meeting audio during that time is missed. "
                       "Headphones on a separate output are better.")
            else:
                msg = ("⚠ FEEDBACK RISK: the Thai voice plays on the same device being captured, "
                       "so the app will hear and re-translate its own voice. Pick another output "
                       "(e.g. headphones), tick 'Pause listening while voice speaks', or Mute voice.")
        elif in_dev.kind == "mic" and not self.pause_var.get():
            msg = ("Mic mode: if the Thai voice plays on speakers in the room, the mic will pick it "
                   "up. Use headphones, or tick 'Pause listening while voice speaks'.")
        self.warn_label.configure(text=msg, fg="#b00020" if "⚠" in msg else "#8a5a00")

    # ----------------------------------------------------------- start/stop
    def on_start(self):
        if self.state != "idle":
            return
        self.state = "starting"
        self.start_btn.configure(state="disabled")  # immediately: no double start
        in_dev = self._selected(self.in_combo, self._input_choices())
        out_dev = self._selected(self.out_combo, self.devices.outputs)
        if in_dev is None or out_dev is None:
            self.state = "idle"
            self.start_btn.configure(state="normal")
            self.set_status("error", "Choose an input device and a voice output device first.")
            return
        render_dev = next((d for d in self.devices.outputs if d.name == in_dev.render_name), None)
        self._set_device_widgets("disabled")
        self.stop_btn.configure(state="normal")
        self._stop_requested.clear()
        self._error_shown = False
        self.set_status("connecting", "Checking Azure key and region…")
        muted = self.mute_var.get()  # read Tk vars on the Tk thread only
        threading.Thread(target=self._start_worker, args=(in_dev, out_dev, render_dev, muted),
                         name="start", daemon=True).start()

    def _start_worker(self, in_dev: Device, out_dev: Device, render_dev: Optional[Device],
                      muted: bool):
        try:
            creds = config.load_credentials()
            config.preflight_check(creds)
        except config.ConfigError as e:
            self.post(self._fail, str(e))
            return
        except ConnectionError as e:
            self.post(self._fail, f"{e} Check your internet connection.")
            return
        if self._stop_requested.is_set():
            self.post(self._finish_stop)
            return
        try:
            self.usage.new_session()
            self.transcript = Transcript(config.TRANSCRIPT_DIR, time.time())
            self.pump = AudioPump(is_paused=self._is_listening_paused)
            self.tts = TtsQueue(
                creds,
                on_queue=lambda n, s: self.post(self._show_queue, n, s),
                on_spoken_chars=self._add_chars,
                on_error=lambda m: self.post(self.set_status, None, m),
            )
            self.tts.set_muted(muted)
            self.tts.start(Player(self.pa, out_dev))
            self.controller = RecognitionController(
                creds, self.pump,
                on_interim=lambda en, th: self.post(self._show_interim, en, th),
                on_final=lambda seg: self.post(self._show_final, seg),
                on_status=lambda st, m: self.post(self.set_status, st, m),
                on_fatal=lambda m: self.post(self._fail, m),
            )
            self.pump.start()
            self.capture = AudioCapture(self.pa)
            self.capture.start(in_dev, self.pump.feed, render_dev)
            self.post(self._note, f"Session started. Auto-saving to {self.transcript.txt_path}")
            self.controller.start()
        except OSError as e:
            self.post(self._fail, f"Audio device error: {e}. Try another device or Refresh devices.")
            return
        except Exception as e:
            self.post(self._fail, f"Could not start: {e}")
            return
        self.post(self._started)

    def _started(self):
        if self.state == "starting":
            self.state = "running"
        if self._stop_requested.is_set():
            self.on_stop()

    def on_stop(self):
        if self.state not in ("starting", "running"):
            return
        self._stop_requested.set()
        self.stop_btn.configure(state="disabled")
        if self.state == "starting" and self.controller is None:
            return  # start worker sees the flag and finishes the stop
        self.state = "stopping"
        if not self._error_shown:
            self.set_status("stopping", "Stopping…")
        threading.Thread(target=self._stop_worker, name="stop", daemon=True).start()

    def _stop_worker(self):
        self._teardown_session()
        self.post(self._finish_stop)

    def _teardown_session(self):
        with self._teardown_lock:
            if self.controller:
                self.controller.stop()
            if self.capture:
                self.capture.stop()
            if self.pump:
                self.pump.stop()
                self.usage.session_audio_s = self.pump.seconds_sent
            if self.tts:
                self.tts.stop()
            self.controller = self.capture = self.pump = self.tts = None
            self.usage.save()

    def _finish_stop(self):
        self.state = "idle"
        self._set_device_widgets("normal")
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        if not self._error_shown:
            self.set_status("idle", "Stopped.")
        self._update_usage_label()
        self.queue_label.configure(text="Voice queue: 0")

    def _fail(self, message: str):
        """Fatal error: show it, then tear down. Never reconnects."""
        self._error_shown = True
        self.set_status("error", message)
        if self.state in ("starting", "running"):
            self.state = "stopping"
            self._stop_requested.set()
            self.stop_btn.configure(state="disabled")
            threading.Thread(target=self._stop_worker, name="stop", daemon=True).start()

    def _set_device_widgets(self, state):
        combo_state = "readonly" if state == "normal" else "disabled"
        self.in_combo.configure(state=combo_state)
        self.out_combo.configure(state=combo_state)
        for w in (self.rb_loop, self.rb_mic, self.refresh_btn):
            w.configure(state=state)

    # -------------------------------------------------------- live updates
    def post(self, fn, *args):
        self._ui_q.put((fn, args))

    def _drain(self):
        try:
            while True:
                fn, args = self._ui_q.get_nowait()
                fn(*args)
        except queue.Empty:
            pass
        self.root.after(50, self._drain)

    def set_status(self, state: Optional[str], message: str):
        color = STATUS_COLORS.get(state, "#8a5a00")
        if state is not None and self._error_shown and state != "error":
            return  # keep the error visible until the next Start
        self.status_label.configure(text=message, fg=color)

    def _show_interim(self, en: str, th: str):
        self.th_label.configure(text=th or "…", fg="#9a9a9a")
        self.en_label.configure(text=en, fg="#9a9a9a")

    def _show_final(self, seg: Segment):
        self.th_label.configure(text=seg.thai, fg="#000000")
        self.en_label.configure(text=seg.english, fg="#444444")
        h = self.history
        at_bottom = h.yview()[1] >= 0.999
        h.configure(state="normal")
        h.insert("end", f"[{datetime.fromtimestamp(seg.start):%H:%M:%S}] ", "time")
        h.insert("end", seg.english, "en")
        h.insert("end", "  →  ", "arrow")
        h.insert("end", seg.thai + "\n", "th")
        h.configure(state="disabled")
        if at_bottom:
            h.see("end")
        if self.transcript:
            try:
                self.transcript.add(seg)
            except OSError as e:
                self.set_status(None, f"Auto-save failed: {e}")
        if self.tts:
            self.tts.enqueue(seg.thai)

    def _note(self, text: str):
        self.history.configure(state="normal")
        self.history.insert("end", text + "\n", "note")
        self.history.configure(state="disabled")
        self.history.see("end")

    def _show_queue(self, waiting: int, skipped: int):
        text = f"Voice queue: {waiting}"
        if skipped:
            text += f"  (skipped {skipped} to keep up)"
        self.queue_label.configure(text=text)

    def _add_chars(self, n: int):
        self.usage.session_chars += n  # int += is fine across threads here

    def _is_listening_paused(self) -> bool:
        tts = self.tts
        return self._pause_while_speaking and tts is not None and tts.is_busy()

    def _on_mute(self):
        if self.tts:
            self.tts.set_muted(self.mute_var.get())

    def _on_pause_opt(self):
        self._pause_while_speaking = self.pause_var.get()
        self._update_feedback_warning()

    def _tick(self):
        if self.pump:
            self.usage.session_audio_s = self.pump.seconds_sent
        self._update_usage_label()
        if time.monotonic() - self._last_usage_save > 30 and self.state == "running":
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
        if self.state in ("starting", "running", "stopping"):
            self._stop_requested.set()
            self._teardown_session()
        try:
            self.usage.save()
        except OSError:
            pass
        self.pa.terminate()
        self.root.destroy()


def run():
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # crisp text on high-DPI screens
    except Exception:
        pass
    root = tk.Tk()
    App(root)
    root.mainloop()
