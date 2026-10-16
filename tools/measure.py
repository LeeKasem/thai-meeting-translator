"""Measure language-detection accuracy and per-stage latency with real Azure,
mic and speaker.

  python -m tools.measure --list-devices
  python -m tools.measure --source speaker               # a TTS voice reads the prompts
                                                         # aloud; the mic listens (room path)
  python -m tools.measure --source synthetic             # prompts pushed straight into
                                                         # the recognizer (no acoustics)
  python -m tools.measure --source human                 # you read the prompts aloud
  python -m tools.measure --mode floor                   # direction set per phrase, like
                                                         # pressing the onsite floor buttons
  options: --mic NAME  --out NAME  --silence-ms 500  --limit 10  --no-voice

It runs the app's own pipeline (AudioPump, RecognitionController, TtsQueue,
Player) without the window, plays each phrase from tools/phrases.tsv, waits
for its translation to be spoken, and writes
results/measure-<time>-<source>-<mode>/ with summary.md, phrases.csv,
rows.jsonl (one latency row per recognized segment) and run.log.

Speech end: from the recognizer (Azure's audio offset mapped to capture
time, as in the app). For synthetic/speaker sources the true end of the
prompt audio is also known; summary.md compares the two as a sanity check.
"""
import argparse
import csv
import difflib
import hashlib
import itertools
import logging
import os
import statistics
import sys
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

from translator import config
from translator.latency import LatencyLog, outcome_counts, percentile, stage_table
from translator.onsite import mode_for

RESULTS_DIR = config.APP_DIR / "results"
PHRASES = Path(__file__).with_name("phrases.tsv")
# Voices that read the prompts: deliberately not the translation voices.
PROMPT_VOICES = {"en": "en-US-GuyNeural", "th": "th-TH-PremwadeeNeural"}
PROMPT_RATE = 16000
CHUNK_S = 0.1
SETTLE_S = 1.2            # quiet time after the last result before the next phrase
RESULT_TIMEOUT_S = 12.0   # no result this long after the phrase ended: missed
HUMAN_TIMEOUT_S = 20.0
CONNECT_TIMEOUT_S = 20.0
MODES = ("auto", "floor", config.MODE_EN_TH, config.MODE_TH_EN)


# ------------------------------------------------------------------ phrases
@dataclass(frozen=True)
class Phrase:
    id: str
    lang: str
    category: str
    text: str


def load_phrases(path: Path) -> List[Phrase]:
    out, seen = [], set()
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 4:
            raise ValueError(f"{path.name}:{n}: expected id<TAB>lang<TAB>category<TAB>text")
        pid, lang, cat, text = (p.strip() for p in parts[:4])
        if lang not in config.LANGUAGES:
            raise ValueError(f"{path.name}:{n}: unknown language {lang!r}")
        if pid in seen:
            raise ValueError(f"{path.name}:{n}: duplicate id {pid!r}")
        seen.add(pid)
        out.append(Phrase(pid, lang, cat, text))
    return out


def norm(text: str) -> str:
    """Letters, combining marks (Thai vowels/tones) and digits, case-folded."""
    t = unicodedata.normalize("NFC", text).casefold()
    return "".join(c for c in t if unicodedata.category(c)[0] in "LMN")


def similarity(a: str, b: str) -> float:
    a, b = norm(a), norm(b)
    if not a and not b:
        return 1.0
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


@dataclass
class Trial:
    phrase: Phrase
    t_prompt: float = 0.0             # phrase started (monotonic)
    ref_end: Optional[float] = None   # its audio really ended (synthetic/speaker)
    switch_s: Optional[float] = None  # floor mode: direction switch before it
    segs: list = field(default_factory=list)  # [(seg_id, Segment)]

    @property
    def detected(self) -> Optional[str]:
        """Language it was recognized as: majority by characters."""
        weight: Dict[str, int] = {}
        for _, s in self.segs:
            weight[s.source_lang] = weight.get(s.source_lang, 0) + max(1, len(s.source_text))
        return max(weight, key=weight.get) if weight else None

    @property
    def recognized(self) -> str:
        return " ".join(s.source_text for _, s in self.segs)

    @property
    def translation(self) -> str:
        return " ".join(s.translated_text for _, s in self.segs)


