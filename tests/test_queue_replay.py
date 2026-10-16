"""Queue policies (catch-up vs in-order with merging) and replay. Offline."""
import threading
import time
import tkinter as tk
import unittest
from types import SimpleNamespace

from translator import config
from translator.recognition import Segment
from translator.session import State
from translator.tts import POLICY_CATCH_UP, POLICY_IN_ORDER, TtsQueue, Utterance

from test_lifecycle import FakeSynth, Fakes, headless_app, run_ui_until, wait_until


def queue(policy=POLICY_CATCH_UP, **kw):
    q = TtsQueue(SimpleNamespace(key="k", region="r"), on_queue=lambda *a: None,
                 on_spoken_chars=lambda n: None, on_error=lambda m: None,
                 policy=policy, **kw)
    q._running = True  # no worker: exercise the queue only
    return q


def texts(q):
    return [u.text for u in q._q]


class PolicyTest(unittest.TestCase):
    def test_unknown_policy_rejected(self):
        with self.assertRaises(ValueError):
            queue("newest_only")

    def test_catch_up_drops_oldest_but_never_replays(self):
        q = queue(POLICY_CATCH_UP, max_backlog=2)
        q.replay("again", "th", 1)
        for i in range(5):
            q.enqueue(f"s{i}", "th", i + 10)
        self.assertEqual(texts(q), ["again", "s3", "s4"])
        self.assertEqual(q.skipped, 3)

    def test_in_order_never_drops(self):
        q = queue(POLICY_IN_ORDER, max_backlog=2)
        for i in range(10):
            q.enqueue(f"s{i}", "en", i)
        self.assertEqual(texts(q), [f"s{i}" for i in range(10)])
        self.assertEqual(q.skipped, 0)

    def test_in_order_merges_same_language_runs(self):
        q = queue(POLICY_IN_ORDER, merge_max_chars=20)
        for i, (t, lang) in enumerate([("One.", "en"), ("Two.", "en"), ("สาม", "th"),
                                       ("Four is long.", "en"), ("Five is long.", "en")]):
            q.enqueue(t, lang, i + 1)
        got = []
        while q._q:
            u = q._take()
            got.append((u.text, u.lang, u.seg_ids))
        self.assertEqual(got, [("One. Two.", "en", (1, 2)), ("สาม", "th", (3,)),
                               ("Four is long.", "en", (4,)),  # merging would exceed 20
                               ("Five is long.", "en", (5,))])
        self.assertEqual(q.merged, 1)

    def test_catch_up_does_not_merge(self):
        q = queue(POLICY_CATCH_UP, max_backlog=5)
        q.enqueue("a", "en")
        q.enqueue("b", "en")
        self.assertEqual(q._take().text, "a")

    def test_replays_are_not_merged(self):
        q = queue(POLICY_IN_ORDER)
        q.enqueue("live one", "en", 2)
        q.replay("replayed", "en", 1)
        q.enqueue("live two", "en", 3)
        self.assertEqual(q._take(), Utterance("replayed", "en", (1,), replay=True))
        self.assertEqual(q._take().text, "live one live two")

    def test_replays_go_first_and_keep_their_order(self):
        q = queue(POLICY_IN_ORDER)
        q.enqueue("x", "en")
        q.enqueue("y", "en")
        q.replay("r1", "th")
        q.replay("r2", "en")
        self.assertEqual(texts(q), ["r1", "r2", "x", "y"])

    def test_replay_needs_a_running_queue(self):
        q = queue()
        q._running = False
        self.assertFalse(q.replay("x", "en"))

    def test_behind_estimate(self):
        q = queue(POLICY_IN_ORDER)
        q.enqueue("a" * 150, "en")   # ~10 s at 15 chars/s
        q.enqueue("ก" * 120, "th")   # ~10 s at 12 chars/s
        self.assertAlmostEqual(q.behind_seconds(), 20.0, places=1)
        self.assertEqual(q.pending_chars, 270)


class RecordingPlayer:
    """Plays for `dur` seconds (interruptible); records what was played."""

    def __init__(self, dur=0.05):
        self.dur = dur
        self.played = []
        self.closed = False

    def play(self, pcm, rate, cancel):
        self.played.append(pcm)
        cancel.wait(self.dur)

    def close(self):
        self.closed = True


