"""Long-session soak test: is translation still continuous after 30+ minutes?

Live (real Azure, real mic, real speaker; needs .env):

  python -m tools.soak --minutes 30                        # meeting, auto-detect, voice on
  python -m tools.soak --minutes 30 --setting onsite       # onsite: in-order voice, 0.7 s tail
  python -m tools.soak --minutes 30 --mute                 # subtitles only (mic never gated)
  python -m tools.soak --minutes 30 --mute-every 5         # toggle the voice every 5 min
  python -m tools.soak --minutes 30 --drop-every 10        # force a reconnect every 10 min
  python -m tools.soak --minutes 30 --pace lecture --mute  # back-to-back speech, no turns
  options: --mic NAME  --out NAME  --mode auto|en>th|th>en  --source speaker|human

A TTS voice reads tools/phrases.tsv aloud on the speaker, over and over, and
the app's own pipeline (AudioPump -> RecognitionController -> TtsQueue ->
Player) listens through the mic in the room. Every phrase is checked for a
result (missed / duplicated / late), every outage is timed, mute windows are
checked for audio that should not have played, and memory and CPU are
sampled. Results: results/soak-<time>-<setting>/summary.md, samples.csv,
phrases.csv, rows.jsonl, run.log.

Offline (no Azure; the real window, fed with simulated speech, accelerated):

  python -m tools.soak --offline --minutes 120 --speed 30

checks that the UI stays bounded over a long session: memory, Tk callbacks,
canvas items, rendered cards.

Cost of a live 30-minute run: ~30 min of the 5 free translation hours a month
and, with the voice on, ~15-20k of the 500k free TTS characters.
"""
import os
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")  # see main.py
import argparse
import ctypes
import csv
import itertools
import logging
import os
import statistics
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from translator import config
from translator.latency import LatencyLog, percentile, stage_table

RESULTS_DIR = config.APP_DIR / "results"
LATE_S = 6.0  # speech end -> caption later than this counts as delayed


# ---------------------------------------------------------- resource sampling
class _PMC(ctypes.Structure):
    _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]


def memory_mb():
    """(working set, private bytes) of this process in MB (Windows)."""
    try:
        k32, psapi = ctypes.windll.kernel32, ctypes.windll.psapi
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        pmc = _PMC()
        pmc.cb = ctypes.sizeof(_PMC)
        psapi.GetProcessMemoryInfo(ctypes.c_void_p(k32.GetCurrentProcess()), ctypes.byref(pmc),
                                   pmc.cb)
        return pmc.WorkingSetSize / 2 ** 20, pmc.PagefileUsage / 2 ** 20
    except Exception:
        return float("nan"), float("nan")


class Sampler:
    """Samples memory and CPU (% of one core) every `every_s` seconds."""

    def __init__(self, every_s: float = 10.0, extra=None):
        self.every_s = every_s
        self.extra = extra  # () -> dict of extra columns
        self.samples: List[dict] = []
        self._stop = threading.Event()
        self._t0 = time.monotonic()
        self._last = (time.monotonic(), time.process_time())
        self._thread = threading.Thread(target=self._run, name="sampler", daemon=True)

    def start(self):
        self.sample()
        self._thread.start()
        return self

    def sample(self):
        now, cpu = time.monotonic(), time.process_time()
        lw, lc = self._last
        self._last = (now, cpu)
        ws, priv = memory_mb()
        row = {"t_min": round((now - self._t0) / 60, 2), "working_set_mb": round(ws, 1),
               "private_mb": round(priv, 1),
               "cpu_pct": round(100 * (cpu - lc) / (now - lw), 1) if now > lw else 0.0,
               "threads": threading.active_count()}
        if self.extra is not None:
            try:
                row.update(self.extra())
            except Exception:
                pass
        self.samples.append(row)
        return row

    def _run(self):
        while not self._stop.wait(self.every_s):
            self.sample()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=2)
        self.sample()