# ------------------------------------------------------------------ runner
class Runner:
    """Presents phrases one by one and attributes each final result to one."""

    def __init__(self, phrases: List[Phrase], mode: str, source, lat: LatencyLog,
                 tts=None, out: Callable[[str], None] = print,
                 settle_s: float = SETTLE_S, timeout_s: float = RESULT_TIMEOUT_S):
        self.trials = [Trial(p) for p in phrases]
        self.mode = mode
        self.source = source
        self.lat = lat
        self.tts = tts
        self.out = out
        self.settle_s = settle_s
        self.timeout_s = timeout_s
        self.controller = None
        self.stop = threading.Event()
        self.connected = threading.Event()
        self.fatal_msg: Optional[str] = None
        self.stray: list = []  # results that arrived with no phrase active
        self._lock = threading.Lock()
        self._cur: Optional[int] = None
        self._last_result = 0.0
        self._ids = itertools.count(1)

    # ---- callbacks (SDK / TTS threads)
    def on_status(self, state: str, message: str) -> None:
        if state == "listening":
            self.connected.set()
        self.out(f"   ({message})")

    def on_fatal(self, message: str) -> None:
        self.fatal_msg = message
        self.out(f"FATAL: {message}")
        self.stop.set()

    def on_final(self, seg) -> None:
        now = time.monotonic()
        sid = next(self._ids)
        with self._lock:
            idx = self._assign(seg)
            if idx is None:
                self.stray.append((sid, seg))
            else:
                self.trials[idx].segs.append((sid, seg))
            self._last_result = now
        voiced = self.tts is not None
        self.lat.recognized(sid, seg, shown=now, enqueued=voiced)
        if voiced and not self.tts.enqueue(seg.translated_text, seg.target_lang, sid):
            self.lat.unvoiced(sid, "muted")
        self.out(f"   -> [{seg.source_lang}] {seg.source_text}  =>  {seg.translated_text}")

    def _assign(self, seg) -> Optional[int]:
        """Call with the lock held. A result whose speech began before the
        current prompt belongs to the previous phrase (its late tail)."""
        cur = self._cur
        if cur is None:
            return None
        start = (seg.timing or {}).get("speech_start")
        if start is not None and cur > 0 and start < self.trials[cur].t_prompt - 0.3:
            return cur - 1
        return cur

    # ---- main loop
    def wait_connected(self, timeout: float = CONNECT_TIMEOUT_S) -> bool:
        end = time.monotonic() + timeout
        while not self.connected.is_set() and not self.stop.is_set():
            if time.monotonic() >= end:
                return False
            self.connected.wait(0.1)
        return self.connected.is_set()

    def run(self) -> None:
        n = len(self.trials)
        for i, trial in enumerate(self.trials):
            if self.stop.is_set():
                break
            p = trial.phrase
            if self.mode == "floor":
                trial.switch_s = self._set_floor(p.lang)
            self.out(f"[{i + 1}/{n}] {p.id} ({p.lang}, {p.category}): {p.text}")
            with self._lock:
                self._cur = i
                trial.t_prompt = time.monotonic()
            trial.ref_end = self.source.present(p)
            self._wait_done(trial)
        with self._lock:
            self._cur = None

    def _set_floor(self, lang: str) -> Optional[float]:
        c = self.controller
        mode = mode_for(lang)
        if c is None or c.mode == mode:
            return None
        t0 = time.monotonic()
        c.set_mode(mode)
        while c.is_switching and not self.stop.is_set() and time.monotonic() - t0 < 10:
            time.sleep(0.02)
        return round(time.monotonic() - t0, 3)

    def _tts_idle(self) -> bool:
        return self.tts is None or self.tts.state() == "idle"

    def _wait_done(self, trial: Trial) -> None:
        ended = trial.ref_end or trial.t_prompt
        human = trial.ref_end is None
        deadline = ended + (HUMAN_TIMEOUT_S if human else self.timeout_s)
        settle = self.settle_s * (1.5 if human else 1.0)
        while not self.stop.is_set():
            now = time.monotonic()
            with self._lock:
                have, last = bool(trial.segs), self._last_result
            if have and self._tts_idle() and now - max(last, ended) >= settle:
                return
            if not have and now >= deadline:
                self.out("   (no result)")
                return
            if have and now >= deadline + 20:
                self.out("   (voice did not finish; moving on)")
                return
            self.stop.wait(0.05)


