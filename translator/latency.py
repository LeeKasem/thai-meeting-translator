"""Per-stage latency: timestamps for every utterance, from the end of speech
to the start of the spoken translation.

Stages (all times are time.monotonic(); "speech end" is when the last audio
of the utterance was *captured*, recovered from Azure's audio offset through
the StreamClock, so buffering during turn switches is accounted for):

  speech_start ──► first_interim                 caption responsiveness
  speech_end   ──► final       recognize_s       Azure end-of-speech + translation
  final        ──► shown       ui_s              Tk queue + drawing
  shown        ──► synth_start queue_s           waiting behind earlier speech (+ F0 rate limit)
  synth_start  ──► synth_end   synth_s           Azure TTS (whole clip; 0 if cached)
  synth_end    ──► play_start  play_s            conversion + output buffer latency
  speech_end   ──► shown       caption_s         speech end -> translated text on screen
  speech_end   ──► play_start  total_s           speech end -> translated voice starts

`LatencyLog` collects these per history segment and writes one JSON line per
utterance to logs/latency-*.jsonl once its voice outcome is known (played,
cut off, dropped, muted...). The app log omits the text (meeting content);
the measurement harness includes it.
"""
import bisect
import json
import logging
import statistics
import threading
import time
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional

log = logging.getLogger(__name__)
BYTES_PER_S = 16000 * 2  # audio.SDK_RATE, 16-bit mono (audio imports this module)

STAGES = [
    ("first_interim_s", "Speech start → first partial caption"),
    ("recognize_s", "Speech end → final text (Azure)"),
    ("ui_s", "Final text → shown on screen"),
    ("caption_s", "Speech end → translated text on screen"),
    ("queue_s", "Shown → TTS request (waiting in queue)"),
    ("rate_wait_s", "  of which: F0 20/min rate limit"),
    ("synth_s", "TTS synthesis (Azure)"),
    ("play_s", "Audio ready → sound starts"),
    ("total_s", "Speech end → translated voice starts"),
]


class StreamClock:
    """Maps a position in one push stream (seconds of 16 kHz audio) to the
    monotonic time that audio was captured. Fed by the pump, one entry per
    chunk written; keeps the most recent `keep_s` of audio."""

    def __init__(self, keep_s: float = 600.0):
        self._lock = threading.Lock()
        self._ends: List[int] = []    # cumulative byte position at each chunk's end
        self._times: List[float] = []  # capture time of each chunk's last sample
        self._base = 0  # byte position where the first retained chunk starts
        self._pos = 0
        self._keep = int(keep_s * BYTES_PER_S)

    def add(self, nbytes: int, captured_at: float) -> None:
        if nbytes <= 0:
            return
        with self._lock:
            self._pos += nbytes
            self._ends.append(self._pos)
            self._times.append(captured_at)
            if self._pos - self._base > 2 * self._keep:
                cut = bisect.bisect_left(self._ends, self._pos - self._keep)
                self._base = self._ends[cut - 1] if cut else self._base
                del self._ends[:cut], self._times[:cut]

    @property
    def seconds(self) -> float:
        return self._pos / BYTES_PER_S

    def time_at(self, pos_s: float) -> Optional[float]:
        """Capture time of the audio at `pos_s`, or None if not (or no longer) known."""
        pos = pos_s * BYTES_PER_S
        with self._lock:
            if pos < self._base or pos > self._pos or not self._ends:
                return None
            i = bisect.bisect_left(self._ends, pos)
            return self._times[i] - (self._ends[i] - pos) / BYTES_PER_S


