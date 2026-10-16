"""Onsite mode: turn-taking, feedback-loop protection, lossless turn switches.
Offline: fake SDK objects, no mic, key or network."""
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import azure.cognitiveservices.speech as speechsdk

from translator import config
from translator.audio import AudioPump
from translator.onsite import EchoFilter, Phase, TurnTaker, mode_for, phase_of
from translator.recognition import RecognitionController, Segment
from translator.session import State

from test_lifecycle import (FakePlayer, FakeSynth, Fakes, headless_app, make_tts,
                            run_ui_until, wait_until)


# -------------------------------------------------------------------- pump
class Sink:
    def __init__(self):
        self.data = []

    def write(self, b):
        self.data.append(b)


class PumpGateTest(unittest.TestCase):
    def test_gate_is_decided_at_capture_time(self):
        """Audio captured while the voice played stays silent even if the pump
        thread only gets to it after the gate has reopened."""
        gate = {"closed": True}
        p = AudioPump(lambda: gate["closed"])
        sink = Sink()
        p.set_sink(sink)
        p.feed(b"\x05\x00" * 160)  # captured during playback
        gate["closed"] = False      # gate reopens before the pump runs
        p.feed(b"\x06\x00" * 160)
        p.start()
        p.stop()
        self.assertEqual(sink.data, [bytes(320), b"\x06\x00" * 160])

    def test_gate_fails_closed(self):
        def broken():
            raise RuntimeError("boom")
        p = AudioPump(broken)
        sink = Sink()
        p.set_sink(sink)
        p.start()
        p.feed(b"\x07\x00" * 10)
        p.stop()
        self.assertEqual(sink.data, [bytes(20)])

    def test_audio_without_sink_is_buffered_and_replayed_in_order(self):
        p = AudioPump(lambda: False)
        p.start()
        for i in range(3):
            p.feed(bytes([i + 1, 0]) * 100)
        time.sleep(0.05)
        sink = Sink()
        p.set_sink(sink)  # new recognizer after a turn switch
        p.feed(b"\x09\x00" * 100)
        p.stop()
        self.assertEqual([c[0] for c in sink.data], [1, 2, 3, 9])
        self.assertAlmostEqual(p.seconds_sent, 400 * 2 / 32000)

    def test_buffer_is_bounded_keeps_newest(self):
        p = AudioPump(lambda: False, buffer_s=0.01)  # 320 bytes
        p.start()
        for i in range(5):
            p.feed(bytes([i + 1, 0]) * 80)  # 160 bytes each
        time.sleep(0.05)
        sink = Sink()
        p.set_sink(sink)
        p.stop()
        self.assertEqual([c[0] for c in sink.data], [4, 5])
        self.assertEqual(p.bytes_dropped, 480)


# ----------------------------------------------------------------------- tts
class TtsStateTest(unittest.TestCase):
    def test_states_tail_skip_and_on_playing(self):
        gate = threading.Event()
        synth, player = FakeSynth(gate), FakePlayer(block_until_cancel=True)
        events = []
        q = make_tts(synth, events)
        q.set_tail(0.3)
        busy_at_play = []
        q.on_playing = lambda u: busy_at_play.append((u.text, u.lang, q.is_busy()))
        q.start(player)
        self.assertEqual(q.state(), "idle")
        q.enqueue("one", "th")
        q.enqueue("two", "en")
        self.assertTrue(wait_until(lambda: synth.calls == 1))
        self.assertEqual(q.state(), "pending")  # synthesizing
        self.assertFalse(q.is_busy())           # nothing audible yet: mic stays open
        gate.set()
        self.assertTrue(wait_until(lambda: q.state() == "speaking"))
        self.assertTrue(q.is_busy())
        self.assertEqual(busy_at_play, [("one", "th", True)])  # gate closed before sound
        q.skip()  # cut "one"; "two" still plays
        self.assertTrue(wait_until(lambda: len(busy_at_play) == 2))
        self.assertEqual(busy_at_play[1][:2], ("two", "en"))
        q.skip()
        self.assertTrue(wait_until(lambda: q.state() == "tail"))
        self.assertTrue(q.is_busy())  # echo tail keeps the mic gated
        self.assertTrue(wait_until(lambda: q.state() == "idle", timeout=1))
        self.assertFalse(q.is_busy())
        q.stop()


