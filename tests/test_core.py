"""Offline tests: python -m unittest discover -s tests"""
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import azure.cognitiveservices.speech as speechsdk

from translator import config
from translator.audio import AudioPump, StreamResampler, convert_for_output
from translator.recognition import RecognitionController, Segment
from translator.transcript import Transcript
from translator.tts import TtsQueue


class ResamplerTest(unittest.TestCase):
    def test_48k_stereo_to_16k_mono_keeps_tone_and_length(self):
        rate, secs, freq = 48000, 2.0, 440.0
        t = np.arange(int(rate * secs)) / rate
        mono = (np.sin(2 * np.pi * freq * t) * 10000).astype(np.int16)
        stereo = np.repeat(mono, 2).tobytes()
        r = StreamResampler(rate, 2)
        out = b"".join(r.process(stereo[i:i + 9600]) for i in range(0, len(stereo), 9600))
        y = np.frombuffer(out, np.int16).astype(float)
        self.assertAlmostEqual(len(y), 32000, delta=2)
        spec = np.abs(np.fft.rfft(y[1000:]))
        peak_hz = np.argmax(spec) * 16000 / len(y[1000:])
        self.assertAlmostEqual(peak_hz, freq, delta=3)

    def test_aliasing_is_filtered(self):
        rate = 48000
        t = np.arange(rate) / rate
        tone = (np.sin(2 * np.pi * 12000 * t) * 10000).astype(np.int16).tobytes()  # above 8 kHz
        y = np.frombuffer(StreamResampler(rate, 1).process(tone), np.int16)
        self.assertLess(np.abs(y[200:]).max(), 600)

    def test_44100_rate(self):
        x = np.zeros(44100, np.int16).tobytes()
        r = StreamResampler(44100, 1)
        out = b"".join(r.process(x[i:i + 882]) for i in range(0, len(x), 882))
        self.assertAlmostEqual(len(out) // 2, 16000, delta=2)

    def test_output_conversion(self):
        pcm = np.ones(24000, np.int16).tobytes()
        out = convert_for_output(pcm, 24000, 48000, 2)
        self.assertEqual(len(out) // 2, 96000)


class PumpTest(unittest.TestCase):
    def test_paused_sends_silence_and_counts(self):
        paused = {"v": True}
        sink = SimpleNamespace(data=[], write=lambda b: sink.data.append(b))
        p = AudioPump(lambda: paused["v"])
        p.set_sink(sink)
        p.start()
        p.feed(b"\x01\x02" * 1600)
        time.sleep(0.1)
        paused["v"] = False
        p.feed(b"\x01\x02" * 1600)
        p.stop()
        self.assertEqual(sink.data[0], bytes(3200))
        self.assertEqual(sink.data[1], b"\x01\x02" * 1600)
        self.assertAlmostEqual(p.seconds_sent, 0.2)


class TranscriptTest(unittest.TestCase):
    def test_autosave_bom_and_srt(self):
        with tempfile.TemporaryDirectory() as d:
            t0 = 1_700_000_000.0
            tr = Transcript(Path(d), t0)
            tr.add(Segment(t0 + 1.5, t0 + 3.25, "Hello everyone.", "สวัสดีทุกคน"))
            tr.add(Segment(t0 + 3661.0, t0 + 3662.0, "Next item.", "หัวข้อถัดไป"))
            raw = tr.txt_path.read_bytes()
            self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
            self.assertEqual(raw.count(b"\xef\xbb\xbf"), 1)
            self.assertIn("สวัสดีทุกคน", raw.decode("utf-8-sig"))
            self.assertIn(b"\r\n", raw)
            srt = tr.srt_path.read_bytes()
            self.assertTrue(srt.startswith(b"\xef\xbb\xbf1\r\n00:00:01,500 --> 00:00:03,250\r\n"))
            self.assertIn("01:01:01,000 --> 01:01:02,000", srt.decode("utf-8-sig"))
            out = Path(d) / "x.srt"
            tr.export(out)
            self.assertEqual(out.read_bytes(), srt)


class TtsBacklogTest(unittest.TestCase):
    def test_drops_oldest_beyond_backlog(self):
        seen = []
        q = TtsQueue(config.Credentials("k", "r"), on_queue=lambda n, s: seen.append((n, s)),
                     on_spoken_chars=lambda n: None, on_error=lambda m: None, max_backlog=2)
        q._running = True  # no worker thread: just exercise the queue policy
        for i in range(5):
            q.enqueue(f"s{i}", "th")
        self.assertEqual([u.text for u in q._q], ["s3", "s4"])
        self.assertEqual(q.skipped, 3)
        self.assertEqual(seen[-1], (2, 3))
        q.set_muted(True)
        q.enqueue("s5", "th")
        self.assertEqual(q.waiting, 0)


def _canceled(code, reason=speechsdk.CancellationReason.Error):
    return SimpleNamespace(reason=reason, error_code=code, error_details="details")


class ReconnectLogicTest(unittest.TestCase):
    def make(self):
        events = {"fatal": [], "status": [], "opens": 0}
        pump = AudioPump(lambda: False)
        c = RecognitionController(
            config.Credentials("k", "r"), pump,
            on_interim=lambda *a: None, on_final=lambda s: None,
            on_status=lambda st, m: events["status"].append(st),
            on_fatal=lambda m: events["fatal"].append(m))

        def fake_open():
            with c._lock:
                if c._stop.is_set():
                    return
                c._gen += 1
                events["opens"] += 1
        c._open = fake_open
        c._gen = 1
        return c, events

    def test_auth_failure_is_fatal_no_reconnect(self):
        c, ev = self.make()
        c._on_canceled(1, _canceled(speechsdk.CancellationErrorCode.AuthenticationFailure))
        c._on_session_stopped(1)
        time.sleep(0.1)
        self.assertEqual(len(ev["fatal"]), 1)
        self.assertNotIn("reconnecting", ev["status"])
        self.assertEqual(ev["opens"], 0)

    def test_bad_request_is_fatal(self):
        c, ev = self.make()
        c._on_canceled(1, _canceled(speechsdk.CancellationErrorCode.BadRequest))
        self.assertEqual(len(ev["fatal"]), 1)

    def test_failure_before_first_connection_does_not_retry(self):
        c, ev = self.make()
        c._on_canceled(1, _canceled(speechsdk.CancellationErrorCode.ConnectionFailure))
        time.sleep(0.1)
        self.assertEqual(len(ev["fatal"]), 1)
        self.assertEqual(ev["opens"], 0)

    def test_drop_counted_once_and_reconnects(self):
        c, ev = self.make()
        c._on_connected(1)
        with unittest.mock.patch.object(config, "RECONNECT_BACKOFF_S", (0.05,)):
            c._on_canceled(1, _canceled(speechsdk.CancellationErrorCode.ConnectionFailure))
            c._on_session_stopped(1)  # same drop, must be ignored
            time.sleep(0.3)
        self.assertEqual(ev["status"].count("reconnecting"), 1)
        self.assertEqual(c._attempts, 1)
        self.assertEqual(ev["opens"], 1)
        self.assertEqual(ev["fatal"], [])

    def test_stale_generation_ignored(self):
        c, ev = self.make()
        c._on_connected(1)
        c._gen = 2
        c._on_canceled(1, _canceled(speechsdk.CancellationErrorCode.AuthenticationFailure))
        self.assertEqual(ev["fatal"], [])

    def test_retries_can_be_capped(self):
        c, ev = self.make()
        c._on_connected(1)
        with unittest.mock.patch.object(config, "RECONNECT_BACKOFF_S", (0.01,)), \
                unittest.mock.patch.object(config, "MAX_RECONNECT_ATTEMPTS", 5):
            for _ in range(5 + 1):
                g = c._gen
                c._on_canceled(g, _canceled(speechsdk.CancellationErrorCode.ServiceTimeout))
                time.sleep(0.1)
        self.assertEqual(ev["opens"], 5)
        self.assertEqual(len(ev["fatal"]), 1)
        self.assertIn("Gave up", ev["fatal"][0])

    def test_stop_during_backoff_cancels_reconnect(self):
        c, ev = self.make()
        c._on_connected(1)
        with unittest.mock.patch.object(config, "RECONNECT_BACKOFF_S", (5,)):
            c._on_canceled(1, _canceled(speechsdk.CancellationErrorCode.ConnectionFailure))
            time.sleep(0.1)
            t0 = time.monotonic()
            c.stop()
            threads = [t for t in threading.enumerate() if t.name == "reconnect"]
            for t in threads:
                t.join(timeout=1)
            self.assertLess(time.monotonic() - t0, 1.5)
        self.assertEqual(ev["opens"], 0)
        self.assertEqual(ev["fatal"], [])


class PreflightTest(unittest.TestCase):
    """Needs internet. Uses fake credentials only."""

    def test_wrong_key_is_config_error(self):
        with self.assertRaises(config.ConfigError) as cm:
            config.preflight_check(config.Credentials("0" * 32, "southeastasia"))
        self.assertIn("rejected the key", str(cm.exception))

    def test_wrong_region_is_config_error(self):
        with self.assertRaises(config.ConfigError) as cm:
            config.preflight_check(config.Credentials("0" * 32, "nosuchregion"))
        self.assertIn("not found", str(cm.exception))


import unittest.mock  # noqa: E402

if __name__ == "__main__":
    unittest.main()
