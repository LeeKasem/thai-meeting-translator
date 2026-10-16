"""Long-form use (lectures, hours-long sessions): reconnects never give up,
outages are reported, mute = subtitles only with a 100 % open mic, onsite
uses language detection instead of turn switches, and memory stays bounded.

The real-world part of this (30+ minutes with Azure, a mic and a speaker)
is tools/soak.py; these tests run the same logic offline, accelerated.
"""
import gc
import threading
import time
import tkinter as tk
import tracemalloc
import unittest
from types import SimpleNamespace
from unittest import mock

import azure.cognitiveservices.speech as speechsdk

from translator import config, paint, theme, ui
from translator.audio import AudioPump
from translator.latency import LatencyLog
from translator.onsite import TurnTaker, mode_for
from translator.recognition import RecognitionController, Segment
from translator.session import State

from test_lifecycle import Fakes, headless_app, run_ui_until, wait_until
from test_queue_replay import running


def _canceled(code):
    return SimpleNamespace(reason=speechsdk.CancellationReason.Error, error_code=code,
                           error_details="details")


class ReconnectForeverTest(unittest.TestCase):
    def make(self, fail_opens=0):
        ev = {"fatal": [], "status": [], "opens": 0, "gaps": [], "messages": []}
        c = RecognitionController(
            config.Credentials("k", "r"), AudioPump(lambda: False),
            on_interim=lambda *a: None, on_final=lambda s: None,
            on_status=lambda st, m: (ev["status"].append(st), ev["messages"].append(m)),
            on_fatal=lambda m: ev["fatal"].append(m),
            on_gap=lambda at, secs: ev["gaps"].append(secs))
        left = {"fail": fail_opens}

        def fake_open():
            with c._lock:
                if c._stop.is_set():
                    return
                if left["fail"] > 0:
                    left["fail"] -= 1
                    raise RuntimeError("SDK object creation failed")
                c._gen += 1
                ev["opens"] += 1
        c._open = fake_open
        c._gen = 1
        return c, ev

    def test_never_gives_up_by_default(self):
        self.assertIsNone(config.MAX_RECONNECT_ATTEMPTS)
        c, ev = self.make()
        c._on_connected(1)
        with mock.patch.object(config, "RECONNECT_BACKOFF_S", (0.005,)):
            for _ in range(12):  # well past the old cap of 5
                c._on_canceled(c._gen, _canceled(speechsdk.CancellationErrorCode.ServiceTimeout))
                self.assertTrue(wait_until(lambda n=ev["opens"]: ev["opens"] == n + 1, timeout=2))
        c.stop()
        self.assertEqual(ev["opens"], 12)
        self.assertEqual(ev["fatal"], [])
        self.assertIn("attempt 12)", ev["messages"][-1])

    def test_failed_reopen_is_retried_not_fatal(self):
        c, ev = self.make(fail_opens=3)
        c._on_connected(1)
        with mock.patch.object(config, "RECONNECT_BACKOFF_S", (0.005,)):
            c._on_canceled(1, _canceled(speechsdk.CancellationErrorCode.ConnectionFailure))
            self.assertTrue(wait_until(lambda: ev["opens"] == 1, timeout=2))
        c.stop()
        self.assertEqual(ev["fatal"], [])
        self.assertEqual(ev["status"].count("reconnecting"), 4)

    def test_gap_reported_once_when_back(self):
        c, ev = self.make()
        c._on_connected(1)
        self.assertEqual(ev["gaps"], [])
        with mock.patch.object(config, "RECONNECT_BACKOFF_S", (0.05,)):
            c._on_canceled(1, _canceled(speechsdk.CancellationErrorCode.ConnectionFailure))
            self.assertTrue(wait_until(lambda: ev["opens"] == 1, timeout=2))
            # the new connection drops before it ever connects: same outage
            c._on_canceled(c._gen, _canceled(speechsdk.CancellationErrorCode.ConnectionFailure))
            self.assertTrue(wait_until(lambda: ev["opens"] == 2, timeout=2))
        c._on_connected(c._gen)
        c._on_connected(c._gen)  # a second connected event is not a second gap
        c.stop()
        self.assertEqual(len(ev["gaps"]), 1)
        self.assertGreaterEqual(ev["gaps"][0], 0.1)  # both backoffs
        self.assertEqual(c.gaps, 1)

    def test_reconnect_buffer_covers_a_typical_outage(self):
        self.assertGreaterEqual(config.RECONNECT_BUFFER_S, 15)

    def test_stop_still_ends_an_endless_retry_loop(self):
        c, ev = self.make(fail_opens=10 ** 6)
        c._on_connected(1)
        with mock.patch.object(config, "RECONNECT_BACKOFF_S", (0.005,)):
            c._on_canceled(1, _canceled(speechsdk.CancellationErrorCode.ConnectionFailure))
            time.sleep(0.1)
            c.stop()
            for t in [t for t in threading.enumerate() if t.name == "reconnect"]:
                t.join(timeout=2)
                self.assertFalse(t.is_alive())