# --------------------------------------------------------- recognizer flush
def _translated(text, translations):
    return SimpleNamespace(result=SimpleNamespace(
        text=text, translations=translations, reason=speechsdk.ResultReason.TranslatedSpeech,
        offset=0, duration=10_000_000, properties={}))


class FakePush:
    def __init__(self, on_close=None):
        self.on_close = on_close
        self.data = []
        self.closed = False

    def write(self, b):
        if self.closed:
            raise RuntimeError("closed")
        self.data.append(b)

    def close(self):
        self.closed = True
        if self.on_close:
            self.on_close()


class SwitchFlushTest(unittest.TestCase):
    """A turn switch must not lose the old speaker's last sentence (Azure
    finalizes it when the input stream ends) nor the new speaker's first words
    (buffered by the pump)."""

    def make(self, flush_behaviour="final"):
        log, ev = [], {"final": [], "fatal": [], "status": []}
        pump = AudioPump(lambda: False)
        pump.start()
        c = RecognitionController(
            config.Credentials("k", "r"), pump,
            on_interim=lambda *a: None,
            on_final=lambda seg: (ev["final"].append(seg), log.append(("final", seg.source_text))),
            on_status=lambda st, m: ev["status"].append(st),
            on_fatal=ev["fatal"].append)
        pushes = []

        def fake_open():
            with c._lock:
                if c._stop.is_set():
                    return
                c._gen += 1
                gen, mode = c._gen, c._mode

                def finish():
                    def run():
                        time.sleep(0.05)  # Azure finalizing
                        if flush_behaviour == "final":
                            c._on_recognized(gen, mode, _translated(
                                "the last words", {"th": "คำสุดท้าย"}))
                            c._on_canceled(gen, SimpleNamespace(
                                reason=speechsdk.CancellationReason.EndOfStream))
                            c._on_session_stopped(gen)
                        elif flush_behaviour == "error":
                            c._on_canceled(gen, SimpleNamespace(
                                reason=speechsdk.CancellationReason.Error,
                                error_code=speechsdk.CancellationErrorCode.ServiceTimeout,
                                error_details="x"))
                        # "silent": never reports end of stream
                    threading.Thread(target=run, daemon=True).start()
                push = FakePush(on_close=finish)
                pushes.append(push)
                c._rec, c._push, c._conn = f"rec{gen}", push, None
                log.append(("open", mode))
            c.pump.set_sink(push)
        c._open = fake_open
        c._teardown = lambda rec, push, conn: log.append(("teardown", rec))
        self.addCleanup(pump.stop)
        return c, pump, pushes, log, ev

    def wait_switch(self, c):
        self.assertTrue(wait_until(lambda: not c.is_switching, timeout=4))

    def test_last_sentence_kept_and_no_reconnect(self):
        c, pump, pushes, log, ev = self.make("final")
        c.start()
        c._on_connected(c._gen)
        c.set_mode(config.MODE_TH_EN)
        self.assertTrue(c.is_switching)
        self.wait_switch(c)
        kinds = [k for k, _ in log]
        self.assertEqual(kinds, ["open", "final", "teardown", "open"])
        seg = ev["final"][0]
        self.assertEqual((seg.source_lang, seg.target_lang), ("en", "th"))  # old direction
        self.assertEqual(log[-1], ("open", config.MODE_TH_EN))
        time.sleep(0.1)
        self.assertNotIn("reconnecting", ev["status"])
        self.assertEqual(ev["fatal"], [])
        self.assertFalse([t for t in threading.enumerate() if t.name == "reconnect"])

    def test_new_speaker_audio_buffered_during_switch(self):
        c, pump, pushes, log, ev = self.make("final")
        c.start()
        pump.feed(b"\x01\x00" * 50)  # old speaker
        time.sleep(0.05)
        c.set_mode(config.MODE_TH_EN)
        self.assertTrue(wait_until(lambda: pushes[0].closed))
        pump.feed(b"\x02\x00" * 50)  # new speaker starts at once
        self.wait_switch(c)
        pump.feed(b"\x03\x00" * 50)
        time.sleep(0.05)
        self.assertEqual([b[0] for b in pushes[0].data], [1])
        self.assertEqual([b[0] for b in pushes[1].data], [2, 3])

    def test_no_end_of_stream_times_out(self):
        c, *_, log, ev = self.make("silent")
        with mock.patch.object(config, "SWITCH_FLUSH_TIMEOUT_S", 0.2):
            c.start()
            t0 = time.monotonic()
            c.set_mode(config.MODE_TH_EN)
            self.wait_switch(c)
            self.assertLess(time.monotonic() - t0, 1.0)
        self.assertEqual(log[-1], ("open", config.MODE_TH_EN))

    def test_error_while_flushing_is_not_a_drop(self):
        c, *_, log, ev = self.make("error")
        c.start()
        c._on_connected(c._gen)
        c.set_mode(config.MODE_TH_EN)
        self.wait_switch(c)
        time.sleep(0.1)
        self.assertNotIn("reconnecting", ev["status"])
        self.assertEqual(ev["fatal"], [])

    def test_stop_during_flush_is_prompt_and_opens_nothing(self):
        c, *_, log, ev = self.make("silent")
        with mock.patch.object(config, "SWITCH_FLUSH_TIMEOUT_S", 5):
            c.start()
            c.set_mode(config.MODE_TH_EN)
            time.sleep(0.1)
            t0 = time.monotonic()
            c.stop()  # also waits for the switch thread to finish
            self.assertLess(time.monotonic() - t0, 1.0)
        self.assertEqual([k for k, _ in log].count("open"), 1)
        self.assertFalse(c.is_switching)