def memory_verdict(samples: List[dict], key="private_mb", warmup=0.15) -> dict:
    """Growth after warm-up, as MB per hour (a linear fit)."""
    xs = [s for s in samples if s.get(key) == s.get(key)]  # drop NaN
    if len(xs) < 3:
        return {"slope_mb_per_h": None}
    t_end = xs[-1]["t_min"]
    xs = [s for s in xs if s["t_min"] >= t_end * warmup] or xs
    t = [s["t_min"] / 60 for s in xs]
    m = [s[key] for s in xs]
    if len(t) < 2 or max(t) == min(t):
        return {"slope_mb_per_h": None}
    tm, mm = statistics.mean(t), statistics.mean(m)
    slope = sum((a - tm) * (b - mm) for a, b in zip(t, m)) / sum((a - tm) ** 2 for a in t)
    return {"slope_mb_per_h": round(slope, 2), "start_mb": m[0], "end_mb": m[-1],
            "max_mb": max(m)}


def write_samples(path: Path, samples: List[dict]) -> None:
    if not samples:
        return
    keys = list(dict.fromkeys(k for s in samples for k in s))
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(samples)


# ------------------------------------------------------------------- analysis
def analyse(trials, rows, gaps, mute_windows, stray) -> dict:
    """Per-phrase outcome plus the session-level problems the soak looks for."""
    from tools.measure import norm, similarity
    missed, dup, late = [], [], []
    for i, tr in enumerate(trials):
        if not tr.t_prompt:
            continue  # not presented (run ended)
        if not tr.segs:
            missed.append(i)
            continue
        texts = [norm(s.source_text) for _, s in tr.segs]
        if any(similarity(a, b) > 0.85 for a, b in itertools.combinations(texts, 2)):
            dup.append(i)
    by_id = {r.get("id"): r for r in rows if r.get("kind") == "speech"}
    for i, tr in enumerate(trials):
        for sid, _ in tr.segs:
            r = by_id.get(sid)
            if r and r.get("caption_s") is not None and r["caption_s"] > LATE_S:
                late.append((i, r["caption_s"]))
    # Audio that played while the voice was muted would be a mute failure.
    leaks = []
    for r in rows:
        t = r.get("t") or {}
        ps = t.get("play_start")
        if r.get("outcome") == "played" and ps is not None:
            if any(a <= ps <= (b or float("inf")) for a, b in mute_windows):
                leaks.append(r.get("id"))
    return {"missed": missed, "duplicated": dup, "late": late, "mute_leaks": leaks,
            "gaps": gaps, "stray": len(stray)}


