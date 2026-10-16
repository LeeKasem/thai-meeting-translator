"""Start/stop lifecycle and TTS cancellation, headless (no mic, key or network)."""
import random
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import azure.cognitiveservices.speech as speechsdk

from translator import session as sessmod
from translator import ui
from translator.audio import Player
from translator.session import Cancelled, Session, State, StateMachine
from translator.tts import TtsQueue


def wait_until(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.005)
    return cond()


# --------------------------------------------------------------- state machine
class StateMachineTest(unittest.TestCase):
    def test_happy_path(self):
        sm = StateMachine()
        for to in (State.STARTING, State.RUNNING, State.STOPPING, State.IDLE):
            self.assertTrue(sm.transition(to))
        self.assertIs(sm.state, State.IDLE)

    def test_stop_during_start(self):
        sm = StateMachine()
        sm.transition(State.STARTING)
        self.assertTrue(sm.transition(State.STOPPING))
        self.assertFalse(sm.transition(State.RUNNING))  # late "started" refused

    def test_invalid_transitions_refused(self):
        sm = StateMachine()
        for bad in (State.RUNNING, State.STOPPING, State.IDLE):
            self.assertFalse(sm.transition(bad))
        sm.transition(State.STARTING)
        self.assertFalse(sm.transition(State.STARTING))  # double start
        self.assertFalse(sm.transition(State.IDLE))
        sm.transition(State.STOPPING)
        self.assertFalse(sm.transition(State.STOPPING))  # double stop
        self.assertFalse(sm.transition(State.STARTING))  # start while stopping

    def test_concurrent_start_only_one_wins(self):
        sm = StateMachine()
        wins = []
        barrier = threading.Barrier(16)

        def go():
            barrier.wait()
            if sm.transition(State.STARTING):
                wins.append(1)
        ts = [threading.Thread(target=go) for _ in range(16)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(len(wins), 1)


# --------------------------------------------------------------------- session
class SessionTest(unittest.TestCase):
    def test_own_after_stop_closes_immediately(self):
        s = Session()
        s.stop()
        closed = []
        with self.assertRaises(Cancelled):
            s.own("x", "obj", closed.append)
        self.assertEqual(closed, ["obj"])

    def test_stop_interrupts_waits_for_start_then_closes_in_reverse(self):
        s = Session()
        log = []
        gate = threading.Event()

        def starter(sess):
            sess.own("a", "a", lambda o: log.append("close a"))
            sess.own("b", "b", lambda o: log.append("close b"),
                     interrupt=lambda: (log.append("interrupt b"), gate.set()))
            gate.wait(2)  # a blocking connect that the interrupt aborts
            log.append("start returned")
            try:
                sess.own("c", "c", lambda o: log.append("close c (late)"))
            except Cancelled:
                pass
        s.run_start(starter)
        self.assertTrue(wait_until(lambda: len(s._interrupts) == 1))
        s.stop()
        self.assertEqual(log, ["interrupt b", "start returned", "close c (late)",
                               "close b", "close a"])

    def test_concurrent_stop_waits_for_teardown(self):
        s = Session()
        release = threading.Event()
        done = []
        s.own("slow", None, lambda o: (release.wait(2), done.append("closed")))
        t = threading.Thread(target=s.stop)
        t.start()
        time.sleep(0.05)
        threading.Timer(0.1, release.set).start()
        s.stop()  # must not return before the first stop finished closing
        self.assertEqual(done, ["closed"])
        t.join(2)


# ----------------------------------------------------------- app-level (fakes)
class Fakes:
    """Records every resource created so tests can check nothing is left open."""

    def __init__(self):
        self.captures, self.pumps, self.ttss, self.controllers, self.players = [], [], [], [], []
        self.capture_gate = None      # threading.Event: block capture.start until set
        self.capture_entered = threading.Event()
        self.capture_error = None
        self.preflight_gate = None
        self.preflight_entered = threading.Event()
        self.connect_delay = 0.0
        self.log_dir = tempfile.TemporaryDirectory()  # latency logs from app sessions
        f = self

        class Capture:
            def __init__(self, pa):
                self.open = False
                f.captures.append(self)

            def start(self, *a):
                f.capture_entered.set()
                if f.capture_gate is not None:
                    f.capture_gate.wait(3)
                if f.capture_error:
                    raise f.capture_error
                self.open = True

            def stop(self):
                self.open = False

        class Pump:
            seconds_sent = 0.0

            def __init__(self, is_paused, buffer_s=5.0):
                self.running = False
                self.is_paused = is_paused
                f.pumps.append(self)

            def start(self):
                self.running = True

            def stop(self):
                self.running = False

            def feed(self, *a):
                pass

        class Tts:
            def __init__(self, *a, **k):
                self.running = False
                self.interrupted = False
                self.enqueued = []
                self.seg_ids = []
                self.replays = []
                self.behind = 0.0
                self.kwargs = k
                self.busy = False
                self.tts_state = "idle"
                self.skips = 0
                self.waiting = 0
                self.skipped = 0
                self.muted = False
                f.ttss.append(self)

            def set_muted(self, m):
                pass

            def start(self, player):
                self.player = player
                self.running = True

            def interrupt(self):
                self.interrupted = True

            def stop(self):
                self.running = False
                self.player.closed = True

            def enqueue(self, text, lang, seg_id=None):
                if self.muted:
                    return False
                self.enqueued.append((text, lang))
                self.seg_ids.append(seg_id)
                return True

            def replay(self, text, lang, seg_id=None):
                self.replays.append((text, lang, seg_id))
                return True

            def behind_seconds(self):
                return self.behind

            def is_busy(self):
                return self.busy

            def state(self):
                return self.tts_state

            def skip(self):
                self.skips += 1

        class Controller:
            def __init__(self, *a, mode=ui.config.DEFAULT_MODE, **k):
                self.mode = mode
                self.modes = []
                self.is_switching = False
                self.stopped = threading.Event()
                self.started = False
                f.controllers.append(self)

            def start(self):
                if self.stopped.is_set():
                    return
                self.started = True
                self.stopped.wait(f.connect_delay)  # connect; stop() aborts it

            def stop(self):
                self.stopped.set()

            def set_mode(self, mode):
                self.mode = mode
                self.modes.append(mode)

        def make_player(pa, dev):
            p = SimpleNamespace(closed=False)
            f.players.append(p)
            return p

        def preflight(creds):
            f.preflight_entered.set()
            if f.preflight_gate is not None:
                f.preflight_gate.wait(3)

        self.patches = [
            mock.patch.multiple(ui, AudioCapture=Capture, AudioPump=Pump, TtsQueue=Tts,
                                RecognitionController=Controller, Player=make_player,
                                Transcript=lambda *a: SimpleNamespace(txt_path="fake.txt",
                                                                      add=lambda seg: None)),
            mock.patch.object(ui.config, "load_credentials", return_value=object()),
            mock.patch.object(ui.config, "preflight_check", side_effect=preflight),
            mock.patch.object(ui.config, "load_voices", return_value=dict(ui.config.DEFAULT_VOICES)),
            mock.patch.object(ui.config, "LOG_DIR", Path(self.log_dir.name)),
            mock.patch.object(ui.winmic, "mic_blocked", return_value=False),
            mock.patch.object(ui.voices, "chosen", return_value={}),
        ]

    def __enter__(self):
        for p in self.patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self.patches):
            p.stop()
        try:
            self.log_dir.cleanup()
        except OSError:
            pass  # a session still holding its log open; the OS temp dir is fine

    def leaks(self):
        out = [f"capture#{i}" for i, c in enumerate(self.captures) if c.open]
        out += [f"pump#{i}" for i, p in enumerate(self.pumps) if p.running]
        out += [f"tts#{i}" for i, t in enumerate(self.ttss) if t.running]
        out += [f"player#{i}" for i, p in enumerate(self.players) if not p.closed]
        out += [f"controller#{i}" for i, c in enumerate(self.controllers)
                if not c.stopped.is_set()]
        return out