# ------------------------------------------------------------------- turns
class TurnTakerTest(unittest.TestCase):
    def test_mode_for_side(self):
        self.assertEqual(mode_for("en"), config.MODE_EN_TH)
        self.assertEqual(mode_for("th"), config.MODE_TH_EN)
        with self.assertRaises(ValueError):
            mode_for("fr")

    def test_give_floor(self):
        t = TurnTaker("en")
        self.assertIsNone(t.give_floor("en"))
        self.assertEqual(t.give_floor("th"), config.MODE_TH_EN)
        self.assertEqual(t.side, "th")

    def test_auto_alternate_waits_for_voice_to_finish(self):
        t = TurnTaker("en", auto_alternate=True)
        t.on_final("en")
        for state, switching in (("pending", False), ("speaking", False), ("tail", False),
                                 ("idle", True)):
            self.assertIsNone(t.poll(state, switching), state)
        self.assertEqual(t.poll("idle", False), config.MODE_TH_EN)
        self.assertIsNone(t.poll("idle", False))  # once only

    def test_manual_choice_cancels_pending_alternate(self):
        t = TurnTaker("en", auto_alternate=True)
        t.on_final("en")
        t.give_floor("en")  # English speaker wants to continue
        self.assertIsNone(t.poll("idle", False))

    def test_no_alternate_when_off_or_other_side(self):
        t = TurnTaker("en", auto_alternate=False)
        t.on_final("en")
        self.assertIsNone(t.poll("idle", False))
        t = TurnTaker("en", auto_alternate=True)
        t.on_final("th")  # flushed sentence from the previous speaker
        self.assertIsNone(t.poll("idle", False))

    def test_phase_of(self):
        self.assertIs(phase_of("speaking", True), Phase.SPEAKING)  # mic gating wins
        self.assertIs(phase_of("tail", False), Phase.TAIL)
        self.assertIs(phase_of("idle", True), Phase.SWITCHING)
        self.assertIs(phase_of("pending", False), Phase.TRANSLATING)
        self.assertIs(phase_of("idle", False), Phase.LISTENING)


class EchoFilterTest(unittest.TestCase):
    def make(self):
        now = {"t": 100.0}
        return EchoFilter(similarity=0.8, window_s=15, clock=lambda: now["t"]), now

    def test_detects_own_voice(self):
        f, _ = self.make()
        f.spoken("It is about two hundred baht.", "en")
        f.spoken("ประมาณสองร้อยบาทครับ", "th")
        self.assertTrue(f.is_echo("it's about two hundred baht", "en"))
        self.assertTrue(f.is_echo("ประมาณ สองร้อยบาท ครับ", "th"))
        self.assertTrue(f.is_echo("about two hundred baht", "en"))  # fragment

    def test_does_not_flag_real_speech(self):
        f, now = self.make()
        f.spoken("It is about two hundred baht.", "en")
        self.assertFalse(f.is_echo("Yes", "en"))  # short common word
        self.assertFalse(f.is_echo("Can I pay by card?", "en"))
        self.assertFalse(f.is_echo("It is about two hundred baht.", "th"))  # other language
        now["t"] += 16
        self.assertFalse(f.is_echo("It is about two hundred baht.", "en"))  # expired
        self.assertFalse(f.is_echo("", "en"))


