"""TranslationRecognizer lifecycle: start, stop, error classification, reconnect.

Rules (lessons from the earlier caption web app):
- Every recognizer gets a generation number. Callbacks from an older
  generation are ignored, so a late event from a torn-down recognizer can't
  trigger anything.
- Wrong key/region (AuthenticationFailure, BadRequest, Forbidden) is fatal:
  clear error, no reconnect.
- A drop is handled once per generation. `canceled` and `session_stopped`
  both arrive for one drop; the second is ignored.
- Reconnect only happens if this Start already reached a live connection.
  Failing to connect at all is reported as an error.
- Retries back off (1 s .. 30 s) and, for long-form use, never give up on
  their own (MAX_RECONNECT_ATTEMPTS = None); a session that stayed up
  STABLE_SESSION_S resets the backoff. Stop wakes the backoff wait and
  cancels the reconnect. Each outage is reported once, when the connection
  is back, through `on_gap(lost_at_epoch, seconds)`.
- The old recognizer is fully stopped before a new one opens (F0 allows one
  concurrent recognition connection). Opening is serialized by `_open_lock`,
  shared by start, reconnect and direction switches.
- Direction: a manual mode (en>th / th>en) fixes the recognition locale and
  the single target. MODE_AUTO uses continuous language ID over the pair and
  both targets; each result's detected locale picks the source, and the
  target is the other language. Switching mode reopens the recognizer
  (the source locale can't be changed on a live one).
- A switch first *flushes* the old recognizer: its input stream is closed so
  Azure finalizes the last sentence, and results are still accepted until it
  reports end of stream (or SWITCH_FLUSH_TIMEOUT_S). Meanwhile the pump
  buffers the new speaker's audio and replays it into the new recognizer.
  So a turn change right after someone stops talking loses neither their
  last sentence nor the start of the next speaker's.
- Latency: every final Segment carries `timing` (monotonic timestamps: speech
  start/end recovered from the result's audio offset via the pump's
  StreamClock, first partial, final) for translator.latency.
"""
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional, Tuple

import azure.cognitiveservices.speech as speechsdk

from . import config
from .audio import SDK_RATE, AudioPump

log = logging.getLogger(__name__)
Code = speechsdk.CancellationErrorCode
FATAL_CODES = {
    Code.AuthenticationFailure: "Azure rejected the key (AuthenticationFailure). "
                                "Check AZURE_SPEECH_KEY and AZURE_SPEECH_REGION.",
    Code.BadRequest: "Azure rejected the request (BadRequest). Check "
                     "AZURE_SPEECH_REGION and the language settings.",
    Code.Forbidden: "Access denied (Forbidden). The free monthly quota may be "
                    "used up, or the resource is disabled.",
    Code.RuntimeError: "Speech SDK internal error.",
    Code.EmbeddedModelError: "Speech SDK model error.",
}


@dataclass
class Segment:
    start: float  # epoch seconds
    end: float
    source_text: str
    translated_text: str
    source_lang: str = "en"
    target_lang: str = "th"
    # Latency timestamps (see translator.latency); not part of equality.
    timing: Optional[dict] = field(default=None, compare=False, repr=False)

    def text_in(self, lang: str) -> str:
        if lang == self.source_lang:
            return self.source_text
        return self.translated_text if lang == self.target_lang else ""

    @property
    def english(self) -> str:
        return self.text_in("en")

    @property
    def thai(self) -> str:
        return self.text_in("th")