class Var:
    """Stand-in for a Tk variable."""

    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


def headless_app(setting="meeting"):
    if ui.theme.T is None:
        ui.theme.init(None)  # colour tokens only; no Tk root needed
    app = ui.App.__new__(ui.App)
    app.pa = object()
    app.usage = SimpleNamespace(new_session=lambda: None, save=lambda: None,
                                session_audio_s=0.0, session_chars=0, warnings=lambda: [])
    app._ui_q = ui.queue.Queue()
    app.sm = StateMachine()
    app._error_shown = False
    app.session = None
    app.transcript = None
    app._pause_while_speaking = False
    app._last_delay = None
    dev = SimpleNamespace(name="dev", render_name="dev", kind="mic")
    app.devices = SimpleNamespace(outputs=[dev], mics=[dev], loopbacks=[])
    app._input_choices = lambda: [dev]
    app._selected = lambda combo, devs: dev
    for w in ("record_btn", "in_combo", "out_combo", "rb_loop", "rb_mic", "refresh_btn",
              "status_label", "queue_label", "messages", "usage_label", "rb_meeting", "rb_onsite",
              "pause_cb", "turn_frame", "lang_row", "phase_label", "skip_btn", "head",
              "setting_tabs", "setting_hint", "options_body", "options_btn", "options_summary",
              "src_select", "tgt_select", "swap_btn", "voice_btn", "warn_label", "card"):
        setattr(app, w, mock.MagicMock())
    app.mode_rbs = []
    app.options_open = True
    app._options_open_before = True
    app._compact = False
    app._head_hidden = False
    app._fit_job = None
    app._schedule_fit = lambda: None
    app._voice_choice = {}
    app._level = 0.0
    app._playing_ids = ()
    app._status_state = "idle"
    app._interim_at = 0.0
    app.setting_var = Var(setting)
    app.source_var = Var("mic")
    app.pause_var = Var(False)
    app.auto_var = Var(ui.config.ONSITE_AUTO_ALTERNATE)
    app.turn = ui.TurnTaker()
    app.floor_var = Var(app.turn.side)
    app.echo = None
    app._onsite_live = False
    app._segs = {}
    app._last_seg_id = None
    app._on_source_change = lambda: None
    app._update_feedback_warning = lambda: None
    app.mute_var = SimpleNamespace(get=lambda: False)
    app.mode_var = Var(ui.config.DEFAULT_MODE)
    app.statuses = []
    app.set_status = lambda st, m: app.statuses.append((st, m))
    app._update_usage_label = lambda: None
    app._note = lambda text: None
    return app