# ------------------------------------------------------------------ sources
class HumanSource:
    """You read each phrase into the mic. Returns no reference end time."""

    def __init__(self, out=print):
        self.out = out

    def present(self, phrase: Phrase) -> None:
        self.out(f"\n   >>> PLEASE SAY ({config.LANGUAGES[phrase.lang].name}):\n"
                 f"   >>>     {phrase.text}\n")
        return None


class SyntheticSource:
    """Feeds prompt audio into the pump in real time, silence otherwise, like
    a capture device: each 100 ms chunk is delivered when its last sample
    would have been captured."""

    def __init__(self, feed: Callable[[bytes], None], prompts: "PromptAudio"):
        self.feed = feed
        self.prompts = prompts
        self._buf = b""
        self._lock = threading.Lock()
        self._drained = threading.Event()
        self._last_speech_at: Optional[float] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="synthetic-feed", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(2)

    def present(self, phrase: Phrase) -> float:
        pcm = self.prompts.get(phrase)
        with self._lock:
            self._buf = pcm
            self._drained.clear()
        while not self._drained.wait(0.1):
            if self._stop.is_set():
                break
        return self._last_speech_at or time.monotonic()

    def _run(self) -> None:
        step = int(PROMPT_RATE * CHUNK_S) * 2
        next_t = time.monotonic()
        while not self._stop.is_set():
            next_t += CHUNK_S
            delay = next_t - time.monotonic()
            if delay > 0 and self._stop.wait(delay):
                return
            with self._lock:
                chunk, self._buf = self._buf[:step], self._buf[step:]
                last = bool(chunk) and not self._buf
            speech = len(chunk)
            self.feed(chunk + bytes(step - speech))
            if last:
                pad_s = (step - speech) / (PROMPT_RATE * 2)
                self._last_speech_at = time.monotonic() - pad_s
                self._drained.set()


class SpeakerSource:
    """Plays each prompt through the speaker; the mic (captured by the app's
    AudioCapture) hears it through the room."""

    def __init__(self, player, prompts: "PromptAudio"):
        self.player = player
        self.prompts = prompts
        self._cancel = threading.Event()

    def present(self, phrase: Phrase) -> float:
        pcm = self.prompts.get(phrase)
        t0 = time.monotonic()
        self.player.play(pcm, PROMPT_RATE, self._cancel)
        lat = float(getattr(self.player, "output_latency", 0.0) or 0.0)
        return t0 + lat + len(pcm) / (PROMPT_RATE * 2)

    def stop(self) -> None:
        self._cancel.set()
        self.player.close()


