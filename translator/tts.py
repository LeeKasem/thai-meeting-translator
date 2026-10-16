"""Text-to-speech queue: each utterance is read in the voice for its target
language (config.load_voices(); Niwat for Thai, Jenny for English by default).

One worker thread synthesizes and plays utterances one at a time, so speech
never overlaps.

Queue policy (what happens when speech arrives faster than it can be read):

  POLICY_CATCH_UP (meeting): keep at most `max_backlog` sentences waiting;
    beyond that the OLDEST waiting ones are dropped. Listening to a long
    meeting, being current matters more than hearing every sentence. Dropped
    sentences stay in captions, history and transcript, and can be replayed.

  POLICY_IN_ORDER (onsite conversation): nothing is ever dropped; every
    sentence is read, in the order it was said. To keep a backlog from
    building, waiting sentences in the same language are MERGED into one
    synthesis request (up to `merge_max_chars`) when the worker picks them
    up. One request instead of several saves per-request latency and the F0
    limit of 20 requests a minute. If the backlog still grows, the UI shows
    how far behind the voice is rather than silently skipping.

Replay: `replay()` puts an utterance at the FRONT of the queue (it plays
right after the current one; it doesn't cut it off). Replays are never
dropped or merged, and play even when the voice is muted, because the user
explicitly asked. Recently synthesized audio is cached (LRU), so replaying
something already spoken is immediate and uses no TTS quota.

Stop is fenced: after `stop()` returns nothing more is played, no callback
fires, and the output stream has been aborted (buffered audio discarded). A
synthesis request already in flight can't be interrupted from Python beyond
`stop_speaking_async()`, so its result is simply discarded when it arrives.

Latency: `on_timing(utterance, info)` reports, per utterance, the monotonic
times synth_start / synth_end / play_start, the output device latency, any
wait for the F0 rate limit, whether the audio was cached, and the outcome:
played | cut_off (skip/mute/stop) | muted | dropped (catch-up) | error.
Called from the worker (or the enqueueing thread for drops), never with the
queue lock held.
"""
import logging
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Tuple
from xml.sax.saxutils import escape, quoteattr

import azure.cognitiveservices.speech as speechsdk

from . import config
from .audio import Player

TTS_RATE = 24000
log = logging.getLogger(__name__)


POLICY_CATCH_UP = "catch_up"
POLICY_IN_ORDER = "in_order"

# Rough speaking rates, only for the "voice is N s behind" estimate.
CHARS_PER_SECOND = {"en": 15.0, "th": 12.0}


@dataclass(frozen=True)
class Utterance:
    text: str
    lang: str  # target language code; selects the voice
    seg_ids: Tuple[int, ...] = ()  # history segments this utterance reads
    replay: bool = False


def merge(items) -> Utterance:
    """One utterance reading `items` (same language) back to back."""
    return Utterance(" ".join(u.text for u in items), items[0].lang,
                     tuple(i for u in items for i in u.seg_ids))


def build_ssml(text: str, lang: str, voice: str) -> str:
    locale = config.LANGUAGES[lang].locale
    return (f"<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis' "
            f"xml:lang={quoteattr(locale)}><voice name={quoteattr(voice)}>"
            f"{escape(text)}</voice></speak>")