class MuteTest(unittest.TestCase):
    def test_mute_while_speaking_stops_at_once_and_opens_the_gate(self):
        q, synth, player, _, playing = running(dur=2.0)
        q.enqueue("long sentence", "en", 1)
        q.enqueue("queued", "en", 2)
        self.assertTrue(wait_until(lambda: q.state() == "speaking"))
        self.assertTrue(q.is_busy())
        t0 = time.monotonic()
        q.set_muted(True)
        self.assertFalse(q.is_busy())  # gate open immediately, no echo tail
        self.assertTrue(wait_until(lambda: q.state() in ("idle", "tail") and q._current is None,
                                   timeout=1))
        self.assertLess(time.monotonic() - t0, 0.5)
        self.assertEqual(q.waiting, 0)
        time.sleep(0.2)
        self.assertEqual(len(player.played), 1)  # the queued one never played
        q.set_muted(False)
        self.assertEqual(q.waiting, 0)  # no backlog comes back
        q.enqueue("new", "en", 3)
        self.assertTrue(wait_until(lambda: len(player.played) == 2))
        q.stop()

    def test_app_mute_keeps_mic_ungated_even_onsite(self):
        with Fakes() as f:
            app = headless_app("onsite")
            app._apply_setting()
            app.on_start()
            self.assertTrue(run_ui_until(app, lambda: app.state is State.RUNNING))
            tts, gate = f.ttss[0], f.pumps[0].is_paused
            tts.busy = True
            self.assertTrue(gate())  # unmuted onsite: gated while the voice plays
            app.mute_var = SimpleNamespace(get=lambda: True, set=lambda v: None)
            app._on_mute()
            self.assertTrue(tts.muted)
            self.assertFalse(gate())  # muted: 100 % capture
            self.assertFalse(app._replay(1) if app._segs else False)
            app._segs[1] = Segment(0, 1, "Hello", "สวัสดี", "en", "th")
            self.assertFalse(app._replay(1))
            self.assertIn("muted", app.statuses[-1][1])
            self.assertEqual(f.controllers[0].modes, [])  # recognizer untouched
            app.on_stop()
            run_ui_until(app, lambda: app.state is State.IDLE)

    def test_muted_sentences_still_become_subtitles(self):
        with Fakes() as f:
            app = headless_app()
            app.mute_var = SimpleNamespace(get=lambda: True, set=lambda v: None)
            app.on_start()
            self.assertTrue(run_ui_until(app, lambda: app.state is State.RUNNING))
            f.ttss[0].muted = True
            for i in range(5):
                app._show_final(Segment(0, 1, f"s{i}", f"t{i}", "en", "th"))
            self.assertEqual(app.messages.add_message.call_count, 5)
            self.assertEqual(f.ttss[0].enqueued, [])
            app.on_stop()
            run_ui_until(app, lambda: app.state is State.IDLE)