class PromptAudio:
    """Prompt audio, synthesized once and cached on disk (results/.prompt-cache)."""

    def __init__(self, creds, voices=None, cache_dir: Path = RESULTS_DIR / ".prompt-cache",
                 out=print, per_minute: int = config.F0_TTS_TRANSACTIONS_PER_MINUTE - 2):
        self.creds = creds
        self.voices = dict(PROMPT_VOICES if voices is None else voices)
        self.cache_dir = cache_dir
        self.out = out
        self.per_minute = per_minute
        self.sent: List[float] = []  # monotonic times of requests made
        self._synth = None

    def path(self, phrase: Phrase) -> Path:
        key = f"{self.voices[phrase.lang]}|{PROMPT_RATE}|{phrase.text}".encode("utf-8")
        return self.cache_dir / f"{hashlib.sha1(key).hexdigest()[:16]}.pcm"

    def get(self, phrase: Phrase) -> bytes:
        p = self.path(phrase)
        if p.exists():
            return p.read_bytes()
        return self._synthesize(phrase)

    def prepare(self, phrases: List[Phrase]) -> None:
        todo = [p for p in phrases if not self.path(p).exists()]
        if not todo:
            return
        self.out(f"Synthesizing {len(todo)} prompt clips (once; cached in {self.cache_dir})…")
        for p in todo:
            self._synthesize(p)
        # Leave most of the F0 20/min budget to the translation voice, so prompt
        # requests don't throttle it and inflate its measured latency.
        keep = 10
        wait = 60 - (time.monotonic() - self.sent[-keep]) if len(self.sent) >= keep else 0
        if wait > 0:
            self.out(f"Waiting {wait:.0f} s for the TTS rate limit window to clear…")
            time.sleep(wait)

    def _synthesize(self, phrase: Phrase) -> bytes:
        import azure.cognitiveservices.speech as speechsdk
        from translator.tts import build_ssml
        recent = [t for t in self.sent if time.monotonic() - t < 60]
        if len(recent) >= self.per_minute:
            time.sleep(max(0.0, 60 - (time.monotonic() - recent[0])) + 0.2)
        if self._synth is None:
            cfg = speechsdk.SpeechConfig(subscription=self.creds.key, region=self.creds.region)
            cfg.set_speech_synthesis_output_format(
                speechsdk.SpeechSynthesisOutputFormat.Raw16Khz16BitMonoPcm)
            self._synth = speechsdk.SpeechSynthesizer(speech_config=cfg, audio_config=None)
        self.sent.append(time.monotonic())
        r = self._synth.speak_ssml_async(
            build_ssml(phrase.text, phrase.lang, self.voices[phrase.lang])).get()
        if r.reason != speechsdk.ResultReason.SynthesizingAudioCompleted:
            d = getattr(r, "cancellation_details", None)
            raise RuntimeError(f"prompt synthesis failed for {phrase.id}: "
                               f"{getattr(d, 'error_details', r.reason)}")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.path(phrase).write_bytes(r.audio_data)
        return r.audio_data


# ------------------------------------------------------------------ report
def _fmt(x: Optional[float], unit: str = " s") -> str:
    return "–" if x is None else f"{x:.2f}{unit}"


def _stats(xs: List[float]) -> str:
    if not xs:
        return "–"
    return (f"median {statistics.median(xs):.2f} s, p90 {percentile(xs, 90):.2f} s, "
            f"max {max(xs):.2f} s (n={len(xs)})")


def phrase_rows(trials: List[Trial], rows: List[dict]) -> List[dict]:
    """One row per phrase: detection, recognition quality, latency of its last segment."""
    by_seg = {r["seg"]: r for r in rows if r.get("kind") == "speech"}
    out = []
    for t in trials:
        last = by_seg.get(t.segs[-1][0]) if t.segs else None
        speech_end = (t.segs[-1][1].timing or {}).get("speech_end") if t.segs else None
        audible = None
        if last is not None and last.get("t", {}).get("play_start") is not None:
            audible = last["t"]["play_start"] + (last["t"].get("output_latency") or 0.0)
        out.append({
            "id": t.phrase.id, "lang": t.phrase.lang, "category": t.phrase.category,
            "detected": t.detected or "", "correct": t.detected == t.phrase.lang,
            "segments": len(t.segs), "similarity": round(similarity(t.phrase.text, t.recognized), 3),
            "prompt": t.phrase.text, "recognized": t.recognized, "translation": t.translation,
            "recognize_s": last and last.get("recognize_s"),
            "caption_s": last and last.get("caption_s"),
            "total_s": last and last.get("total_s"),
            "voice": last and last.get("outcome"),
            # Cross-checks against the true end of the prompt audio.
            "speech_end_vs_ref_s": (round(speech_end - t.ref_end, 3)
                                    if speech_end is not None and t.ref_end is not None else None),
            "total_from_ref_s": (round(audible - t.ref_end, 3)
                                 if audible is not None and t.ref_end is not None else None),
            "switch_s": t.switch_s,
        })
    return out