def summary_md(meta: dict, trials, rows, res: dict, samples, mem: dict) -> str:
    presented = [t for t in trials if t.t_prompt]
    lines = [f"# Soak test: {meta.get('setting')} · {meta.get('mode')} · "
             f"{'voice muted' if meta.get('muted') else 'voice on'}", ""]
    for k, v in meta.items():
        lines.append(f"- **{k}:** {v}")
    n = len(presented)
    ok = n - len(res["missed"])
    lines += ["", "## Continuity", "",
              "| Check | Result |", "|---|---|",
              f"| Phrases presented | {n} |",
              f"| With a result | {ok} ({100 * ok / max(1, n):.1f} %) |",
              f"| **Missed (no result)** | {len(res['missed'])} |",
              f"| **Duplicated** | {len(res['duplicated'])} |",
              f"| **Delayed** (caption > {LATE_S:.0f} s after speech) | {len(res['late'])} |",
              f"| Results with no phrase active | {res['stray']} |",
              f"| Connection outages | {len(res['gaps'])} "
              + (f"(longest {max(g for _, g in res['gaps']):.1f} s)" if res["gaps"] else "")
              + " |",
              f"| Audio played while muted | {len(res['mute_leaks'])} |", ""]

    def show(title, idx, extra=None):
        if not idx:
            return
        lines.append(f"**{title}:**")
        lines.append("")
        for item in idx[:40]:
            i, more = (item if isinstance(item, tuple) else (item, None))
            tr = trials[i]
            at = f"{(tr.t_prompt - meta['_t0']) / 60:5.1f} min"
            rec = " | ".join(s.source_text for _, s in tr.segs) or "–"
            lines.append(f"- {at} `{tr.phrase.id}` ({tr.phrase.lang}) {tr.phrase.text} → {rec}"
                         + (f" ({more:.1f} s)" if more is not None else ""))
        lines.append("")
    show("Missed", res["missed"])
    show("Duplicated", res["duplicated"])
    show("Delayed", res["late"])
    if res["gaps"]:
        lines += ["**Outages:**", ""]
        lines += [f"- {datetime.fromtimestamp(a):%H:%M:%S}: {s:.1f} s" for a, s in res["gaps"]]
        lines.append("")
    lines += ["## Latency (all sentences)", "", stage_table(rows), ""]
    # Drift check: latency in the first vs the last third of the session.
    sp = [r for r in rows if r.get("kind") == "speech" and r.get("caption_s") is not None]
    if len(sp) >= 9:
        k = len(sp) // 3
        a = [r["caption_s"] for r in sp[:k]]
        b = [r["caption_s"] for r in sp[-k:]]
        lines += [f"Caption delay, median: first third {statistics.median(a):.2f} s, "
                  f"last third {statistics.median(b):.2f} s "
                  f"(p90 {percentile(a, 90):.2f} → {percentile(b, 90):.2f} s).", ""]
    lines += ["## Resources", ""]
    if samples:
        cpu = [s["cpu_pct"] for s in samples[1:]] or [0]
        lines += [f"- Private memory: {mem.get('start_mb')} → {mem.get('end_mb')} MB "
                  f"(max {mem.get('max_mb')} MB), trend after warm-up "
                  f"**{mem.get('slope_mb_per_h')} MB/h**",
                  f"- CPU (one core): mean {statistics.mean(cpu):.1f} %, max {max(cpu):.1f} %",
                  f"- Threads: {samples[0]['threads']} → {samples[-1]['threads']}",
                  "- Every sample: samples.csv", ""]
    return "\n".join(lines)