class TextSynth(FakeSynth):
    """Returns the text itself as 'audio' so the test can see what was played."""

    def speak_ssml_async(self, ssml):
        import xml.etree.ElementTree as ET
        import azure.cognitiveservices.speech as speechsdk
        self.calls += 1
        self.ssml.append(ssml)
        text = ET.fromstring(ssml)[0].text
        return SimpleNamespace(get=lambda: SimpleNamespace(
            reason=speechsdk.ResultReason.SynthesizingAudioCompleted,
            audio_data=text.encode("utf-8")))


def running(policy=POLICY_IN_ORDER, dur=0.05, **kw):
    chars, playing = [], []
    q = TtsQueue(SimpleNamespace(key="k", region="r"), on_queue=lambda *a: None,
                 on_spoken_chars=chars.append, on_error=lambda m: None, policy=policy,
                 on_playing=lambda u: playing.append(u), **kw)
    synth, player = TextSynth(), RecordingPlayer(dur)
    q._make_synth = lambda: synth
    q.start(player)
    return q, synth, player, chars, playing


class ReplayPlaybackTest(unittest.TestCase):
    def test_replay_uses_cached_audio_no_new_request(self):
        q, synth, player, chars, _ = running()
        q.enqueue("Hello there", "en", 1)
        self.assertTrue(wait_until(lambda: len(player.played) == 1))
        self.assertTrue(q.replay("Hello there", "en", 1))
        self.assertTrue(wait_until(lambda: len(player.played) == 2))
        q.stop()
        self.assertEqual(synth.calls, 1)
        self.assertEqual(chars, [len("Hello there")])  # replay used no TTS characters
        self.assertEqual(player.played[0], player.played[1])

    def test_replay_plays_after_current_before_queue(self):
        q, synth, player, _, playing = running(dur=0.3)
        q.enqueue("first", "en", 1)
        self.assertTrue(wait_until(lambda: len(player.played) == 1))
        q.enqueue("next", "th", 2)
        q.replay("again", "en", 9)
        self.assertTrue(wait_until(lambda: len(player.played) == 3, timeout=3))
        q.stop()
        self.assertEqual([u.text for u in playing], ["first", "again", "next"])
        self.assertTrue(playing[1].replay)

    def test_in_order_merges_backlog_into_one_request(self):
        q, synth, player, _, playing = running(dur=0.3)
        q.enqueue("A.", "en", 1)
        self.assertTrue(wait_until(lambda: len(player.played) == 1))
        for i, t in enumerate(["B.", "C.", "D."]):
            q.enqueue(t, "en", i + 2)
        self.assertTrue(wait_until(lambda: len(player.played) == 2, timeout=3))
        q.stop()
        self.assertEqual([u.text for u in playing], ["A.", "B. C. D."])
        self.assertEqual(playing[1].seg_ids, (2, 3, 4))
        self.assertEqual(synth.calls, 2)

    def test_replay_plays_even_when_muted(self):
        q, synth, player, _, playing = running()
        q.set_muted(True)
        q.enqueue("live", "en", 1)  # muted: ignored
        q.replay("asked for", "en", 2)
        self.assertTrue(wait_until(lambda: len(player.played) == 1))
        time.sleep(0.1)
        q.stop()
        self.assertEqual([u.text for u in playing], ["asked for"])

    def test_cached_replay_not_held_by_rate_limit(self):
        q, synth, player, _, _ = running(per_minute=1)
        q.enqueue("only request", "en", 1)
        self.assertTrue(wait_until(lambda: len(player.played) == 1))
        t0 = time.monotonic()
        q.replay("only request", "en", 1)
        self.assertTrue(wait_until(lambda: len(player.played) == 2, timeout=2))
        self.assertLess(time.monotonic() - t0, 1.0)
        q.stop()

    def test_cache_is_bounded(self):
        q, synth, player, _, _ = running(cache_items=2)
        for i, t in enumerate(["one", "two", "three"]):
            q.enqueue(t, "en", i)
            self.assertTrue(wait_until(lambda n=i + 1: len(player.played) == n))
        q.replay("one", "en", 0)  # evicted: must be synthesized again
        self.assertTrue(wait_until(lambda: len(player.played) == 4))
        q.stop()
        self.assertEqual(synth.calls, 4)
        self.assertEqual(len(q._cache), 2)