def run_ui_until(app, cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app._drain_once()
        if cond():
            return True
        time.sleep(0.005)
    app._drain_once()
    return cond()


class AppLifecycleTest(unittest.TestCase):
    def errors(self, app):
        return [m for st, m in app.statuses if st == "error"]

    def test_normal_start_then_stop(self):
        with Fakes() as f:
            app = headless_app()
            app.on_start()
            self.assertTrue(run_ui_until(app, lambda: app.state is State.RUNNING))
            app.on_stop()
            self.assertTrue(run_ui_until(app, lambda: app.state is State.IDLE))
            self.assertEqual(f.leaks(), [])
            self.assertEqual(self.errors(app), [])
            self.assertTrue(f.ttss[0].interrupted)

    def test_stop_while_capture_is_opening(self):
        """The bug from the review: capture opens after Stop's teardown."""
        with Fakes() as f:
            f.capture_gate = threading.Event()
            app = headless_app()
            app.on_start()
            self.assertTrue(f.capture_entered.wait(3))
            app.on_stop()
            time.sleep(0.05)
            sess = app.session
            f.capture_gate.set()  # the device finishes opening after Stop
            self.assertTrue(run_ui_until(app, lambda: app.state is State.IDLE))
            sess._start_thread.join(3)
            self.assertEqual(f.leaks(), [])
            self.assertEqual(self.errors(app), [])
            self.assertFalse(f.controllers[0].started)  # never connected after Stop
            self.assertIsNone(app.session)

    def test_capture_opening_longer_than_stop_waits(self):
        """Stop gives up waiting for the start thread; the late capture is
        still closed (by Session.own) and nothing runs on afterwards."""
        with Fakes() as f, mock.patch.object(sessmod.Session, "JOIN_TIMEOUT_S", 0.05):
            f.capture_gate = threading.Event()
            app = headless_app()
            app.on_start()
            self.assertTrue(f.capture_entered.wait(3))
            sess = app.session
            app.on_stop()
            self.assertTrue(run_ui_until(app, lambda: app.state is State.IDLE))
            self.assertTrue(sess._start_thread.is_alive())  # really outlived the wait
            f.capture_gate.set()  # opens well after teardown finished
            sess._start_thread.join(3)
            self.assertEqual(f.leaks(), [])
            self.assertFalse(f.controllers[0].started)
            self.assertEqual(self.errors(app), [])

    def test_stop_during_preflight_acquires_nothing(self):
        with Fakes() as f:
            f.preflight_gate = threading.Event()
            app = headless_app()
            app.on_start()
            self.assertTrue(f.preflight_entered.wait(3))
            app.on_stop()
            f.preflight_gate.set()
            self.assertTrue(run_ui_until(app, lambda: app.state is State.IDLE))
            self.assertEqual((f.pumps, f.captures, f.ttss), ([], [], []))

    def test_stop_during_connect_aborts_it(self):
        with Fakes() as f:
            f.connect_delay = 5.0
            app = headless_app()
            app.on_start()
            self.assertTrue(wait_until(lambda: f.controllers and f.controllers[0].started))
            t0 = time.monotonic()
            app.on_stop()
            self.assertTrue(run_ui_until(app, lambda: app.state is State.IDLE))
            self.assertLess(time.monotonic() - t0, 1.0)
            self.assertEqual(f.leaks(), [])

    def test_double_start_and_double_stop_are_noops(self):
        with Fakes() as f:
            f.preflight_gate = threading.Event()
            app = headless_app()
            app.on_start()
            first = app.session
            app.on_start()
            self.assertIs(app.session, first)
            f.preflight_gate.set()
            self.assertTrue(run_ui_until(app, lambda: app.state is State.RUNNING))
            app.on_stop()
            app.on_stop()
            self.assertTrue(run_ui_until(app, lambda: app.state is State.IDLE))
            self.assertEqual(len(f.pumps), 1)
            self.assertEqual(f.leaks(), [])

    def test_start_failure_tears_down_and_shows_error(self):
        with Fakes() as f:
            f.capture_error = OSError("device busy")
            app = headless_app()
            app.on_start()
            self.assertTrue(run_ui_until(app, lambda: app.state is State.IDLE))
            self.assertEqual(f.leaks(), [])
            self.assertEqual(len(self.errors(app)), 1)
            self.assertIn("device busy", self.errors(app)[0])

    def test_late_callback_from_old_session_is_dropped(self):
        with Fakes():
            app = headless_app()
            app.on_start()
            self.assertTrue(run_ui_until(app, lambda: app.state is State.RUNNING))
            old = app.session
            app.on_stop()
            self.assertTrue(run_ui_until(app, lambda: app.state is State.IDLE))
            app.on_start()
            self.assertTrue(run_ui_until(app, lambda: app.state is State.RUNNING))
            hit = []
            app.post_for(old, lambda: hit.append("old"))
            app.post_for(app.session, lambda: hit.append("new"))
            app._drain_once()
            self.assertEqual(hit, ["new"])
            app.on_stop()
            run_ui_until(app, lambda: app.state is State.IDLE)

    def test_final_result_while_stopping_is_not_spoken(self):
        with Fakes() as f:
            app = headless_app()
            app.on_start()
            self.assertTrue(run_ui_until(app, lambda: app.state is State.RUNNING))
            seg = ui.Segment(0, 1, "hi", "สวัสดี")
            app._show_final(seg)
            app.sm.transition(State.STOPPING)
            app._show_final(seg)
            self.assertEqual(f.ttss[0].enqueued, [("สวัสดี", "th")])
            app.session.stop()  # the state was forced; tear the session down too

    def test_rapid_start_stop_stress(self):
        rnd = random.Random(1234)
        with Fakes() as f:
            f.connect_delay = 0.02
            app = headless_app()
            for i in range(60):
                app.on_start()
                time.sleep(rnd.choice([0, 0, 0.001, 0.005, 0.02, 0.05]))
                if rnd.random() < 0.3:
                    app.on_start()  # stray double click
                app._drain_once()
                app.on_stop()
                if rnd.random() < 0.3:
                    app.on_stop()
                self.assertTrue(run_ui_until(app, lambda: app.state is State.IDLE),
                                f"iteration {i} stuck in {app.state}")
                self.assertEqual(f.leaks(), [], f"iteration {i}")
            self.assertEqual(self.errors(app), [])


# ------------------------------------------------------------- TTS cancellation
class FakeFuture:
    def __init__(self, fn):
        self._fn = fn

    def get(self):
        return self._fn()


class FakeSynth:
    def __init__(self, gate=None):
        self.gate = gate
        self.ssml = []
        self.calls = 0
        self.stop_speaking_calls = 0

    def speak_ssml_async(self, ssml):
        self.calls += 1
        self.ssml.append(ssml)

        def done():
            if self.gate is not None:
                self.gate.wait(3)
            return SimpleNamespace(reason=speechsdk.ResultReason.SynthesizingAudioCompleted,
                                   audio_data=b"\0\0" * 24000)
        return FakeFuture(done)

    def stop_speaking_async(self):
        self.stop_speaking_calls += 1


class FakePlayer:
    def __init__(self, block_until_cancel=False):
        self.block = block_until_cancel
        self.plays = 0
        self.playing = threading.Event()
        self.closed = False
        self.closed_while_playing = False
        self._in_play = False

    def play(self, pcm, rate, cancel):
        self.plays += 1
        self._in_play = True
        self.playing.set()
        try:
            while self.block and not cancel.is_set():
                time.sleep(0.01)
        finally:
            self._in_play = False

    def close(self):
        self.closed_while_playing = self._in_play
        self.closed = True


def make_tts(synth, events):
    q = TtsQueue(SimpleNamespace(key="k", region="r"),
                 on_queue=lambda n, s: events.append(("queue", n)),
                 on_spoken_chars=lambda n: events.append(("chars", n)),
                 on_error=lambda m: events.append(("error", m)))
    q._make_synth = lambda: synth
    return q


class TtsCancellationTest(unittest.TestCase):
    def test_stop_during_playback_cuts_audio_quickly(self):
        events, player = [], FakePlayer(block_until_cancel=True)
        q = make_tts(FakeSynth(), events)
        q.start(player)
        q.enqueue("สวัสดีครับ", "th")
        self.assertTrue(player.playing.wait(2))
        t0 = time.monotonic()
        q.stop()
        self.assertLess(time.monotonic() - t0, 0.5)
        self.assertTrue(player.closed)
        self.assertFalse(player.closed_while_playing)
        self.assertFalse(q.is_busy())

    def test_stop_during_synthesis_discards_result_and_fences_callbacks(self):
        gate = threading.Event()
        events, player = [], FakePlayer()
        synth = FakeSynth(gate)
        q = make_tts(synth, events)
        q.start(player)
        q.enqueue("one", "th")
        q.enqueue("two", "th")
        self.assertTrue(wait_until(lambda: synth.calls == 1))
        q.stop(join_timeout=0.2)  # synthesis still pending
        self.assertTrue(player.closed)
        self.assertEqual(synth.stop_speaking_calls, 1)
        n_events = len(events)
        gate.set()  # the request completes after Stop
        q._thread.join(2)
        self.assertFalse(q._thread.is_alive())
        self.assertEqual(player.plays, 0)
        self.assertEqual(synth.calls, 1)  # "two" was dropped, never synthesized
        self.assertEqual(len(events), n_events)  # no callbacks after stop

    def test_enqueue_after_stop_is_ignored(self):
        events = []
        q = make_tts(FakeSynth(), events)
        q.start(FakePlayer())
        q.stop()
        q.enqueue("late", "th")
        self.assertEqual(q.waiting, 0)
        q.stop()  # idempotent

    def test_start_failure_closes_player(self):
        q = make_tts(None, [])

        def boom():
            raise RuntimeError("no synth")
        q._make_synth = boom
        p = FakePlayer()
        with self.assertRaises(RuntimeError):
            q.start(p)
        self.assertTrue(p.closed)


class PlayerTest(unittest.TestCase):
    def make(self, write_delay=0.0):
        calls = []

        class Stream:
            def write(self, data):
                calls.append("write")
                time.sleep(write_delay)

            def stop_stream(self):
                calls.append("stop_stream")

            def close(self):
                calls.append("close")
        pa = SimpleNamespace(open=lambda **k: Stream())
        dev = SimpleNamespace(rate=24000, channels=1, index=0)
        return Player(pa, dev), calls

    def test_close_aborts_instead_of_draining(self):
        p, calls = self.make()
        p.close()
        p.close()
        self.assertEqual(calls, ["close"])  # no stop_stream (which drains)

    def test_close_waits_for_inflight_write_and_play_stops(self):
        p, calls = self.make(write_delay=0.05)
        cancel = threading.Event()
        t = threading.Thread(target=p.play, args=(b"\0\0" * 24000 * 5, 24000, cancel))
        t.start()
        self.assertTrue(wait_until(lambda: "write" in calls))
        cancel.set()
        t0 = time.monotonic()
        p.close()
        t.join(1)
        self.assertLess(time.monotonic() - t0, 0.3)
        self.assertEqual(calls[-1], "close")
        self.assertLess(calls.count("write"), 5 * 10)  # stopped early
        self.assertFalse(p.play(b"\0\0" * 100, 24000, threading.Event()))  # closed


if __name__ == "__main__":
    unittest.main()
