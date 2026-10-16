"""Per-stage latency instrumentation. Offline (fake Azure, fake audio)."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

import azure.cognitiveservices.speech as speechsdk

from translator import config, latency
from translator.audio import AudioPump
from translator.latency import BYTES_PER_S, LatencyLog, StreamClock, stage_table, stages
from translator.recognition import RecognitionController, Segment
from translator.session import State
from translator.tts import POLICY_CATCH_UP, POLICY_IN_ORDER, TtsQueue, Utterance

from test_lifecycle import FakeSynth, Fakes, headless_app, run_ui_until, wait_until
from test_queue_replay import RecordingPlayer


class StreamClockTest(unittest.TestCase):
    def test_maps_position_to_capture_time(self):
        c = StreamClock()
        c.add(BYTES_PER_S // 10, 100.1)  # 0.0-0.1 s captured, last sample at t=100.1
        c.add(BYTES_PER_S // 10, 100.2)
        self.assertAlmostEqual(c.time_at(0.1), 100.1)
        self.assertAlmostEqual(c.time_at(0.15), 100.15)
        self.assertAlmostEqual(c.time_at(0.0), 100.0)
        self.assertAlmostEqual(c.seconds, 0.2)
        self.assertIsNone(c.time_at(0.25))  # not sent yet

    def test_gap_in_capture_is_respected(self):
        """Buffered audio flushed later keeps its own capture times."""
        c = StreamClock()
        c.add(BYTES_PER_S, 10.0)   # 1 s of audio captured by t=10
        c.add(BYTES_PER_S, 50.0)   # next 1 s captured by t=50
        self.assertAlmostEqual(c.time_at(1.5), 49.5)

    def test_keeps_only_recent_audio(self):
        c = StreamClock(keep_s=1.0)
        for i in range(40):  # 4 s in 100 ms chunks
            c.add(BYTES_PER_S // 10, i * 0.1 + 0.1)
        self.assertIsNone(c.time_at(0.5))
        self.assertAlmostEqual(c.time_at(3.9), 3.9)
        self.assertLess(len(c._ends), 40)


class PumpClockTest(unittest.TestCase):
    def test_buffered_audio_keeps_capture_time(self):
        pump = AudioPump(lambda: False)
        pump.start()
        self.addCleanup(pump.stop)
        t_feed = time.monotonic()
        pump.feed(b"\x01\x00" * 1600)  # 100 ms, captured while there is no sink
        time.sleep(0.3)
        sink = SimpleNamespace(data=[], write=lambda b: sink.data.append(b))
        clock = pump.set_sink(sink)  # e.g. the recognizer after a turn switch
        self.assertEqual(len(sink.data), 1)
        self.assertAlmostEqual(clock.time_at(0.1), t_feed, delta=0.05)
        pump.feed(b"\x02\x00" * 1600)
        self.assertTrue(wait_until(lambda: len(sink.data) == 2))
        self.assertGreater(clock.time_at(0.2), t_feed + 0.25)
        self.assertIsNone(pump.set_sink(None))


def _result(text, translations, offset_s, duration_s, detected=""):
    props = {}
    if detected:
        props[speechsdk.PropertyId.SpeechServiceConnection_AutoDetectSourceLanguageResult] = detected
    return SimpleNamespace(result=SimpleNamespace(
        text=text, translations=translations, reason=speechsdk.ResultReason.TranslatedSpeech,
        offset=int(offset_s * 1e7), duration=int(duration_s * 1e7), properties=props))


class RecognitionTimingTest(unittest.TestCase):
    def make(self, mode=config.MODE_EN_TH):
        finals = []
        c = RecognitionController(config.Credentials("k", "r"),
                                  SimpleNamespace(set_sink=lambda s: None),
                                  on_interim=lambda *a: None, on_final=finals.append,
                                  on_status=lambda *a: None, on_fatal=lambda m: None, mode=mode)
        c._gen = 1
        clock = StreamClock()
        t0 = time.monotonic() - 5.0
        for i in range(50):  # 5 s of audio, captured in real time ending now
            clock.add(BYTES_PER_S // 10, t0 + (i + 1) * 0.1)
        c._clocks[1] = clock
        return c, finals, t0

    def test_final_carries_speech_end_and_first_interim(self):
        c, finals, t0 = self.make()
        c._on_recognizing(1, config.MODE_EN_TH, _result("Hel", {"th": "สวั"}, 1.0, 0.5))
        t_interim = time.monotonic()
        c._on_recognizing(1, config.MODE_EN_TH, _result("Hello", {"th": "สวัสดี"}, 1.0, 1.0))
        c._on_recognized(1, config.MODE_EN_TH, _result("Hello there", {"th": "สวัสดี"}, 1.0, 2.0))
        t = finals[0].timing
        self.assertAlmostEqual(t["speech_start"], t0 + 1.0, places=3)
        self.assertAlmostEqual(t["speech_end"], t0 + 3.0, places=3)
        self.assertLessEqual(t["first_interim"], t_interim)  # the FIRST partial
        self.assertGreaterEqual(t["final"], t["first_interim"])
        self.assertEqual(t["mode"], config.MODE_EN_TH)
        self.assertIsNone(t["detected"])
        self.assertEqual(c._first_interim, {})

    def test_auto_records_detected_locale(self):
        c, finals, _ = self.make(config.MODE_AUTO)
        c._on_recognized(1, config.MODE_AUTO,
                         _result("สวัสดีครับ", {"en": "Hello", "th": "สวัสดีครับ"}, 0, 1, "th-TH"))
        self.assertEqual(finals[0].timing["detected"], "th-TH")
        self.assertEqual((finals[0].source_lang, finals[0].target_lang), ("th", "en"))

    def test_no_clock_gives_none_not_crash(self):
        c, finals, _ = self.make()
        c._clocks.clear()
        c._on_recognized(1, config.MODE_EN_TH, _result("Hi", {"th": "หวัดดี"}, 0, 1))
        self.assertIsNone(finals[0].timing["speech_end"])

    def test_timing_not_part_of_equality(self):
        a = Segment(0, 1, "a", "b", timing={"final": 1.0})
        self.assertEqual(a, Segment(0, 1, "a", "b"))


def tts_queue(policy=POLICY_IN_ORDER, dur=0.02, **kw):
    timings = []
    q = TtsQueue(SimpleNamespace(key="k", region="r"), on_queue=lambda *a: None,
                 on_spoken_chars=lambda n: None, on_error=lambda m: None, policy=policy,
                 on_timing=lambda u, info: timings.append((u, info)), **kw)
    synth, player = FakeSynth(), RecordingPlayer(dur)
    player.output_latency = 0.05
    q._make_synth = lambda: synth
    return q, synth, player, timings


class TtsTimingTest(unittest.TestCase):
    def test_played_utterance_has_ordered_timestamps(self):
        q, synth, player, timings = tts_queue(dur=0.2)
        called = []
        real_play = player.play
        player.play = lambda *a: (called.append(time.monotonic()), real_play(*a))[1]
        q.start(player)
        q.enqueue("Hello", "en", 1)
        self.assertTrue(wait_until(lambda: timings))
        q.stop()
        u, info = timings[0]
        self.assertEqual((u.seg_ids, info["outcome"], info["cached"]), ((1,), "played", False))
        self.assertLessEqual(info["synth_start"], info["synth_end"])
        self.assertLessEqual(info["synth_end"], info["play_start"])
        # Stamped as playback begins, not when the clip has finished.
        self.assertLessEqual(info["play_start"], called[0])
        self.assertGreaterEqual(info["play_end"] - info["play_start"], 0.2)
        self.assertEqual(info["output_latency"], 0.05)
        self.assertEqual(info["rate_wait_s"], 0.0)

    def test_replay_from_cache_is_marked(self):
        q, synth, player, timings = tts_queue()
        q.start(player)
        q.enqueue("Hello", "en", 1)
        self.assertTrue(wait_until(lambda: len(timings) == 1))
        q.replay("Hello", "en", 1)
        self.assertTrue(wait_until(lambda: len(timings) == 2))
        q.stop()
        u, info = timings[1]
        self.assertTrue(u.replay)
        self.assertTrue(info["cached"])

    def test_catch_up_drop_is_reported(self):
        q, synth, player, timings = tts_queue(POLICY_CATCH_UP, max_backlog=1)
        q._running = True  # no worker
        q.enqueue("a", "en", 1)
        q.enqueue("b", "en", 2)
        self.assertEqual([(u.seg_ids, i["outcome"]) for u, i in timings], [((1,), "dropped")])

    def test_mute_reports_cleared_items(self):
        q, synth, player, timings = tts_queue()
        q._running = True
        q.enqueue("a", "en", 1)
        q.set_muted(True)
        self.assertEqual(timings[0][1]["outcome"], "muted")
        self.assertFalse(q.enqueue("b", "en", 2))  # muted: not queued

    def test_rate_limit_wait_is_measured(self):
        q, synth, player, timings = tts_queue(per_minute=1)
        q._sent.append(time.monotonic() - 59.7)  # the 1/min budget frees in ~0.3 s
        q.start(player)
        q.enqueue("Hello", "en", 1)
        self.assertTrue(wait_until(lambda: timings, timeout=3))
        q.stop()
        self.assertGreater(timings[0][1]["rate_wait_s"], 0.2)

    def test_cut_off_still_reports_start(self):
        q, synth, player, timings = tts_queue(dur=2.0)
        player.play = lambda pcm, rate, cancel: not cancel.wait(2.0)
        q.start(player)
        q.enqueue("long", "en", 1)
        self.assertTrue(wait_until(lambda: q.state() == "speaking"))
        q.skip()
        self.assertTrue(wait_until(lambda: timings))
        q.stop()
        self.assertEqual(timings[0][1]["outcome"], "cut_off")
        self.assertIn("play_start", timings[0][1])


def seg(src="Hello", tgt="สวัสดี", s="en", t="th", **timing):
    return Segment(0, 1.5, src, tgt, s, t, timing=timing or None)


class LatencyLogTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / "lat.jsonl"

    def test_row_has_every_stage(self):
        lg = LatencyLog(self.path)
        lg.recognized(1, seg(speech_start=10.0, speech_end=11.5, first_interim=10.6,
                             final=12.3, mode="en>th"), shown=12.35, enqueued=True)
        self.assertEqual(lg.rows, [])  # waits for the voice outcome
        lg.tts(Utterance("สวัสดี", "th", (1,)),
               {"outcome": "played", "cached": False, "synth_start": 12.4, "synth_end": 13.0,
                "play_start": 13.02, "output_latency": 0.08, "rate_wait_s": 0.0})
        lg.close()
        r = json.loads(self.path.read_text(encoding="utf-8").strip())
        self.assertEqual(r["outcome"], "played")
        expect = {"first_interim_s": 0.6, "recognize_s": 0.8, "ui_s": 0.05, "caption_s": 0.85,
                  "queue_s": 0.05, "synth_s": 0.6, "play_s": 0.1, "total_s": 1.6}
        for k, v in expect.items():
            self.assertAlmostEqual(r[k], v, places=2, msg=k)
        self.assertNotIn("src_text", r)  # app logs keep meeting content out
        self.assertEqual(r["mode"], "en>th")

    def test_include_text_for_measurements(self):
        lg = LatencyLog(None, include_text=True)
        lg.recognized(1, seg(), shown=1.0, enqueued=False)
        self.assertEqual(lg.rows[0]["src_text"], "Hello")
        self.assertEqual(lg.rows[0]["outcome"], "not_voiced")

    def test_merged_utterance_fills_every_segment(self):
        lg = LatencyLog(None)
        for i in (1, 2):
            lg.recognized(i, seg(speech_end=float(i), final=i + 0.5), shown=i + 0.6, enqueued=True)
        lg.tts(Utterance("x y", "th", (1, 2)), {"outcome": "played", "synth_start": 3.0,
                                                "synth_end": 3.5, "play_start": 3.5})
        self.assertEqual([(r["seg"], r["merged_with"], r["total_s"]) for r in lg.rows],
                         [(1, [2], 2.5), (2, [1], 1.5)])

    def test_close_marks_unspoken_and_unvoiced(self):
        lg = LatencyLog(None)
        lg.recognized(1, seg(), shown=1.0, enqueued=True)
        lg.recognized(2, seg(), shown=1.0, enqueued=True)
        lg.unvoiced(2, "muted")
        lg.close()
        self.assertEqual([(r["seg"], r["outcome"]) for r in lg.rows],
                         [(2, "muted"), (1, "not_spoken")])
        lg.recognized(3, seg(), shown=1.0, enqueued=False)  # after close: ignored
        self.assertEqual(len(lg.rows), 2)

    def test_unwritable_path_does_not_raise(self):
        blocker = Path(self.dir.name) / "file"
        blocker.write_text("x")
        with self.assertLogs("translator.latency", "WARNING"):
            lg = LatencyLog(blocker / "sub" / "lat.jsonl")  # parent is a file
        lg.recognized(1, seg(), shown=1.0, enqueued=False)
        self.assertEqual(len(lg.rows), 1)

    def test_stage_table_and_percentile(self):
        rows = [dict(kind="speech", outcome="played", **stages(
            {"speech_end": 0.0, "final": x, "shown": x, "synth_start": x, "synth_end": x,
             "play_start": x})) for x in (1.0, 2.0, 3.0, 4.0)]
        table = stage_table(rows)
        self.assertIn("| Speech end → final text (Azure) | 4 | 2.50 s | 3.70 s | 4.00 s |", table)
        self.assertIn("| Speech start → first partial caption | 0 |", table)
        self.assertEqual(latency.percentile([5.0], 90), 5.0)


class AppLatencyTest(unittest.TestCase):
    def start(self, f, setting="meeting"):
        app = headless_app(setting)
        app._apply_setting()
        app.on_start()
        self.assertTrue(run_ui_until(app, lambda: app.state is State.RUNNING))
        self.addCleanup(lambda: (app.on_stop(),
                                 run_ui_until(app, lambda: app.state is State.IDLE)))
        return app

    def test_session_logs_and_shows_last_delay(self):
        with Fakes() as f:
            app = self.start(f)
            tts = f.ttss[0]
            on_timing = tts.kwargs["on_timing"]
            app._show_final(seg(speech_end=time.monotonic() - 1.0, final=time.monotonic()))
            lat = app.latency
            self.assertIn(1, lat._open)
            now = time.monotonic()
            on_timing(Utterance("สวัสดี", "th", (1,)),
                      {"outcome": "played", "synth_start": now, "synth_end": now + 0.4,
                       "play_start": now + 0.4})
            self.assertTrue(run_ui_until(app, lambda: app._last_delay is not None))
            text = app.queue_label.configure.call_args.kwargs["text"]
            self.assertIn("last delay after speech: text 1.0 s, voice 1.4 s", text)
            path = lat.path
            app.on_stop()
            self.assertTrue(run_ui_until(app, lambda: app.state is State.IDLE))
            rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([r["outcome"] for r in rows], ["played"])
            self.assertEqual(path.parent, config.LOG_DIR)

    def test_muted_and_echo_are_logged(self):
        with Fakes() as f:
            app = self.start(f, "onsite")
            f.ttss[0].muted = True
            app._show_final(seg())
            app.echo.spoken("Thank you very much", "en")
            app._show_final(seg("Thank you very much", "ขอบคุณมาก"))
            outcomes = [(r["kind"], r["outcome"]) for r in app.latency.rows]
            self.assertEqual(outcomes, [("speech", "muted"), ("ignored", "echo")])

    def test_bad_silence_setting_fails_start(self):
        import os
        from unittest import mock
        with Fakes(), mock.patch.dict(os.environ, {"SEGMENTATION_SILENCE_MS": "fast"}):
            app = headless_app()
            app.on_start()
            self.assertTrue(run_ui_until(app, lambda: app.state is State.IDLE))
            self.assertIn("SEGMENTATION_SILENCE_MS", app.statuses[-2][1] + app.statuses[-1][1])


class ReportToolTest(unittest.TestCase):
    def test_report_from_app_logs(self):
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from tools.latency_report import report
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "latency-x.jsonl"
            lg = LatencyLog(path)
            lg.recognized(1, seg(speech_end=0.0, final=0.8), shown=0.85, enqueued=True)
            lg.tts(Utterance("x", "th", (1,)), {"outcome": "played", "synth_start": 0.9,
                                                "synth_end": 1.3, "play_start": 1.3})
            lg.recognized(2, seg("ขอบคุณ", "Thank you", "th", "en", speech_end=5.0, final=6.0),
                          shown=6.0, enqueued=True)
            lg.close()
            text = report([path])
        self.assertIn("2 utterances", text)
        self.assertIn("## English → Thai", text)
        self.assertIn("## Thai → English", text)
        self.assertIn("| Speech end → translated voice starts | 1 | 1.30 s |", text)
        self.assertIn("'not_spoken': 1", text)


class SilenceConfigTest(unittest.TestCase):
    def test_parse(self):
        import os
        from unittest import mock
        for raw, want in [("", None), ("500", 500)]:
            with mock.patch.dict(os.environ, {"SEGMENTATION_SILENCE_MS": raw}):
                self.assertEqual(config.segmentation_silence_ms(), want)
        for raw in ("50", "9000", "x"):
            with mock.patch.dict(os.environ, {"SEGMENTATION_SILENCE_MS": raw}):
                with self.assertRaises(config.ConfigError):
                    config.segmentation_silence_ms()


if __name__ == "__main__":
    unittest.main()
