"""Thai text-to-speech queue (th-TH-NiwatNeural).

One worker thread synthesizes and plays sentences in order, so speech never
overlaps. When live speech outruns the voice, the oldest unspoken sentences
are dropped (they stay in the transcript) so audio can't drift further and
further behind.
"""
import threading
import time
from collections import deque
from typing import Callable, Optional

import azure.cognitiveservices.speech as speechsdk

from . import config
from .audio import Player

TTS_RATE = 24000


class TtsQueue:
    def __init__(self, creds: config.Credentials,
                 on_queue: Callable[[int, int], None],
                 on_spoken_chars: Callable[[int], None],
                 on_error: Callable[[str], None],
                 max_backlog: int = config.TTS_MAX_BACKLOG,
                 per_minute: int = config.F0_TTS_TRANSACTIONS_PER_MINUTE):
        self.creds = creds
        self.on_queue = on_queue  # (waiting, skipped_total)
        self.on_spoken_chars = on_spoken_chars
        self.on_error = on_error
        self.max_backlog = max_backlog
        self.per_minute = per_minute
        self._q: deque = deque()
        self._cv = threading.Condition()
        self._cancel = threading.Event()
        self._running = False
        self._muted = False
        self._speaking = False
        self._pause_until = 0.0
        self._sent = deque()  # monotonic times of recent synth requests
        self.skipped = 0
        self.player: Optional[Player] = None
        self._synth = None
        self._thread: Optional[threading.Thread] = None

    # ---- control --------------------------------------------------------
    def start(self, player: Player) -> None:
        cfg = speechsdk.SpeechConfig(subscription=self.creds.key, region=self.creds.region)
        cfg.speech_synthesis_voice_name = config.TTS_VOICE
        cfg.set_speech_synthesis_output_format(
            speechsdk.SpeechSynthesisOutputFormat.Raw24Khz16BitMonoPcm)
        self._synth = speechsdk.SpeechSynthesizer(speech_config=cfg, audio_config=None)
        self.player = player
        self._running = True
        self._cancel.clear()
        self.skipped = 0
        self._thread = threading.Thread(target=self._run, name="tts", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        with self._cv:
            self._running = False
            self._q.clear()
            self._cv.notify_all()
        self._cancel.set()
        if self._thread:
            self._thread.join(timeout=5)
        if self.player:
            self.player.close()
            self.player = None
        self._synth = None
        self._report()

    def set_muted(self, muted: bool) -> None:
        with self._cv:
            self._muted = muted
            if muted:
                self._q.clear()
                self._cancel.set()  # cut off the sentence being spoken
            self._cv.notify_all()
        self._report()

    def enqueue(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        with self._cv:
            if self._muted or not self._running:
                return
            self._q.append(text)
            while len(self._q) > self.max_backlog:
                self._q.popleft()
                self.skipped += 1
            self._cv.notify_all()
        self._report()

    def is_busy(self) -> bool:
        """True while speaking, plus a short echo tail afterwards."""
        return self._speaking or time.monotonic() < self._pause_until

    @property
    def waiting(self) -> int:
        return len(self._q)

    # ---- worker -----------------------------------------------------------
    def _report(self) -> None:
        self.on_queue(len(self._q), self.skipped)

    def _next(self) -> Optional[str]:
        with self._cv:
            while self._running and not self._q:
                self._cv.wait()
            # F0 allows 20 synthesis requests per 60 s.
            while self._running and self._q and len(self._sent) >= self.per_minute:
                wait = 60 - (time.monotonic() - self._sent[0])
                if wait <= 0:
                    self._sent.popleft()
                else:
                    self._cv.wait(wait)
            if not self._running or not self._q:
                return None
            text = self._q.popleft()
            self._sent.append(time.monotonic())
            while self._sent and time.monotonic() - self._sent[0] > 60:
                self._sent.popleft()
            self._cancel.clear()
        self._report()
        return text

    def _run(self) -> None:
        while self._running:
            text = self._next()
            if text is None:
                continue
            try:
                result = self._synth.speak_text_async(text).get()
            except Exception as e:
                self.on_error(f"TTS failed: {e}")
                continue
            if result.reason == speechsdk.ResultReason.Canceled:
                d = result.cancellation_details
                self.on_error(f"TTS error ({d.error_code.name}): {d.error_details}")
                if d.error_code in (speechsdk.CancellationErrorCode.AuthenticationFailure,
                                    speechsdk.CancellationErrorCode.Forbidden):
                    self.set_muted(True)
                continue
            if result.reason != speechsdk.ResultReason.SynthesizingAudioCompleted:
                continue
            self.on_spoken_chars(len(text))
            if self._cancel.is_set() or not self._running:
                continue
            self._speaking = True
            try:
                self.player.play(result.audio_data, TTS_RATE, self._cancel)
            except Exception as e:
                self.on_error(f"Audio output error: {e}")
            finally:
                self._speaking = False
                self._pause_until = time.monotonic() + config.PAUSE_TAIL_S