def _d(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None or b is None:
        return None
    return round(b - a, 3)


def stages(t: dict) -> dict:
    """Stage durations (seconds) from one record's raw timestamps."""
    out_lat = t.get("output_latency") or 0.0
    audible = t["play_start"] + out_lat if t.get("play_start") is not None else None
    return {
        "first_interim_s": _d(t.get("speech_start"), t.get("first_interim")),
        "recognize_s": _d(t.get("speech_end"), t.get("final")),
        "ui_s": _d(t.get("final"), t.get("shown")),
        "caption_s": _d(t.get("speech_end"), t.get("shown")),
        "queue_s": _d(t.get("shown"), t.get("synth_start")),
        "rate_wait_s": t.get("rate_wait_s"),
        "synth_s": _d(t.get("synth_start"), t.get("synth_end")),
        "play_s": _d(t.get("synth_end"), audible),
        "total_s": _d(t.get("speech_end"), audible),
    }


class LatencyLog:
    """Thread-safe. `recognized()` from the Tk thread, `tts()` from the TTS
    worker; a row is emitted when both halves are in (or at close())."""

    def __init__(self, path: Optional[Path] = None, include_text: bool = False,
                 on_row: Optional[Callable[[dict], None]] = None):
        self.path = path
        self.include_text = include_text
        self.on_row = on_row
        self.rows: List[dict] = []
        self._open: Dict[int, dict] = {}
        self._lock = threading.Lock()
        self._fh = None
        self._closed = False
        if path is not None:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                self._fh = open(path, "a", encoding="utf-8")
            except OSError as e:  # logging must never stop a session
                log.warning("latency log disabled: %s", e)

    def recognized(self, seg_id: int, seg, shown: float, enqueued: bool) -> None:
        t = dict(getattr(seg, "timing", None) or {})
        t["shown"] = shown
        rec = {"seg": seg_id, "kind": "speech", "at": time.time(),
               "src": seg.source_lang, "tgt": seg.target_lang,
               "mode": t.pop("mode", None), "detected": t.pop("detected", None),
               "audio_s": round(seg.end - seg.start, 2),
               "src_chars": len(seg.source_text), "tgt_chars": len(seg.translated_text),
               "_t": t}
        if self.include_text:
            rec["src_text"], rec["tgt_text"] = seg.source_text, seg.translated_text
        with self._lock:
            if self._closed:
                return
            if not enqueued:
                rec["outcome"] = "not_voiced"
                self._emit(rec)
            else:
                self._open[seg_id] = rec

    def unvoiced(self, seg_id: int, reason: str) -> None:
        """A segment that was registered for voice but won't be spoken."""
        with self._lock:
            rec = self._open.pop(seg_id, None)
            if rec is not None and not self._closed:
                rec["outcome"] = reason
                self._emit(rec)

    def ignored(self, seg, reason: str) -> None:
        """A final result the app dropped (e.g. the echo filter)."""
        t = dict(getattr(seg, "timing", None) or {})
        rec = {"seg": None, "kind": "ignored", "at": time.time(), "outcome": reason,
               "src": seg.source_lang, "tgt": seg.target_lang,
               "mode": t.pop("mode", None), "detected": t.pop("detected", None),
               "audio_s": round(seg.end - seg.start, 2), "_t": t}
        if self.include_text:
            rec["src_text"], rec["tgt_text"] = seg.source_text, seg.translated_text
        with self._lock:
            if not self._closed:
                self._emit(rec)

    def tts(self, utt, timing: dict) -> None:
        """Voice outcome for an utterance (one or several merged segments)."""
        with self._lock:
            if self._closed:
                return
            if getattr(utt, "replay", False):
                rec = {"seg": utt.seg_ids[0] if utt.seg_ids else None, "kind": "replay",
                       "at": time.time(), "tgt": utt.lang, "tgt_chars": len(utt.text),
                       "outcome": timing.get("outcome"), "_t": dict(timing)}
                self._emit(rec)
                return
            ids = list(utt.seg_ids)
            for sid in ids:
                rec = self._open.pop(sid, None)
                if rec is None:
                    continue
                rec["outcome"] = timing.get("outcome")
                rec["cached"] = timing.get("cached", False)
                if len(ids) > 1:
                    rec["merged_with"] = [i for i in ids if i != sid]
                rec["_t"].update({k: v for k, v in timing.items()
                                  if k not in ("outcome", "cached")})
                self._emit(rec)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            for rec in self._open.values():
                rec["outcome"] = "not_spoken"  # still queued (or synthesizing) at Stop
                self._emit(rec)
            self._open.clear()
            self._closed = True
            if self._fh is not None:
                self._fh.close()
                self._fh = None

    def _emit(self, rec: dict) -> None:
        """Call with the lock held."""
        t = rec.pop("_t")
        rec.update(stages(t))
        rec["t"] = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in t.items()}
        self.rows.append(rec)
        if self._fh is not None:
            try:
                self._fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                self._fh.flush()
            except OSError as e:
                log.warning("latency log write failed: %s", e)
        if rec["kind"] == "speech":
            log.info("latency seg %s %s>%s %s: %s", rec["seg"], rec["src"], rec["tgt"],
                     rec["outcome"], "  ".join(
                         f"{k[:-2]}={rec[k]:.2f}" for k, _ in STAGES
                         if rec.get(k) is not None and k != "rate_wait_s"))
        if self.on_row is not None:
            try:
                self.on_row(rec)
            except Exception:
                log.exception("latency on_row callback failed")


# ---------------------------------------------------------------- reporting
def percentile(xs: List[float], p: float) -> float:
    """Linear-interpolated percentile, p in [0, 100]."""
    s = sorted(xs)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * p / 100
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def stage_table(rows: Iterable[dict]) -> str:
    """Markdown table: n / median / p90 / max per stage, over spoken utterances."""
    rows = [r for r in rows if r.get("kind") == "speech"]
    lines = ["| Stage | n | median | p90 | max |", "|---|---:|---:|---:|---:|"]
    for key, label in STAGES:
        xs = [r[key] for r in rows if r.get(key) is not None]
        if not xs:
            lines.append(f"| {label} | 0 | – | – | – |")
            continue
        lines.append(f"| {label} | {len(xs)} | {statistics.median(xs):.2f} s | "
                     f"{percentile(xs, 90):.2f} s | {max(xs):.2f} s |")
    return "\n".join(lines)


def load_rows(paths: Iterable[Path]) -> List[dict]:
    rows = []
    for p in paths:
        with open(p, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    return rows


def outcome_counts(rows: Iterable[dict]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for r in rows:
        if r.get("kind") == "speech":
            out[r["outcome"]] = out.get(r["outcome"], 0) + 1
    return out