def write_csv(path: Path, prow: List[dict]) -> None:
    if not prow:
        return
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:  # BOM: Excel shows Thai
        w = csv.DictWriter(fh, fieldnames=list(prow[0]))
        w.writeheader()
        w.writerows(prow)


def summary_md(meta: dict, trials: List[Trial], rows: List[dict], stray: list) -> str:
    prow = phrase_rows(trials, rows)
    attempted = [p for p, t in zip(prow, trials) if t.t_prompt]
    lines = [f"# Measurement {meta['started']}", ""]
    lines += [f"- **{k}**: {v}" for k, v in meta.items() if k != "started"]
    lines.append("")

    # Detection
    lines += ["## Language detection", ""]
    if meta["mode"] != "auto":
        lines += [f"Direction was fixed ({meta['mode']}), so detection was not tested. "
                  "Run with `--mode auto` for accuracy.", ""]
    n = len(attempted)
    missed = [p for p in attempted if not p["segments"]]
    wrong = [p for p in attempted if p["segments"] and not p["correct"]]
    ok = n - len(missed) - len(wrong)
    if meta["mode"] == "auto" and n:
        lines.append(f"**{ok}/{n} correct ({100 * ok / n:.0f}%)**, {len(wrong)} wrong language, "
                     f"{len(missed)} missed (no result).")
        lines += ["", "| Group | n | correct | wrong | missed |", "|---|---:|---:|---:|---:|"]
        groups = [(f"spoken {config.LANGUAGES[l].name}", lambda p, l=l: p["lang"] == l)
                  for l in config.PAIR]
        groups += [(f"category: {c}", lambda p, c=c: p["category"] == c)
                   for c in sorted({p["category"] for p in attempted})]
        for label, pred in groups:
            g = [p for p in attempted if pred(p)]
            if not g:
                continue
            gm = sum(1 for p in g if not p["segments"])
            gw = sum(1 for p in g if p["segments"] and not p["correct"])
            lines.append(f"| {label} | {len(g)} | {len(g) - gm - gw} | {gw} | {gm} |")
        if wrong:
            lines += ["", "Wrong language:", ""]
            lines += [f"- `{p['id']}` said {p['lang']}, detected {p['detected']}: "
                      f"“{p['prompt']}” → “{p['recognized']}”" for p in wrong]
    if missed:
        lines += ["", "No result:", ""]
        lines += [f"- `{p['id']}` ({p['lang']}, {p['category']}): “{p['prompt']}”" for p in missed]
    lines.append("")

    # Recognition quality
    lines += ["## Recognition text", "",
              "Character similarity between prompt and recognized text (1.00 = identical "
              "after removing punctuation/spaces; numbers written as digits vs words lower it).", ""]
    for lang in config.PAIR:
        xs = [p["similarity"] for p in attempted if p["lang"] == lang and p["segments"]]
        if xs:
            lines.append(f"- {config.LANGUAGES[lang].name}: median {statistics.median(xs):.2f}, "
                         f"min {min(xs):.2f} (n={len(xs)})")
    worst = sorted((p for p in attempted if p["segments"]), key=lambda p: p["similarity"])[:5]
    if worst:
        lines += ["", "Lowest:", ""]
        lines += [f"- `{p['id']}` {p['similarity']:.2f}: “{p['prompt']}” → “{p['recognized']}”"
                  for p in worst]
    split = [p for p in attempted if p["segments"] > 1]
    if split:
        lines += ["", f"Split into several results: {', '.join(p['id'] for p in split)}"]
    lines.append("")

    # Latency
    speech = [r for r in rows if r.get("kind") == "speech"]
    lines += ["## Latency per stage (per recognized segment)", "", stage_table(speech), ""]
    for lang in config.PAIR:
        sub = [r for r in speech if r.get("src") == lang]
        tot = [r["total_s"] for r in sub if r.get("total_s") is not None]
        cap = [r["caption_s"] for r in sub if r.get("caption_s") is not None]
        if sub:
            name = config.LANGUAGES[lang].name
            lines.append(f"- Spoken {name}: text on screen {_stats(cap)}; voice starts {_stats(tot)}")
    synth = [(r["tgt_chars"], r["synth_s"]) for r in speech
             if r.get("synth_s") is not None and not r.get("cached")]
    if len(synth) >= 3:
        short = [s for c, s in synth if c <= 40]
        long_ = [s for c, s in synth if c > 40]
        lines.append(f"- TTS synthesis, ≤40 chars: {_stats(short)}; >40 chars: {_stats(long_)}")
    switches = [p["switch_s"] for p in prow if p["switch_s"] is not None]
    if switches:
        lines.append(f"- Direction switch (floor mode): {_stats(switches)}")
    lines.append(f"- Voice outcomes: {outcome_counts(speech)}")
    ref = [p["speech_end_vs_ref_s"] for p in prow if p["speech_end_vs_ref_s"] is not None]
    tref = [p["total_from_ref_s"] for p in prow if p["total_from_ref_s"] is not None]
    if ref:
        lines += ["", "Cross-check against the true end of each prompt clip:", "",
                  f"- recognizer's speech end minus true end: {_stats(ref)} "
                  "(slightly negative is expected: clips end with a little silence)",
                  f"- true end → voice starts (last segment): {_stats(tref)}"]
    if stray:
        lines += ["", f"{len(stray)} result(s) arrived with no phrase active: "
                  + "; ".join(f"“{s.source_text}”" for _, s in stray)]
    lines += ["", "Notes: `ui_s` here is the harness callback (no window); the app adds "
              "its 50 ms UI poll. `total_s` includes the output device latency PortAudio reports, "
              "not the speaker's own acoustic delay.", ""]
    return "\n".join(lines)