class OnsiteAutoTest(unittest.TestCase):
    def test_turn_taker_defaults_to_auto(self):
        t = TurnTaker()
        self.assertIsNone(t.side)
        self.assertEqual(t.mode, config.MODE_AUTO)
        self.assertEqual(mode_for(None), config.MODE_AUTO)
        t.on_final("en")
        self.assertIsNone(t.poll("idle", False))  # no alternation in auto
        self.assertEqual(t.give_floor("th"), config.MODE_TH_EN)  # lock a side
        self.assertEqual(t.give_floor(None), config.MODE_AUTO)   # back to auto

    def test_onsite_session_uses_detection_without_reopening(self):
        with Fakes() as f:
            app = headless_app("onsite")
            app._apply_setting()
            app.on_start()
            self.assertTrue(run_ui_until(app, lambda: app.state is State.RUNNING))
            ctl, tts = f.controllers[0], f.ttss[0]
            self.assertEqual(ctl.mode, config.MODE_AUTO)
            app._show_final(Segment(0, 1, "How much?", "เท่าไร", "en", "th"))
            tts.tts_state = "idle"
            app._update_onsite()
            app._show_final(Segment(0, 1, "ร้อยบาท", "100 baht", "th", "en"))
            app._update_onsite()
            self.assertEqual(tts.enqueued, [("เท่าไร", "th"), ("100 baht", "en")])
            self.assertEqual(ctl.modes, [])  # no turn switch, no recognizer reopen
            app._give_floor("th")
            app._give_floor("auto")
            self.assertEqual(ctl.modes, [config.MODE_TH_EN, config.MODE_AUTO])
            app.on_stop()
            run_ui_until(app, lambda: app.state is State.IDLE)

    def test_auto_banner_text(self):
        self.assertIn("auto", ui.PHASE_BANNER[ui.Phase.LISTENING][1])


class BoundedMemoryTest(unittest.TestCase):
    def test_latency_rows_capped_in_app(self):
        lg = LatencyLog(keep_rows=50)
        for i in range(500):
            lg.unvoiced  # noqa: B018 - attribute exists
            lg.ignored(Segment(0, 1, f"s{i}", f"t{i}", "en", "th"), "echo")
        self.assertEqual(len(lg.rows), 50)
        lg.close()

    def test_two_hours_of_segments_headless(self):
        """~2 h at 25 sentences/min, with partials, mutes and gap notes: the
        history is kept, everything else stays bounded."""
        with Fakes() as f:
            app = headless_app()
            app.on_start()
            self.assertTrue(run_ui_until(app, lambda: app.state is State.RUNNING))
            n = 3000
            for i in range(n):
                app._show_interim(f"partial {i}", f"บางส่วน {i}", "en", "th")
                app._show_final(Segment(time.time(), time.time() + 2, f"Sentence {i} " * 3,
                                        f"ประโยค {i} " * 3, "en", "th",
                                        timing={"speech_end": time.monotonic(),
                                                "final": time.monotonic()}))
                if i % 500 == 250:
                    app.mute_var = SimpleNamespace(get=lambda: True, set=lambda v: None)
                    app._on_mute()
                elif i % 500 == 300:
                    app.mute_var = SimpleNamespace(get=lambda: False, set=lambda v: None)
                    app._on_mute()
            self.assertEqual(len(app._segs), n)  # full history kept for replay/export
            self.assertLessEqual(len(app.latency.rows), ui.LATENCY_ROWS_KEPT)
            app.on_stop()
            self.assertTrue(run_ui_until(app, lambda: app.state is State.IDLE))