class TtsQueue:
    def __init__(self, creds: config.Credentials,
                 on_queue: Callable[[int, int], None],
                 on_spoken_chars: Callable[[int], None],
                 on_error: Callable[[str], None],
                 voices: Optional[Dict[str, str]] = None,
                 on_playing: Optional[Callable[[Utterance], None]] = None,
                 tail_s: float = config.PAUSE_TAIL_S,
                 policy: str = POLICY_CATCH_UP,
                 max_backlog: int = config.TTS_MAX_BACKLOG,
                 merge_max_chars: int = config.TTS_MERGE_MAX_CHARS,
                 cache_items: int = config.TTS_CACHE_ITEMS,
                 per_minute: int = config.F0_TTS_TRANSACTIONS_PER_MINUTE,
                 on_timing: Optional[Callable[[Utterance, dict], None]] = None):
        if policy not in (POLICY_CATCH_UP, POLICY_IN_ORDER):
            raise ValueError(f"unknown queue policy {policy!r}")
        self.creds = creds
        self.voices = dict(config.DEFAULT_VOICES if voices is None else voices)
        self.on_queue = on_queue  # (waiting, skipped_total)
        self.on_spoken_chars = on_spoken_chars
        self.on_error = on_error
        self.on_playing = on_playing  # called as playback of an utterance begins
        self.tail_s = tail_s
        self.policy = policy
        self.max_backlog = max_backlog
        self.merge_max_chars = merge_max_chars
        self.cache_items = cache_items
        self._cache: "OrderedDict[tuple, bytes]" = OrderedDict()  # (lang, voice, text) -> pcm
        self.merged = 0  # sentences folded into another request (in-order policy)
        self.per_minute = per_minute
        self.on_timing = on_timing
        self._rate_wait = 0.0  # seconds the current item waited for the rate limit
        self._q: deque = deque()
        self._cv = threading.Condition()
        self._cancel = threading.Event()
        self._running = False
        self._closed = False  # set once by stop(); fences playback and callbacks
        self._muted = False
        self._speaking = False
        self._pause_until = 0.0
        self._current: Optional[Utterance] = None  # taken from the queue, not finished
        self._sent = deque()  # monotonic times of recent synth requests
        self.skipped = 0
        self.player: Optional[Player] = None
        self._synth = None
        self._thread: Optional[threading.Thread] = None

    # ---- control --------------------------------------------------------
    def start(self, player: Player) -> None:
        """Single use: a TtsQueue belongs to one session. Owns `player`."""
        try:
            if self._closed or self._thread is not None:
                raise RuntimeError("TtsQueue can only be started once")
            self._synth = self._make_synth()
        except Exception:
            player.close()
            raise
        self.player = player
        self._running = True
        self._cancel.clear()
        self.skipped = 0
        self._thread = threading.Thread(target=self._run, name="tts", daemon=True)
        self._thread.start()

    def _make_synth(self):
        cfg = speechsdk.SpeechConfig(subscription=self.creds.key, region=self.creds.region)
        # The voice is chosen per utterance in SSML; this is only the fallback.
        cfg.speech_synthesis_voice_name = self.voices[config.PAIR[0]]
        cfg.set_speech_synthesis_output_format(
            speechsdk.SpeechSynthesisOutputFormat.Raw24Khz16BitMonoPcm)
        return speechsdk.SpeechSynthesizer(speech_config=cfg, audio_config=None)

    def interrupt(self) -> None:
        """Silence now and drop everything queued; fast, never blocks on the SDK."""
        with self._cv:
            self._running = False
            self._closed = True
            self._q.clear()
            self._cv.notify_all()
        self._cancel.set()
        synth = self._synth
        if synth is not None:
            try:
                synth.stop_speaking_async()  # best effort; result is discarded anyway
            except Exception:
                pass

    def stop(self, join_timeout: float = 5.0) -> None:
        """Idempotent. Returns with the output stream aborted and callbacks fenced."""
        self.interrupt()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=join_timeout)
            if t.is_alive():
                log.warning("TTS worker still waiting on a synthesis request; "
                            "its result will be discarded")
        player, self.player = self.player, None
        if player is not None:
            player.close()  # waits for an in-flight write, then aborts the stream
        self._synth = None

    def set_muted(self, muted: bool) -> None:
        cleared = []
        with self._cv:
            self._muted = muted
            if muted:
                cleared = list(self._q)
                self._q.clear()
                self._cancel.set()  # cut off the sentence being spoken
            self._cv.notify_all()
        for u in cleared:
            self._timing(u, {"outcome": "muted"})
        self._report()

    def enqueue(self, text: str, lang: str, seg_id: Optional[int] = None) -> bool:
        """Queue a live sentence. False if it won't be spoken (muted, stopped)."""
        text = text.strip()
        if not text:
            return False
        if lang not in self.voices:
            raise ValueError(f"no voice configured for {lang!r}")
        ids = () if seg_id is None else (seg_id,)
        dropped = []
        with self._cv:
            if self._muted or not self._running or self._closed:
                return False
            self._q.append(Utterance(text, lang, ids))
            if self.policy == POLICY_CATCH_UP:
                dropped = self._drop_oldest()
            self._cv.notify_all()
        for u in dropped:
            self._timing(u, {"outcome": "dropped"})
        self._report()
        return True

    def replay(self, text: str, lang: str, seg_id: Optional[int] = None) -> bool:
        """Play `text` next (after the current utterance). False if not running."""
        text = text.strip()
        if not text:
            return False
        if lang not in self.voices:
            raise ValueError(f"no voice configured for {lang!r}")
        ids = () if seg_id is None else (seg_id,)
        with self._cv:
            if not self._running or self._closed:
                return False
            # Several replays requested in a row keep their order.
            pos = 0
            while pos < len(self._q) and self._q[pos].replay:
                pos += 1
            self._q.insert(pos, Utterance(text, lang, ids, replay=True))
            self._cv.notify_all()
        self._report()
        return True

    def _drop_oldest(self) -> list:
        """Catch-up policy; call with the lock held. Replays are never dropped.
        Returns the dropped utterances."""
        live = [u for u in self._q if not u.replay]
        excess = len(live) - self.max_backlog
        if excess <= 0:
            return []
        drop = set(map(id, live[:excess]))
        self._q = deque(u for u in self._q if id(u) not in drop)
        self.skipped += excess
        return live[:excess]

    @property
    def pending_chars(self) -> int:
        q = list(self._q)
        cur = self._current
        return sum(len(u.text) for u in q) + (len(cur.text) if cur is not None else 0)

    def behind_seconds(self) -> float:
        """Rough length of everything still to be read."""
        q = list(self._q)
        if self._current is not None:
            q.append(self._current)
        return sum(len(u.text) / CHARS_PER_SECOND.get(u.lang, 13.0) for u in q)

    def skip(self) -> None:
        """Cut off the utterance being spoken; the queue carries on. The echo
        tail still applies, since the room may still ring with it."""
        self._cancel.set()

    def set_tail(self, seconds: float) -> None:
        self.tail_s = max(0.0, seconds)

    def is_busy(self) -> bool:
        """True while speaking, plus the echo tail afterwards (feedback gate)."""
        if self._closed:
            return False
        return self._speaking or time.monotonic() < self._pause_until

    def state(self) -> str:
        """'speaking' | 'tail' | 'pending' (queued or synthesizing) | 'idle'."""
        if self._closed:
            return "idle"
        if self._speaking:
            return "speaking"
        if time.monotonic() < self._pause_until:
            return "tail"
        if self._q or self._current is not None:
            return "pending"
        return "idle"

    @property
    def waiting(self) -> int:
        return len(self._q)

    # ---- worker -----------------------------------------------------------
    def _report(self) -> None:
        if not self._closed:
            self.on_queue(len(self._q), self.skipped)

    def _error(self, message: str) -> None:
        if not self._closed:
            self.on_error(message)

    def _timing(self, item: Utterance, info: dict) -> None:
        if self.on_timing is None or self._closed:
            return
        try:
            self.on_timing(item, info)
        except Exception:
            log.exception("on_timing callback failed")

    def _cache_key(self, item: Utterance) -> tuple:
        return (item.lang, self.voices[item.lang], item.text)

    def _take(self) -> Utterance:
        """Pop the next utterance; under in-order, fold following same-language
        sentences into it. Call with the lock held and the queue non-empty."""
        item = self._q.popleft()
        if self.policy != POLICY_IN_ORDER or item.replay:
            return item
        if self._cache_key(item) in self._cache:
            return item  # already have its audio: don't spoil the cache hit
        parts = [item]
        size = len(item.text)
        while self._q and not self._q[0].replay and self._q[0].lang == item.lang \
                and size + 1 + len(self._q[0].text) <= self.merge_max_chars:
            nxt = self._q.popleft()
            parts.append(nxt)
            size += 1 + len(nxt.text)
        if len(parts) > 1:
            self.merged += len(parts) - 1
            return merge(parts)
        return item

    def _next(self) -> Optional[Utterance]:
        with self._cv:
            while self._running and not self._q:
                self._cv.wait()
            # F0 allows 20 synthesis requests per 60 s. Cached audio needs no request.
            waited_from = None
            while (self._running and self._q and len(self._sent) >= self.per_minute
                   and self._cache_key(self._q[0]) not in self._cache):
                wait = 60 - (time.monotonic() - self._sent[0])
                if wait <= 0:
                    self._sent.popleft()
                else:
                    if waited_from is None:
                        waited_from = time.monotonic()
                    self._cv.wait(wait)
            self._rate_wait = 0.0 if waited_from is None else time.monotonic() - waited_from
            if not self._running or not self._q:
                return None
            item = self._take()
            self._current = item
            if self._cache_key(item) not in self._cache:
                self._sent.append(time.monotonic())
            while self._sent and time.monotonic() - self._sent[0] > 60:
                self._sent.popleft()
            self._cancel.clear()
        self._report()
        return item

    def _cached_audio(self, item: Utterance) -> Optional[bytes]:
        key = self._cache_key(item)
        with self._cv:
            pcm = self._cache.get(key)
            if pcm is not None:
                self._cache.move_to_end(key)
            return pcm

    def _remember(self, item: Utterance, pcm: bytes) -> None:
        if self.cache_items <= 0:
            return
        with self._cv:
            self._cache[self._cache_key(item)] = pcm
            self._cache.move_to_end(self._cache_key(item))
            while len(self._cache) > self.cache_items:
                self._cache.popitem(last=False)

    def _run(self) -> None:
        synth, player = self._synth, self.player  # stop() may clear the attributes
        while self._running:
            item = self._next()
            if item is None:
                continue
            try:
                self._speak(synth, player, item)
            finally:
                self._current = None

    def _synthesize(self, synth, item: Utterance) -> Optional[bytes]:
        ssml = build_ssml(item.text, item.lang, self.voices[item.lang])
        try:
            result = synth.speak_ssml_async(ssml).get()
        except Exception as e:
            self._error(f"TTS failed: {e}")
            return None
        if self._closed:
            return None  # stopped while synthesizing: discard, no callbacks
        if result.reason == speechsdk.ResultReason.Canceled:
            d = result.cancellation_details
            self._error(f"TTS error ({d.error_code.name}): {d.error_details}")
            if d.error_code in (speechsdk.CancellationErrorCode.AuthenticationFailure,
                                speechsdk.CancellationErrorCode.Forbidden):
                self.set_muted(True)
            return None
        if result.reason != speechsdk.ResultReason.SynthesizingAudioCompleted:
            return None
        self.on_spoken_chars(len(item.text))
        self._remember(item, result.audio_data)
        return result.audio_data

    def _speak(self, synth, player, item: Utterance) -> None:
        info = {"synth_start": time.monotonic(), "rate_wait_s": round(self._rate_wait, 3)}
        pcm = self._cached_audio(item)
        info["cached"] = pcm is not None
        if pcm is None:
            pcm = self._synthesize(synth, item)
            if pcm is None:
                self._timing(item, dict(info, outcome="error"))
                return
        info["synth_end"] = time.monotonic()
        info["voice_s"] = round(len(pcm) / (TTS_RATE * 2), 2)
        if self._closed:
            return
        if self._muted and not item.replay:
            self._timing(item, dict(info, outcome="muted"))
            return
        if self._cancel.is_set() or not self._running:
            self._timing(item, dict(info, outcome="cut_off"))
            return
        self._speaking = True  # closes the mic gate before any sound is produced
        if self.on_playing is not None and not self._closed:
            try:
                self.on_playing(item)
            except Exception:
                log.exception("on_playing callback failed")
        info["play_start"] = time.monotonic()
        info["output_latency"] = round(float(getattr(player, "output_latency", 0.0) or 0.0), 4)
        outcome = "played"
        try:
            if player.play(pcm, TTS_RATE, self._cancel) is False:
                outcome = "cut_off"  # it did start: its start latency still counts
        except Exception as e:
            outcome = "error"
            self._error(f"Audio output error: {e}")
        finally:
            self._speaking = False
            self._pause_until = time.monotonic() + self.tail_s
        self._timing(item, dict(info, outcome=outcome, play_end=time.monotonic()))