class AppReplayTest(unittest.TestCase):
    def start(self, f, setting="meeting"):
        app = headless_app(setting)
        app._apply_setting()
        app.on_start()
        self.assertTrue(run_ui_until(app, lambda: app.state is State.RUNNING))
        self.addCleanup(lambda: (app.on_stop(),
                                 run_ui_until(app, lambda: app.state is State.IDLE)))
        return app

    def test_policy_per_setting(self):
        with Fakes() as f:
            self.start(f, "meeting")
            self.assertEqual(f.ttss[0].kwargs["policy"], POLICY_CATCH_UP)
        with Fakes() as f:
            self.start(f, "onsite")
            self.assertEqual(f.ttss[0].kwargs["policy"], POLICY_IN_ORDER)

    def test_segments_get_ids_and_replay_last(self):
        with Fakes() as f:
            app = self.start(f)
            app._show_final(Segment(0, 1, "Hello", "สวัสดี", "en", "th"))
            app._show_final(Segment(0, 1, "ขอบคุณ", "Thank you", "th", "en"))
            tts = f.ttss[0]
            self.assertEqual(tts.seg_ids, [1, 2])
            app._replay_last()
            app._replay(1)
            self.assertEqual(tts.replays, [("Thank you", "en", 2), ("สวัสดี", "th", 1)])
            self.assertFalse(app._replay(99))

    def test_replay_when_stopped_explains(self):
        with Fakes():
            app = headless_app()
            app._segs[1] = Segment(0, 1, "Hello", "สวัสดี", "en", "th")
            self.assertFalse(app._replay(1))
            self.assertIn("while the translator is running", app.statuses[-1][1])

    def test_behind_warning(self):
        with Fakes() as f:
            app = self.start(f, "onsite")
            f.ttss[0].behind = config.TTS_BEHIND_WARN_S + 5
            app._show_queue(4, 0)
            kw = app.queue_label.configure.call_args.kwargs
            self.assertIn("behind", kw["text"])
            from translator.widgets import C
            self.assertEqual(kw["fg"], C("danger_active"))


class MessageReplayClickTest(unittest.TestCase):
    """Real Tk canvas: clicking ▶ on a message card replays that message."""

    def test_click_replays_that_message(self):
        try:
            root = tk.Tk()
        except tk.TclError as e:
            self.skipTest(f"no display: {e}")
        self.addCleanup(root.destroy)
        from translator import paint, theme
        from translator.messages import MessageList
        theme.init(root)
        # Runs before root.destroy: drop Tk images/fonts while Tk is alive, on
        # this thread (otherwise Tcl aborts at exit: "deleted by the wrong thread").
        self.addCleanup(lambda: (paint._photos.clear(), theme.init(None)))
        with Fakes() as f:
            app = headless_app()
            app.on_start()
            self.assertTrue(run_ui_until(app, lambda: app.state is State.RUNNING))
            frame = tk.Frame(root, bg="#FFFFFF")
            frame.pack(fill="both", expand=True)
            app.messages = MessageList(frame, on_replay=app._replay)
            app.messages.pack(fill="both", expand=True)
            root.geometry("600x500")
            root.update()
            app._show_final(Segment(0, 1, "Hello", "สวัสดี", "en", "th"))
            app._show_final(Segment(0, 1, "ขอบคุณ", "Thank you", "th", "en"))
            root.update()
            cv = app.messages.cv
            x0, y0, x1, y1 = app.messages.replay_bbox(2)  # the ▶ of message 2
            cx, cy = (x0 + x1) // 2, (y0 + y1) // 2 - int(cv.canvasy(0))
            # Tk tracks the item under the mouse via Motion, then the click fires.
            cv.event_generate("<Motion>", x=cx, y=cy)
            cv.event_generate("<Button-1>", x=cx, y=cy)
            root.update()
            self.assertEqual(f.ttss[0].replays, [("Thank you", "en", 2)])
            # Clicking the card text (not ▶) doesn't replay.
            tx = x0 - 200
            cv.event_generate("<Motion>", x=tx, y=cy)
            cv.event_generate("<Button-1>", x=tx, y=cy)
            root.update()
            self.assertEqual(len(f.ttss[0].replays), 1)
            # The playing message gets highlighted, and the highlight clears.
            app.messages.set_playing((2,))
            self.assertEqual(app.messages.playing, {2})
            app.messages.set_playing(())
            self.assertEqual(app.messages.playing, set())
            app.on_stop()
            run_ui_until(app, lambda: app.state is State.IDLE)


if __name__ == "__main__":
    unittest.main()