class TkCase(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as e:
            self.skipTest(f"no display: {e}")
        self.addCleanup(self.root.destroy)
        theme.init(self.root, dark=False, reduced_motion=True)
        self.addCleanup(lambda: (paint._photos.clear(), theme.init(None)))
        self.root.geometry("600x500")


class MessageListCapTest(TkCase):
    def test_rendering_is_capped_but_history_kept(self):
        from translator import messages
        frame = tk.Frame(self.root, bg="#FFFFFF")
        frame.pack(fill="both", expand=True)
        ml = messages.MessageList(frame, on_replay=lambda i: None)
        ml.pack(fill="both", expand=True)
        self.root.update()

        def add(a, b):
            for i in range(a, b):
                ml.add_message(i, f"Sentence {i} in English.", f"ประโยคที่ {i}", "en", "th",
                               time.time())
                if i % 50 == 0:
                    ml.add_note(f"note {i}")
                    self.root.update_idletasks()

        tracemalloc.start()
        add(0, 400)
        self.root.update()
        gc.collect()
        items_a = len(ml.cv.find_all())
        mem_a = tracemalloc.get_traced_memory()[0]
        add(400, 2400)
        self.root.update()
        gc.collect()
        items_b = len(ml.cv.find_all())
        mem_b = tracemalloc.get_traced_memory()[0]
        tracemalloc.stop()
        self.assertEqual(len(ml.items), 2400 + 48)  # every message and note kept
        self.assertLessEqual(ml.rendered, messages.MAX_RENDERED + messages.EVICT_BATCH)
        self.assertLess(items_b, items_a * 1.3)  # canvas items bounded
        # Python-side growth: only the small per-message dicts (< 2 KB each).
        self.assertLess((mem_b - mem_a) / 2000, 2048)
        ml.show_earlier()
        self.root.update()
        self.assertGreater(ml.rendered, messages.MAX_RENDERED)
        self.assertTrue(ml.cv.find_withtag("earlier"))
        ml.scroll_to_end()

    def test_no_eviction_while_reading_older_lines(self):
        from translator import messages
        frame = tk.Frame(self.root, bg="#FFFFFF")
        frame.pack(fill="both", expand=True)
        ml = messages.MessageList(frame, on_replay=lambda i: None)
        ml.pack(fill="both", expand=True)
        self.root.update()
        for i in range(50):
            ml.add_message(i, f"s{i}", f"t{i}", "en", "th", time.time())
        self.root.update()
        ml._scroll(-3, "pages")
        first = ml._first
        for i in range(50, 400):
            ml.add_message(i, f"s{i}", f"t{i}", "en", "th", time.time())
        self.assertEqual(ml._first, first)  # the reader isn't moved
        ml.scroll_to_end()
        ml.add_message(400, "s", "t", "en", "th", time.time())
        self.assertLessEqual(ml.rendered, messages.MAX_RENDERED + messages.EVICT_BATCH)


class SubtitleViewTest(TkCase):
    def test_view_is_constant_size_and_closes(self):
        from translator.subtitle_view import SubtitleView
        closed = []
        v = SubtitleView(self.root, on_close=lambda: closed.append(1))
        self.root.update()
        v.show_interim("Hel", "สวั", "en", "th")
        for i in range(300):
            v.show_final(f"Sentence {i}", f"ประโยค {i}", "en", "th")
        self.root.update()
        n = len(v.cv.find_all())
        self.assertLessEqual(n, 6)
        v.zoom(1)
        v.toggle_source()
        self.root.update()
        v._escape()
        self.assertEqual(closed, [1])


class GapNoteTest(unittest.TestCase):
    def test_gap_note_says_what_was_kept(self):
        app = headless_app()
        notes = []
        app._note = notes.append
        app._note_gap(time.time(), 6)
        app._note_gap(time.time(), config.RECONNECT_BUFFER_S + 40)
        self.assertIn("buffered and translated", notes[0])
        self.assertIn("was not translated", notes[1])


if __name__ == "__main__":
    unittest.main()