class RecognitionController:
    def __init__(self, creds: config.Credentials, pump: AudioPump,
                 on_interim: Callable[[str, str, str, str], None],
                 on_final: Callable[[Segment], None],
                 on_status: Callable[[str, str], None],
                 on_fatal: Callable[[str], None],
                 mode: str = config.DEFAULT_MODE,
                 on_gap: Optional[Callable[[float, float], None]] = None):
        config.direction(mode)  # validates
        self.creds = creds
        self.pump = pump
        self._mode = mode
        self._open_lock = threading.Lock()
        self.on_interim = on_interim
        self.on_final = on_final
        self.on_status = on_status  # (state, message)
        self.on_fatal = on_fatal
        self.on_gap = on_gap  # (epoch when the connection was lost, seconds offline)
        self._lost_at: Optional[Tuple[float, float]] = None  # (epoch, monotonic)
        self.gaps = 0
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._gen = 0
        self._rec = None
        self._push = None
        self._conn = None
        self._stream_start: Dict[int, float] = {}
        self._clocks: Dict[int, object] = {}  # gen -> StreamClock of its push stream
        self._first_interim: Dict[Tuple[int, int], float] = {}  # (gen, offset) -> time
        self._connected_at: Dict[int, float] = {}
        self._handled_drop: set = set()
        self._ever_connected = False
        self._attempts = 0
        self._flushing: Dict[int, threading.Event] = {}  # gen -> end of stream seen
        self._switching = False

    # ---- public -------------------------------------------------------
    def start(self) -> None:
        """Blocking; call from a worker thread. Single use: a controller that was
        stopped (even before start) stays stopped, so a Stop can't be undone."""
        if self._stop.is_set():
            return
        self._ever_connected = False
        self._attempts = 0
        self.on_status("connecting", "Connecting to Azure Speech…")
        with self._open_lock:
            self._open()

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def is_switching(self) -> bool:
        return self._switching

    def set_mode(self, mode: str) -> None:
        """Change direction; reopens a live recognizer on a worker thread.
        Before start, or during a reconnect wait, the next open just uses it."""
        config.direction(mode)
        with self._lock:
            if mode == self._mode:
                return
            self._mode = mode
            live = self._rec is not None and not self._stop.is_set()
            if live:
                self._switching = True
        if live:
            threading.Thread(target=self._switch, name="switch", daemon=True).start()

    def stop(self) -> None:
        """Blocking; call from a worker thread. Safe during a reconnect."""
        self._stop.set()  # wakes a pending backoff wait
        with self._lock:
            self._gen += 1  # every callback after this is stale
            rec, push, conn = self._rec, self._push, self._conn
            self._rec = self._push = self._conn = None
        self.pump.set_sink(None)
        self._teardown(rec, push, conn)
        # A switch or reconnect may be mid-open on another thread; once it
        # sees the stop flag it tears down what it opened. Wait for that so no
        # recognizer outlives stop().
        if self._open_lock.acquire(timeout=config.SWITCH_FLUSH_TIMEOUT_S + 5):
            self._open_lock.release()
        else:
            log.warning("recognizer open still in progress after stop()")

    # ---- internals ----------------------------------------------------
    def _switch(self) -> None:
        try:
            with self._open_lock:
                with self._lock:
                    if self._stop.is_set() or self._rec is None:
                        return  # stopped, or a reconnect will open with the new mode
                    gen = self._gen
                    rec, push, conn = self._rec, self._push, self._conn
                    self._rec = self._push = self._conn = None
                    ended = threading.Event()
                    self._flushing[gen] = ended
                    mode = self._mode
                self.pump.set_sink(None)  # the next speaker's audio is buffered meanwhile
                self.on_status("connecting", f"Switching to {config.mode_label(mode)}…")
                self._flush(push, ended)
                with self._lock:
                    self._flushing.pop(gen, None)
                    if self._gen == gen:
                        self._gen += 1  # anything later from it is stale
                self._teardown(rec, push, conn)
                try:
                    self._open()
                except Exception as e:
                    self.on_fatal(f"Could not switch language: {e}")
        finally:
            with self._lock:
                if not self._flushing:
                    self._switching = False

    def _flush(self, push, ended: threading.Event) -> None:
        """End the old input stream and wait (bounded) for its final results."""
        try:
            push.close()
        except Exception:
            return
        deadline = time.monotonic() + config.SWITCH_FLUSH_TIMEOUT_S
        while not ended.wait(0.05):
            if self._stop.is_set() or time.monotonic() >= deadline:
                if not self._stop.is_set():
                    log.info("switch: no end-of-stream within %.1fs; continuing",
                             config.SWITCH_FLUSH_TIMEOUT_S)
                return

    def _make_recognizer(self, mode: str, push):
        audio = speechsdk.audio.AudioConfig(stream=push)
        if mode == config.MODE_AUTO:
            # Continuous language ID with translation needs the v2 endpoint.
            tcfg = speechsdk.translation.SpeechTranslationConfig(
                subscription=self.creds.key,
                endpoint=f"wss://{self.creds.region}.stt.speech.microsoft.com"
                         "/speech/universal/v2")
            tcfg.set_property(speechsdk.PropertyId.SpeechServiceConnection_LanguageIdMode,
                              "Continuous")
            for code in config.PAIR:
                tcfg.add_target_language(code)
            auto = speechsdk.languageconfig.AutoDetectSourceLanguageConfig(
                languages=[config.LANGUAGES[c].locale for c in config.PAIR])
            self._tune(tcfg)
            return speechsdk.translation.TranslationRecognizer(
                translation_config=tcfg, auto_detect_source_language_config=auto,
                audio_config=audio)
        src, tgt = config.direction(mode)
        tcfg = speechsdk.translation.SpeechTranslationConfig(
            subscription=self.creds.key, region=self.creds.region)
        tcfg.speech_recognition_language = config.LANGUAGES[src].locale
        tcfg.add_target_language(tgt)
        self._tune(tcfg)
        return speechsdk.translation.TranslationRecognizer(
            translation_config=tcfg, audio_config=audio)

    @staticmethod
    def _tune(tcfg) -> None:
        """Optional end-of-speech silence (shorter = earlier finals, but long
        pauses split sentences). None keeps the service default."""
        ms = config.segmentation_silence_ms()
        if ms is not None:
            tcfg.set_property(speechsdk.PropertyId.Speech_SegmentationSilenceTimeoutMs, str(ms))

    @staticmethod
    def _detected(result) -> str:
        try:
            return result.properties.get(
                speechsdk.PropertyId.SpeechServiceConnection_AutoDetectSourceLanguageResult, "")
        except Exception:
            return ""

    @classmethod
    def _langs_for(cls, mode: str, result) -> Optional[Tuple[str, str]]:
        """(source, target) for one result, or None if auto-detect gave no
        usable language."""
        fixed = config.direction(mode)
        if fixed is not None:
            return fixed
        detected = cls._detected(result)
        src = config.lang_for_locale(detected)
        if src is None:
            if detected:
                log.info("auto-detect returned %r, not in the pair; ignored", detected)
            return None
        return src, config.other(src)

    def _open(self) -> None:
        """Call with `_open_lock` held."""
        with self._lock:
            if self._stop.is_set():
                return
            self._gen += 1
            gen = self._gen
            mode = self._mode
            fmt = speechsdk.audio.AudioStreamFormat(
                samples_per_second=SDK_RATE, bits_per_sample=16, channels=1)
            push = speechsdk.audio.PushAudioInputStream(stream_format=fmt)
            rec = self._make_recognizer(mode, push)
            rec.recognizing.connect(lambda e: self._on_recognizing(gen, mode, e))
            rec.recognized.connect(lambda e: self._on_recognized(gen, mode, e))
            rec.canceled.connect(lambda e: self._on_canceled(gen, e))
            rec.session_stopped.connect(lambda e: self._on_session_stopped(gen))
            conn = speechsdk.Connection.from_recognizer(rec)
            conn.connected.connect(lambda e: self._on_connected(gen, mode))
            self._rec, self._push, self._conn = rec, push, conn
            self._stream_start[gen] = time.time()
        clock = self.pump.set_sink(push)
        with self._lock:
            self._clocks[gen] = clock
            # Bounded over long sessions: keep only recent generations (the
            # flushing one may still deliver results).
            for d in (self._clocks, self._stream_start, self._connected_at):
                for old in [g for g in d if g < gen - 2]:
                    del d[old]
            self._handled_drop = {g for g in self._handled_drop if g >= gen - 2}
        rec.start_continuous_recognition_async().get()
        # Stop may have run while we were starting; don't leave an orphan.
        with self._lock:
            orphan = self._stop.is_set() or gen != self._gen
        if orphan:
            self._teardown(rec, push, conn)

    @staticmethod
    def _teardown(rec, push, conn) -> None:
        if rec is not None:
            try:
                rec.stop_continuous_recognition_async().get()
            except Exception:
                pass
        if push is not None:
            try:
                push.close()
            except Exception:
                pass
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    def _current(self, gen: int) -> bool:
        """The live recognizer (drops/connects count only for it)."""
        return gen == self._gen and gen not in self._flushing and not self._stop.is_set()

    def _accepts_results(self, gen: int) -> bool:
        """Live, or being flushed by a switch (its last sentence still counts)."""
        return not self._stop.is_set() and (gen == self._gen or gen in self._flushing)

    def _mark_ended(self, gen: int) -> bool:
        ended = self._flushing.get(gen)
        if ended is None:
            return False
        ended.set()
        return True

    def _on_connected(self, gen: int, mode: Optional[str] = None) -> None:
        if not self._current(gen):
            return
        self._connected_at[gen] = time.monotonic()
        self._ever_connected = True
        with self._lock:
            lost, self._lost_at = self._lost_at, None
        self.on_status("listening", f"Listening ({config.mode_label(mode or self._mode)})")
        if lost is not None:
            self.gaps += 1
            if self.on_gap is not None:
                self.on_gap(lost[0], time.monotonic() - lost[1])

    def _on_recognizing(self, gen: int, mode: str, evt) -> None:
        if not self._accepts_results(gen):
            return
        r = evt.result
        key = (gen, getattr(r, "offset", 0))
        if key not in self._first_interim:
            if len(self._first_interim) > 200:
                self._first_interim.clear()  # utterances that never got a final
            self._first_interim[key] = time.monotonic()
        langs = self._langs_for(mode, r)
        if langs is None:  # auto: language not decided yet
            self.on_interim(r.text, "", "", "")
            return
        src, tgt = langs
        self.on_interim(r.text, r.translations.get(tgt, ""), src, tgt)

    def _on_recognized(self, gen: int, mode: str, evt) -> None:
        now = time.monotonic()
        if not self._accepts_results(gen):
            return
        r = evt.result
        first = self._first_interim.pop((gen, getattr(r, "offset", 0)), None)
        if r.reason != speechsdk.ResultReason.TranslatedSpeech or not r.text.strip():
            return
        langs = self._langs_for(mode, r)
        if langs is None:
            return
        src, tgt = langs
        translated = r.translations.get(tgt, "")
        if not translated.strip():
            return
        base = self._stream_start.get(gen, time.time())
        start = base + r.offset / 1e7
        self.on_final(Segment(start, start + r.duration / 1e7, r.text, translated, src, tgt,
                              timing=self._timing(gen, mode, r, first, now)))

    def _timing(self, gen: int, mode: str, r, first: Optional[float], now: float) -> dict:
        clock = self._clocks.get(gen)
        off, dur = r.offset / 1e7, r.duration / 1e7
        t = {"final": now, "first_interim": first, "mode": mode,
             "offset_s": round(off, 3), "duration_s": round(dur, 3),
             "speech_start": None, "speech_end": None,
             "detected": self._detected(r) if mode == config.MODE_AUTO else None}
        if clock is not None:
            t["speech_start"] = clock.time_at(off)
            t["speech_end"] = clock.time_at(off + dur)
        return t

    def _on_canceled(self, gen: int, evt) -> None:
        if self._mark_ended(gen):
            return  # end of a flushed stream (or an error while flushing): not a drop
        if not self._current(gen):
            return
        if evt.reason != speechsdk.CancellationReason.Error:
            return  # EndOfStream / CancelledByUser come from our own stop
        code = evt.error_code
        details = (evt.error_details or "").strip()
        if code in FATAL_CODES:
            self._fatal(gen, f"{FATAL_CODES[code]}\n{details}")
        else:
            self._drop(gen, f"{code.name}: {details}" if details else code.name, code)

    def _on_session_stopped(self, gen: int) -> None:
        if self._mark_ended(gen):
            return
        if self._current(gen):
            self._drop(gen, "session ended unexpectedly", None)

    def _fatal(self, gen: int, message: str) -> None:
        with self._lock:
            if gen in self._handled_drop:
                return
            self._handled_drop.add(gen)
        self.on_fatal(message)

    def _drop(self, gen: int, reason: str, code) -> None:
        with self._lock:
            if gen in self._handled_drop or not self._current(gen):
                return  # second event for the same drop
            self._handled_drop.add(gen)
            connected_at = self._connected_at.get(gen)
            if connected_at and time.monotonic() - connected_at >= config.STABLE_SESSION_S:
                self._attempts = 0
            if not self._ever_connected:
                msg = f"Could not connect to Azure Speech ({reason})."
                if code == Code.TooManyRequests:
                    msg += " A Free (F0) resource allows only one session at a time; " \
                           "close other apps using this key."
                self.on_fatal(msg)
                return
        threading.Thread(target=self._reconnect, args=(reason, code),
                         name="reconnect", daemon=True).start()

    def _reconnect(self, reason: str, code) -> None:
        with self._open_lock:
            with self._lock:
                if self._stop.is_set():
                    return
                rec, push, conn = self._rec, self._push, self._conn
                self._rec = self._push = self._conn = None
                if self._lost_at is None:  # first drop of this outage
                    self._lost_at = (time.time(), time.monotonic())
            # Meanwhile the pump buffers the audio (RECONNECT_BUFFER_S) and
            # replays it into the next recognizer.
            self.pump.set_sink(None)
            self._teardown(rec, push, conn)

        cap = config.MAX_RECONNECT_ATTEMPTS
        while not self._stop.is_set():
            if cap is not None and self._attempts >= cap:
                msg = f"Connection lost ({reason}). Gave up after " \
                      f"{cap} reconnect attempts. Press Start to try again."
                if code == Code.TooManyRequests:
                    msg += " (F0 allows one session at a time.)"
                self.on_fatal(msg)
                return
            backoff = config.RECONNECT_BACKOFF_S
            delay = backoff[min(self._attempts, len(backoff) - 1)]
            self._attempts += 1
            of = f"/{cap}" if cap is not None else ""
            self.on_status("reconnecting",
                           f"Connection lost ({reason}). Reconnecting in {delay}s "
                           f"(attempt {self._attempts}{of})…")
            if self._stop.wait(delay):
                return  # Stop pressed during the wait
            try:
                with self._open_lock:
                    with self._lock:
                        if self._rec is not None:
                            return  # a direction switch already opened a new one
                    self._open()
                return  # a failure to connect comes back as canceled -> _drop
            except Exception as e:  # SDK object creation failed: try again
                log.warning("reconnect attempt failed: %s", e)
                reason = f"reconnect failed: {e}"