# ------------------------------------------------------------------ main
def _pick(devs, name: Optional[str], default_index: Optional[int], kind: str):
    if name:
        hits = [d for d in devs if name.lower() in d.name.lower()]
        if not hits:
            raise SystemExit(f"No {kind} matching {name!r}. Use --list-devices.")
        return hits[0]
    for d in devs:
        if d.index == default_index:
            return d
    if devs:
        return devs[0]
    raise SystemExit(f"No {kind} found.")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--source", choices=("speaker", "synthetic", "human"), default="speaker")
    ap.add_argument("--mode", choices=MODES, default="auto")
    ap.add_argument("--phrases", type=Path, default=PHRASES)
    ap.add_argument("--limit", type=int, default=0, help="only the first N phrases")
    ap.add_argument("--mic", help="part of the mic name (default: system default)")
    ap.add_argument("--out", help="part of the speaker name (default: system default)")
    ap.add_argument("--silence-ms", type=int, help="end-of-speech silence (SEGMENTATION_SILENCE_MS)")
    ap.add_argument("--no-voice", action="store_true", help="don't speak the translations")
    ap.add_argument("--list-devices", action="store_true")
    args = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    import pyaudiowpatch as pyaudio
    from translator.audio import AudioCapture, AudioPump, Player, list_devices
    from translator.recognition import RecognitionController
    from translator.tts import POLICY_IN_ORDER, TtsQueue

    pa = pyaudio.PyAudio()
    handler = None
    try:
        devices = list_devices(pa)
        if args.list_devices:
            for kind, devs in (("mic", devices.mics), ("speaker", devices.outputs)):
                for d in devs:
                    print(f"{kind:8} {d.name}")
            return 0
        phrases = load_phrases(args.phrases)
        if args.limit:
            phrases = phrases[:args.limit]
        if args.silence_ms is not None:
            os.environ["SEGMENTATION_SILENCE_MS"] = str(args.silence_ms)
        creds = config.load_credentials()
        voices = config.load_voices()
        silence = config.segmentation_silence_ms()
        config.preflight_check(creds)

        out_dev = _pick(devices.outputs, args.out, devices.default_output, "speaker")
        mic = None if args.source == "synthetic" else \
            _pick(devices.mics, args.mic, devices.default_mic, "mic")
        started = datetime.now()
        out_dir = RESULTS_DIR / f"measure-{started:%Y%m%d-%H%M%S}-{args.source}-" \
                                f"{args.mode.replace('>', '-')}"
        out_dir.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(out_dir / "run.log", encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logging.getLogger().addHandler(handler)
        logging.getLogger().setLevel(logging.INFO)

        import azure.cognitiveservices.speech as speechsdk
        meta = {"started": f"{started:%Y-%m-%d %H:%M}", "source": args.source, "mode": args.mode,
                "phrases": f"{len(phrases)} from {args.phrases.name}",
                "region": creds.region, "speech SDK": speechsdk.__version__,
                "end-of-speech silence": f"{silence} ms" if silence else "service default",
                "translation voices": ", ".join(voices.values()),
                "speaker": out_dev.name, "mic": mic.name if mic else "– (audio pushed directly)",
                "prompt voices": ", ".join(PROMPT_VOICES.values()) if args.source != "human"
                else "– (human)"}

        prompts = None
        if args.source != "human":
            prompts = PromptAudio(creds)
            prompts.prepare(phrases)

        lat = LatencyLog(out_dir / "rows.jsonl", include_text=True)
        tts = None
        holder = {}
        mic_path = args.source != "synthetic"
        pump = AudioPump(
            # Same feedback gate as onsite: the mic is muted while the voice speaks.
            is_paused=(lambda: holder["tts"].is_busy() if "tts" in holder else False)
            if mic_path else (lambda: False),
            buffer_s=config.SWITCH_BUFFER_S)
        if not args.no_voice:
            tts = TtsQueue(creds, on_queue=lambda *a: None, on_spoken_chars=lambda n: None,
                           on_error=lambda m: print(f"   TTS: {m}"), voices=voices,
                           tail_s=config.ONSITE_TAIL_S if mic_path else config.PAUSE_TAIL_S,
                           policy=POLICY_IN_ORDER, merge_max_chars=0, on_timing=lat.tts)
            tts.start(Player(pa, out_dev))
            holder["tts"] = tts

        if args.source == "synthetic":
            source = SyntheticSource(pump.feed, prompts)
        elif args.source == "speaker":
            source = SpeakerSource(Player(pa, out_dev), prompts)
        else:
            source = HumanSource()
        runner = Runner(phrases, args.mode, source, lat, tts=tts)
        first = config.MODE_AUTO if args.mode == "auto" else \
            mode_for(phrases[0].lang) if args.mode == "floor" else args.mode
        controller = RecognitionController(
            creds, pump, on_interim=lambda *a: None, on_final=runner.on_final,
            on_status=runner.on_status, on_fatal=runner.on_fatal, mode=first)
        runner.controller = controller

        capture = None
        pump.start()
        try:
            if mic_path:
                capture = AudioCapture(pa)
                capture.start(mic, pump.feed)
            else:
                source.start()
            threading.Thread(target=controller.start, name="connect", daemon=True).start()
            if not runner.wait_connected():
                print(runner.fatal_msg or "Could not connect to Azure Speech.")
                return 2
            if args.source == "human":
                print("\nRead each phrase when it appears, at a normal pace. Ctrl+C stops.\n")
                time.sleep(2)
            runner.run()
            time.sleep(1.0)  # late results
        except KeyboardInterrupt:
            print("\nStopped; writing what was measured so far…")
            runner.stop.set()
        finally:
            controller.stop()
            if capture is not None:
                capture.stop()
            if hasattr(source, "stop"):
                source.stop()
            if tts is not None:
                tts.stop()
            pump.stop()
            lat.close()

        meta["audio sent"] = f"{pump.seconds_sent / 60:.1f} min"
        if runner.fatal_msg:
            meta["stopped by error"] = runner.fatal_msg
        prow = phrase_rows(runner.trials, lat.rows)
        write_csv(out_dir / "phrases.csv", prow)
        md = summary_md(meta, runner.trials, lat.rows, runner.stray)
        (out_dir / "summary.md").write_text(md, encoding="utf-8")
        print("\n" + md)
        print(f"\nResults: {out_dir}")
        return 0 if not runner.fatal_msg else 1
    except config.ConfigError as e:
        print(f"Configuration problem: {e}")
        return 2
    finally:
        if handler is not None:
            logging.getLogger().removeHandler(handler)
            handler.close()
        pa.terminate()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
