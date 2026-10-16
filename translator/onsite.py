"""Onsite mode: two people, one device (one mic, one speaker).

Turn-taking
    One side "has the floor" at a time. The floor decides the direction
    (English speaker -> en>th, Thai speaker -> th>en), so only that side's
    language is recognized and translated. The floor changes when someone
    presses their button, or, with auto-alternate, once the translation of
    the current speaker has been read out.

Feedback-loop protection (two layers)
    1. Gate: while the voice plays, and for a tail afterwards, the mic is
       replaced by silence (AudioPump checks this per captured chunk).
    2. Echo filter: if a final result still closely matches something the
       voice said recently, it is dropped as the app hearing itself.

Phases shown to both people:
    SWITCHING    floor changed, recognizer reopening (audio is buffered)
    LISTENING    mic open for the side with the floor
    TRANSLATING  a translation is queued or being synthesized; mic still open
    SPEAKING     the voice is playing; mic gated
    TAIL         voice just ended; mic still gated for the echo tail
"""
import difflib
import enum
import threading
import time
import unicodedata
from collections import deque
from typing import Callable, Optional

from . import config


class Phase(enum.Enum):
    SWITCHING = "switching"
    LISTENING = "listening"
    TRANSLATING = "translating"
    SPEAKING = "speaking"
    TAIL = "tail"


def mode_for(side: str) -> str:
    """The direction for the side that has the floor: speaker's language -> other."""
    mode = f"{side}>{config.other(side)}"
    config.direction(mode)  # validates
    return mode


def phase_of(tts_state: str, switching: bool) -> Phase:
    if tts_state == "speaking":
        return Phase.SPEAKING
    if tts_state == "tail":
        return Phase.TAIL
    if switching:
        return Phase.SWITCHING
    if tts_state == "pending":
        return Phase.TRANSLATING
    return Phase.LISTENING


class TurnTaker:
    """Who has the floor. Pure logic; the UI applies the returned modes."""

    def __init__(self, first_side: str = config.PAIR[0],
                 auto_alternate: bool = config.ONSITE_AUTO_ALTERNATE):
        mode_for(first_side)
        self.side = first_side
        self.auto_alternate = auto_alternate
        self._alternate_pending = False

    @property
    def mode(self) -> str:
        return mode_for(self.side)

    def give_floor(self, side: str) -> Optional[str]:
        """Returns the new mode if the floor changed, else None. A manual
        choice always cancels a pending auto-alternate."""
        mode_for(side)
        self._alternate_pending = False
        if side == self.side:
            return None
        self.side = side
        return self.mode

    def on_final(self, source_lang: str) -> None:
        """A sentence from the side with the floor was accepted for reading."""
        if self.auto_alternate and source_lang == self.side:
            self._alternate_pending = True

    def poll(self, tts_state: str, switching: bool) -> Optional[str]:
        """Call regularly. Returns a new mode when auto-alternate passes the
        floor: only once the voice has finished (incl. the echo tail)."""
        if self._alternate_pending and tts_state == "idle" and not switching:
            self._alternate_pending = False
            return self.give_floor(config.other(self.side))
        return None


def _norm(text: str) -> str:
    """Lowercase, drop punctuation/spaces/symbols (Thai has no word spaces)."""
    return "".join(ch for ch in text.lower()
                   if unicodedata.category(ch)[0] not in "PZSC")


class EchoFilter:
    """Remembers what the voice said; flags results that are the app hearing
    itself. Thread-safe: `spoken` is called from the TTS thread."""

    def __init__(self, similarity: float = config.ECHO_SIMILARITY,
                 window_s: float = config.ECHO_WINDOW_S,
                 clock: Callable[[], float] = time.monotonic):
        self.similarity = similarity
        self.window_s = window_s
        self._clock = clock
        self._lock = threading.Lock()
        self._recent: deque = deque(maxlen=20)  # (time, lang, normalized text)

    def spoken(self, text: str, lang: str) -> None:
        n = _norm(text)
        if n:
            with self._lock:
                self._recent.append((self._clock(), lang, n))

    def is_echo(self, text: str, lang: str) -> bool:
        n = _norm(text)
        if not n:
            return False
        now = self._clock()
        with self._lock:
            recent = [(t, l, s) for t, l, s in self._recent if now - t <= self.window_s]
        for _, l, s in recent:
            if l != lang:
                continue
            if difflib.SequenceMatcher(None, n, s).ratio() >= self.similarity:
                return True
            # A fragment of what was said (recognizer caught part of it), long
            # enough that it isn't just a common short word like "yes".
            if len(n) >= max(6, len(s) // 2) and n in s:
                return True
        return False