# ------------------------------------------------------------------ live run
def live(args) -> int:
    import pyaudiowpatch as pyaudio
    from tools import measure
    from translator.audio import AudioCapture, AudioPump, Player, list_devices
    from translator.onsite import EchoFilter
    from translator.recognition import RecognitionController
    from translator.tts import POLICY_CATCH_UP, POLICY_IN_ORDER, TtsQueue

    pa = pyaudio.PyAudio()
    handler = None
    try:
        devices = list_devices(pa)
        creds = config.load_credentials()
        voices = config.load_voices()
        config.preflight_check(creds)
        out_dev = measure._pick(devices.outputs, args.out, devices.default_output, "speaker")
        mic = measure._pick(devices.mics, args.mic, devices.default_mic, "mic")
        base = measure.load_phrases(args.phrases)
        # Enough phrases for the duration (~10 s each incl. the voice), cycled.
        phrases = [p for _, p in zip(range(int(args.minutes * 60 / 4) + 10),
                                     itertools.cycle(base))]
        started = datetime.now()
        out_dir = RESULTS_DIR / f"soak-{started:%Y%m%d-%H%M%S}-{args.setting}-" \
                                f"{'muted' if args.mute else 'voice'}"
        out_dir.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(out_dir / "run.log", encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logging.getLogger().addHandler(handler)
        logging.getLogger().setLevel(logging.INFO)

        prompts = measure.PromptAudio(creds)
        prompts.prepare(base)
        onsite = args.setting == "onsite"
        lat = LatencyLog(out_dir / "rows.jsonl", include_text=True, keep_rows=None)
        echo = EchoFilter()
        holder = {}
        # The app's gate: closed while the voice plays (+ tail); never while muted.
        pump = AudioPump(is_paused=lambda: holder["tts"].is_busy() if "tts" in holder else False,
                         buffer_s=config.RECONNECT_BUFFER_S)
        tts = TtsQueue(creds, on_queue=lambda *a: None, on_spoken_chars=lambda n: None,
                       on_error=lambda m: print(f"   TTS: {m}"), voices=voices,
                       on_playing=lambda u: echo.spoken(u.text, u.lang),
                       tail_s=config.ONSITE_TAIL_S if onsite else config.PAUSE_TAIL_S,
                       policy=POLICY_IN_ORDER if onsite else POLICY_CATCH_UP, on_timing=lat.tts)
        tts.start(Player(pa, out_dev))
        holder["tts"] = tts
        muted = {"on": bool(args.mute), "since": time.monotonic() if args.mute else None}
        windows = []  # (from, to) monotonic, mute periods
        tts.set_muted(muted["on"])
        source = (measure.SpeakerSource(Player(pa, out_dev), prompts) if args.source == "speaker"
                  else measure.HumanSource())
        runner = measure.Runner(phrases, args.mode, source, lat, tts=tts)
        echoes = []
        on_final = runner.on_final

        def final(seg):
            if echo.is_echo(seg.source_text, seg.source_lang):
                echoes.append(seg.source_text)
                return
            on_final(seg)
        gaps = []
        controller = RecognitionController(
            creds, pump, on_interim=lambda *a: None, on_final=final,
            on_status=runner.on_status, on_fatal=runner.on_fatal,
            mode=config.MODE_AUTO if args.mode == "auto" else args.mode,
            on_gap=lambda at, s: (gaps.append((at, s)), print(f"   (outage: {s:.1f} s)")))
        runner.controller = controller
        sampler = Sampler(extra=lambda: {"segments": sum(len(t.segs) for t in runner.trials),
                                         "tts_waiting": tts.waiting,
                                         "outages": len(gaps)}).start()
        meta = {"started": f"{started:%Y-%m-%d %H:%M}", "setting": args.setting,
                "mode": args.mode, "muted": args.mute, "pace": args.pace,
                "source": args.source, "region": creds.region, "speaker": out_dev.name,
                "mic": mic.name, "voices": ", ".join(voices.values()),
                "mute toggled every": f"{args.mute_every} min" if args.mute_every else "–",
                "forced reconnect every": f"{args.drop_every} min" if args.drop_every else "–",
                "_t0": time.monotonic()}

        capture = AudioCapture(pa)
        pump.start()
        stop_at = time.monotonic() + args.minutes * 60

        def chores():
            next_mute = time.monotonic() + args.mute_every * 60 if args.mute_every else None
            next_drop = time.monotonic() + args.drop_every * 60 if args.drop_every else None
            while not runner.stop.wait(1.0):
                now = time.monotonic()
                if now >= stop_at:
                    runner.stop.set()
                    return
                if next_mute and now >= next_mute:
                    next_mute = now + args.mute_every * 60
                    muted["on"] = not muted["on"]
                    if muted["on"]:
                        muted["since"] = now
                    else:
                        windows.append((muted["since"], now))
                    tts.set_muted(muted["on"])
                    print(f"   (voice {'muted' if muted['on'] else 'unmuted'})")
                if next_drop and now >= next_drop:
                    next_drop = now + args.drop_every * 60
                    print("   (forcing a reconnect)")
                    controller._drop(controller._gen, "simulated drop (soak test)", None)

        try:
            capture.start(mic, pump.feed)
            threading.Thread(target=controller.start, name="connect", daemon=True).start()
            if not runner.wait_connected():
                print(runner.fatal_msg or "Could not connect to Azure Speech.")
                return 2
            threading.Thread(target=chores, name="chores", daemon=True).start()
            if args.pace == "lecture":
                for i, tr in enumerate(runner.trials):
                    if runner.stop.is_set():
                        break
                    with runner._lock:
                        runner._cur = i
                        tr.t_prompt = time.monotonic()
                    print(f"[{i + 1}] {tr.phrase.id} ({tr.phrase.lang}): {tr.phrase.text}")
                    tr.ref_end = source.present(tr.phrase)
                    runner.stop.wait(args.pause_s)
                time.sleep(3)
            else:
                runner.run()
            time.sleep(1.0)
        except KeyboardInterrupt:
            print("\nStopped; writing what was measured so far…")
            runner.stop.set()
        finally:
            runner.stop.set()
            controller.stop()
            capture.stop()
            if hasattr(source, "stop"):
                source.stop()
            tts.stop()
            pump.stop()
            lat.close()
            sampler.stop()
            if muted["on"] and muted["since"] is not None:
                windows.append((muted["since"], None))

        res = analyse(runner.trials, lat.rows, gaps, windows, runner.stray)
        mem = memory_verdict(sampler.samples)
        meta["duration"] = f"{(time.monotonic() - meta['_t0']) / 60:.1f} min"
        meta["audio sent"] = f"{pump.seconds_sent / 60:.1f} min"
        meta["audio dropped (buffer full)"] = f"{pump.bytes_dropped / 32000:.1f} s"
        meta["ignored as the app's own voice"] = len(echoes)
        if runner.fatal_msg:
            meta["stopped by error"] = runner.fatal_msg
        md = summary_md(meta, runner.trials, lat.rows, res, sampler.samples, mem)
        md = md.replace(f"- **_t0:** {meta['_t0']}\n", "")
        (out_dir / "summary.md").write_text(md, encoding="utf-8")
        write_samples(out_dir / "samples.csv", sampler.samples)
        measure.write_csv(out_dir / "phrases.csv",
                          measure.phrase_rows([t for t in runner.trials if t.t_prompt], lat.rows))
        print("\n" + md + f"\n\nResults: {out_dir}")
        return 0 if not runner.fatal_msg else 1
    except config.ConfigError as e:
        print(f"Configuration problem: {e}")
        return 2
    finally:
        if handler is not None:
            logging.getLogger().removeHandler(handler)
            handler.close()
        pa.terminate()


# --------------------------------------------------------------- offline run
def offline(args) -> int:
    """The real window, fed with simulated speech at `speed` x real time."""
    import tkinter as tk
    from types import SimpleNamespace

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    from translator import messages, theme, ui
    from translator.recognition import Segment
    from translator.session import State

    theme.register_fonts()
    root = tk.Tk()
    theme.init(root)
    errors = []
    root.report_callback_exception = lambda *a: errors.append(a)
    app = ui.App(root)

    class FakeTts:
        waiting = skipped = 0

        def __init__(self):
            self.muted = False
            self.spoken = 0

        def state(self):
            return "idle"

        def is_busy(self):
            return False

        def behind_seconds(self):
            return 0.0

        def set_muted(self, m):
            self.muted = m

        def enqueue(self, *a):
            if self.muted:
                return False
            self.spoken += 1
            return True

        def replay(self, *a):
            return not self.muted

    tts = FakeTts()
    app.sm.transition(State.STARTING)
    app.sm.transition(State.RUNNING)
    app.session = SimpleNamespace(tts=tts, latency=LatencyLog(keep_rows=ui.LATENCY_ROWS_KEPT),
                                  pump=None, controller=None, transcript=None)
    app._set_device_widgets("disabled")
    app.set_status("listening", "Listening (simulated)")

    per_min = 25  # sentences per minute of speech
    total = int(args.minutes * per_min)
    interval_ms = max(1, int(60000 / per_min / args.speed))
    texts = [("en", "th", "We will now look at how the results changed over the last quarter.",
              "ตอนนี้เราจะมาดูว่าผลลัพธ์เปลี่ยนไปอย่างไรในไตรมาสที่ผ่านมา"),
             ("th", "en", "ขอบคุณมากครับ มีคำถามเกี่ยวกับตัวเลขนี้ไหมครับ",
              "Thank you very much. Are there any questions about this figure?")]
    state = {"i": 0}

    def extra():
        return {"sim_min": round(state["i"] / per_min, 1), "messages": len(app.messages.items),
                "rendered": app.messages.rendered, "canvas_items": len(app.messages.cv.find_all()),
                # Python callbacks registered with Tcl (excluding cached images,
                # which paint.py caps): growth here would be a leak.
                "tcl_commands": sum(1 for c in root.tk.call("info", "commands")
                                    if not str(c).startswith("pyimage")),
                "tk_images": len(root.tk.call("image", "names")),
                "tk_errors": len(errors)}
    sampler = Sampler(every_s=max(1.0, 60 / args.speed), extra=extra).start()

    def step():
        i = state["i"]
        if i >= total:
            root.quit()
            return
        src, tgt, s, t = texts[i % 2]
        s, t = f"{s} ({i})", f"{t} ({i})"
        app._show_interim(s[: len(s) // 2], t[: len(t) // 2], src, tgt)
        app._show_final(Segment(time.time(), time.time() + 3, s, t, src, tgt))
        sim_min = i / per_min
        if i and i % (per_min * 10) == 0:  # mute toggles every 10 simulated minutes
            app._toggle_mute()
        if i and i % (per_min * 30) == 0:  # an outage every 30 simulated minutes
            app._note_gap(time.time() - 8, 8.0)
        state["i"] = i + 1
        root.after(interval_ms, step)

    root.after(500, step)
    root.mainloop()
    sampler.stop()
    s = sampler.samples
    mem = memory_verdict(s)
    # Growth per *simulated* hour: real-time slope / speed-up.
    per_sim_h = (mem["slope_mb_per_h"] or 0.0) * (s[-1]["t_min"] / 60) / (args.minutes / 60)
    ok_mem = mem["slope_mb_per_h"] is not None and per_sim_h < 20
    tcl_growth = s[-1]["tcl_commands"] - s[1]["tcl_commands"] if len(s) > 2 else 0
    print(f"Simulated {args.minutes} min ({total} sentences) in "
          f"{s[-1]['t_min']:.1f} min of real time.")
    print(f"Messages kept: {s[-1]['messages']}, cards rendered: {s[-1]['rendered']} "
          f"(cap {messages.MAX_RENDERED}+{messages.EVICT_BATCH}), canvas items: "
          f"{min(r['canvas_items'] for r in s[1:])}..{max(r['canvas_items'] for r in s[1:])}")
    print(f"Tcl callbacks: {s[1]['tcl_commands']} -> {s[-1]['tcl_commands']} "
          f"(growth {tcl_growth})")
    print(f"Private memory: {mem.get('start_mb')} -> {mem.get('end_mb')} MB, max "
          f"{mem.get('max_mb')} MB; trend after warm-up {per_sim_h:.2f} MB per simulated hour")
    cpu = [r["cpu_pct"] for r in s[1:]] or [0]
    print(f"CPU: mean {statistics.mean(cpu):.1f} %, max {max(cpu):.1f} % of one core "
          f"(at {args.speed}x the real event rate)")
    print(f"Tk callback errors: {len(errors)}")
    out = RESULTS_DIR / f"soak-{datetime.now():%Y%m%d-%H%M%S}-offline"
    out.mkdir(parents=True, exist_ok=True)
    write_samples(out / "samples.csv", s)
    print(f"Samples: {out / 'samples.csv'}")
    root.destroy()
    bounded = s[-1]["rendered"] <= messages.MAX_RENDERED + messages.EVICT_BATCH
    return 0 if (bounded and not errors and tcl_growth < 50 and ok_mem) else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--minutes", type=float, default=30)
    ap.add_argument("--setting", choices=("meeting", "onsite"), default="meeting")
    ap.add_argument("--mode", default="auto", choices=("auto", config.MODE_EN_TH, config.MODE_TH_EN))
    ap.add_argument("--source", choices=("speaker", "human"), default="speaker")
    ap.add_argument("--pace", choices=("turns", "lecture"), default="turns",
                    help="turns: wait for each translation; lecture: speak back to back")
    ap.add_argument("--pause-s", type=float, default=0.8, help="lecture pace: pause between phrases")
    ap.add_argument("--mute", action="store_true", help="voice off (subtitles only)")
    ap.add_argument("--mute-every", type=float, default=0, help="toggle the voice every N minutes")
    ap.add_argument("--drop-every", type=float, default=0, help="force a reconnect every N minutes")
    ap.add_argument("--phrases", type=Path, default=Path(__file__).with_name("phrases.tsv"))
    ap.add_argument("--mic")
    ap.add_argument("--out")
    ap.add_argument("--offline", action="store_true", help="no Azure: simulated speech, real UI")
    ap.add_argument("--speed", type=float, default=30, help="offline: times faster than real")
    args = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    return offline(args) if args.offline else live(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