# --------------------------------------------------------------- app level
class OnsiteAppTest(unittest.TestCase):
    def start(self, f, setting="onsite"):
        app = headless_app(setting)
        app.setting_var.set(setting)
        app._apply_setting()
        app.on_start()
        self.assertTrue(run_ui_until(app, lambda: app.state is State.RUNNING))
        self.addCleanup(lambda: (app.on_stop(),
                                 run_ui_until(app, lambda: app.state is State.IDLE)))
        return app

    def test_onsite_forces_mic_gate_and_floor_direction(self):
        with Fakes() as f:
            app = headless_app("onsite")
            app.source_var.set("loopback")
            app.turn.give_floor("th")
            app._apply_setting()
            self.assertEqual(app.source_var.get(), "mic")
            self.assertTrue(app.pause_var.get())
            app.on_start()
            self.assertTrue(run_ui_until(app, lambda: app.state is State.RUNNING))
            self.assertEqual(f.controllers[0].mode, config.MODE_TH_EN)
            tts = f.ttss[0]
            self.assertEqual(tts.kwargs["tail_s"], config.ONSITE_TAIL_S)
            app._pause_while_speaking = False  # gate must not depend on the checkbox
            gate = f.pumps[0].is_paused
            tts.busy = True
            self.assertTrue(gate())
            tts.busy = False
            self.assertFalse(gate())
            app.on_stop()
            run_ui_until(app, lambda: app.state is State.IDLE)

    def test_meeting_mode_gate_follows_checkbox(self):
        with Fakes() as f:
            app = self.start(f, "meeting")
            f.ttss[0].busy = True
            self.assertFalse(f.pumps[0].is_paused())
            self.assertEqual(f.ttss[0].kwargs["tail_s"], config.PAUSE_TAIL_S)

    def test_floor_button_switches_direction(self):
        with Fakes() as f:
            app = self.start(f)
            app._give_floor("th")
            self.assertEqual(f.controllers[0].modes, [config.MODE_TH_EN])
            app._give_floor("th")  # already has it
            app._give_floor("en")
            self.assertEqual(f.controllers[0].modes, [config.MODE_TH_EN, config.MODE_EN_TH])

    def test_floor_buttons_ignored_in_meeting_mode(self):
        with Fakes() as f:
            app = self.start(f, "meeting")
            app._give_floor("th")
            self.assertEqual(f.controllers[0].modes, [])

    def test_echo_of_own_voice_is_dropped(self):
        with Fakes() as f:
            app = self.start(f)
            app._on_voice_playing(SimpleNamespace(text="ประมาณสองร้อยบาท", lang="th"))
            app._show_final(Segment(0, 1, "ประมาณสองร้อยบาท", "About 200 baht", "th", "en"))
            app._show_final(Segment(0, 1, "Thank you very much", "ขอบคุณมาก", "en", "th"))
            self.assertEqual(f.ttss[0].enqueued, [("ขอบคุณมาก", "th")])

    def test_auto_alternate_after_voice_finishes(self):
        with Fakes() as f:
            app = self.start(f)
            tts, ctl = f.ttss[0], f.controllers[0]
            app._show_final(Segment(0, 1, "How much is it?", "ราคาเท่าไร", "en", "th"))
            tts.tts_state = "speaking"
            app._update_onsite()
            self.assertEqual(ctl.modes, [])
            app.phase_label.configure.assert_called_with(
                text=ui_banner(Phase.SPEAKING, "en"), bg="#b00020")
            tts.tts_state = "idle"
            app._update_onsite()
            self.assertEqual(ctl.modes, [config.MODE_TH_EN])
            self.assertEqual(app.turn.side, "th")

    def test_skip_voice(self):
        with Fakes() as f:
            app = self.start(f)
            app._skip_voice()
            self.assertEqual(f.ttss[0].skips, 1)


def ui_banner(phase, side):
    from translator.ui import PHASE_BANNER
    text = PHASE_BANNER[phase][1]
    return text[side] if isinstance(text, dict) else text


if __name__ == "__main__":
    unittest.main()
