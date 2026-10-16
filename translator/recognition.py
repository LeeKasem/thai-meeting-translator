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
- Retries are capped; a session that stayed up STABLE_SESSION_S resets the
  counter. Stop wakes the backoff wait and cancels the reconnect.
- The old recognizer is fully stopped before a new one opens (F0 allows one
  concurrent recognition connection).
"""
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional

import azure.cognitiveservices.speech as speechsdk

from . import config
from .audio import SDK_RATE, AudioPump

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
    english: str
    thai: str


class RecognitionController:
    def __init__(self, creds: config.Credentials, pump: AudioPump,
                 on_interim: Callable[[str, str], None],
                 on_final: Callable[[Segment], None],
                 on_status: Callable[[str, str], None],
                 on_fatal: Callable[[str], None]):
        self.creds = creds
        self.pump = pump
        self.on_interim = on_interim
        self.on_final = on_final
        self.on_status = on_status  # (state, message)
        self.on_fatal = on_fatal
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._gen = 0
        self._rec = None
        self._push = None
        self._conn = None
        self._stream_start: Dict[int, float] = {}
        self._connected_at: Dict[int, float] = {}
        self._handled_drop: set = set()
        self._ever_connected = False
        self._attempts = 0

    # ---- public -------------------------------------------------------
    def start(self) -> None:
        """Blocking; call from a worker thread."""
        self._stop.clear()
        self._ever_connected = False
        self._attempts = 0
        self.on_status("connecting", "Connecting to Azure Speech…")
        self._open()

    def stop(self) -> None:
        """Blocking; call from a worker thread. Safe during a reconnect."""
        self._stop.set()  # wakes a pending backoff wait
        with self._lock:
            self._gen += 1  # every callback after this is stale
            rec, push, conn = self._rec, self._push, self._conn
            self._rec = self._push = self._conn = None
        self.pump.set_sink(None)
        self._teardown(rec, push, conn)

    # ---- internals ----------------------------------------------------
    def _open(self) -> None:
        with self._lock:
            if self._stop.is_set():
                return
            self._gen += 1
            gen = self._gen
            tcfg = speechsdk.translation.SpeechTranslationConfig(
                subscription=self.creds.key, region=self.creds.region)
            tcfg.speech_recognition_language = config.SOURCE_LANGUAGE
            tcfg.add_target_language(config.TARGET_LANGUAGE)
            fmt = speechsdk.audio.AudioStreamFormat(
                samples_per_second=SDK_RATE, bits_per_sample=16, channels=1)
            push = speechsdk.audio.PushAudioInputStream(stream_format=fmt)
            rec = speechsdk.translation.TranslationRecognizer(
                translation_config=tcfg,
                audio_config=speechsdk.audio.AudioConfig(stream=push))
            rec.recognizing.connect(lambda e: self._on_recognizing(gen, e))
            rec.recognized.connect(lambda e: self._on_recognized(gen, e))
            rec.canceled.connect(lambda e: self._on_canceled(gen, e))
            rec.session_stopped.connect(lambda e: self._on_session_stopped(gen))
            conn = speechsdk.Connection.from_recognizer(rec)
            conn.connected.connect(lambda e: self._on_connected(gen))
            self._rec, self._push, self._conn = rec, push, conn
            self._stream_start[gen] = time.time()
        self.pump.set_sink(push)
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
        return gen == self._gen and not self._stop.is_set()

    def _on_connected(self, gen: int) -> None:
        if not self._current(gen):
            return
        self._connected_at[gen] = time.monotonic()
        self._ever_connected = True
        self.on_status("listening", "Listening")

    def _on_recognizing(self, gen: int, evt) -> None:
        if not self._current(gen):
            return
        r = evt.result
        self.on_interim(r.text, r.translations.get(config.TARGET_LANGUAGE, ""))

    def _on_recognized(self, gen: int, evt) -> None:
        if not self._current(gen):
            return
        r = evt.result
        if r.reason != speechsdk.ResultReason.TranslatedSpeech or not r.text.strip():
            return
        base = self._stream_start.get(gen, time.time())
        start = base + r.offset / 1e7
        self.on_final(Segment(start, start + r.duration / 1e7, r.text,
                              r.translations.get(config.TARGET_LANGUAGE, "")))

    def _on_canceled(self, gen: int, evt) -> None:
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
        with self._lock:
            if self._stop.is_set():
                return
            rec, push, conn = self._rec, self._push, self._conn
            self._rec = self._push = self._conn = None
        self.pump.set_sink(None)
        self._teardown(rec, push, conn)

        if self._attempts >= config.MAX_RECONNECT_ATTEMPTS:
            msg = f"Connection lost ({reason}). Gave up after " \
                  f"{config.MAX_RECONNECT_ATTEMPTS} reconnect attempts. Press Start to try again."
            if code == Code.TooManyRequests:
                msg += " (F0 allows one session at a time.)"
            self.on_fatal(msg)
            return
        delay = config.RECONNECT_BACKOFF_S[min(self._attempts, len(config.RECONNECT_BACKOFF_S) - 1)]
        self._attempts += 1
        self.on_status("reconnecting",
                       f"Connection lost ({reason}). Reconnecting in {delay}s "
                       f"(attempt {self._attempts}/{config.MAX_RECONNECT_ATTEMPTS})…")
        if self._stop.wait(delay):
            return  # Stop pressed during the wait
        try:
            self._open()
        except Exception as e:  # SDK object creation failed
            self.on_fatal(f"Reconnect failed: {e}")
