"""measure.main() end to end (synthetic source) with the real pump, stream
clock, recognition timing code and TtsQueue. The Azure recognizer and
synthesizer and the audio devices are faked."""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import azure.cognitiveservices.speech as speechsdk

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools import measure  # noqa: E402
from translator import audio, config, recognition  # noqa: E402
from translator import tts as ttsmod  # noqa: E402
from translator.audio import Device, DeviceList  # noqa: E402

FINAL_AFTER_QUIET = 3  # chunks (300 ms) of silence before the fake Azure finalizes
LOCALE = {"en": "en-US", "th": "th-TH"}
MARK = {"en": 0x10, "th": 0x20}  # first sample byte identifies the prompt's language


class FakePlayer:
    output_latency = 0.02
    played = []

    def __init__(self, pa, dev):
        pass

    def play(self, pcm, rate, cancel):
        FakePlayer.played.append(len(pcm))
        return not cancel.wait(0.05)

    def close(self):
        pass


class FakeRecognizer(recognition.RecognitionController):
    """Listens to the pump like Azure would: a final result a fixed time after
    the speech stops, with Azure-style offset/duration in stream time."""

    def start(self):
        self._gen = 1
        self._pos = 0.0
        self._speech = None  # (start_s, lang)
        self._speech_end = 0.0
        self._quiet = 0
        self._clocks[1] = self.pump.set_sink(SimpleNamespace(write=self._hear))
        self.on_status("listening", "Listening")

    def stop(self):
        self.pump.set_sink(None)

    def _hear(self, chunk: bytes):
        pos0 = self._pos
        self._pos += len(chunk) / 32000
        voiced = len(chunk.rstrip(b"\x00"))
        if voiced:
            if self._speech is None:
                lang = "en" if chunk[0] == MARK["en"] else "th"
                self._speech = (pos0, lang)
            self._speech_end = pos0 + (voiced + voiced % 2) / 32000
            self._quiet = 0
            return
        if self._speech is None:
            return
        self._quiet += 1
        if self._quiet < FINAL_AFTER_QUIET:
            return
        start, lang = self._speech
        self._speech = None
        r = SimpleNamespace(
            text="Hello" if lang == "en" else "สวัสดี",
            translations={"th": "สวัสดี", "en": "Hello"},
            reason=speechsdk.ResultReason.TranslatedSpeech,
            offset=int(start * 1e7), duration=int((self._speech_end - start) * 1e7),
            properties={speechsdk.PropertyId.SpeechServiceConnection_AutoDetectSourceLanguageResult:
                        LOCALE[lang]})
        self._on_recognized(1, config.MODE_AUTO, SimpleNamespace(result=r))


class FakeSynth:
    def speak_ssml_async(self, ssml):
        def get():
            time.sleep(0.05)
            return SimpleNamespace(reason=speechsdk.ResultReason.SynthesizingAudioCompleted,
                                   audio_data=b"\x01\x00" * 2400)
        return SimpleNamespace(get=get)

    def stop_speaking_async(self):
        pass


def tone(lang: str, seconds: float) -> bytes:
    return bytes([MARK[lang], 0x01]) * int(16000 * seconds)


class MainSmokeTest(unittest.TestCase):
    def test_synthetic_run_writes_results(self):
        out_dev = Device(1, "Speakers", "output", 48000, 2)
        devices = DeviceList([], [], [out_dev], None, None, 1)
        clips = {"en": tone("en", 0.55), "th": tone("th", 0.75)}
        FakePlayer.played = []
        with tempfile.TemporaryDirectory() as d:
            tsv = Path(d) / "p.tsv"
            tsv.write_text("a\ten\tnormal\tHello\nb\tth\tnormal\tสวัสดี\n", encoding="utf-8")
            patches = [
                mock.patch.object(measure, "RESULTS_DIR", Path(d)),
                mock.patch.object(measure, "SETTLE_S", 0.2),
                mock.patch.object(config, "load_credentials",
                                  return_value=config.Credentials("k", "r")),
                mock.patch.object(config, "preflight_check"),
                mock.patch.object(config, "load_voices", return_value=dict(config.DEFAULT_VOICES)),
                mock.patch.object(measure.PromptAudio, "get", lambda self, p: clips[p.lang]),
                mock.patch.object(measure.PromptAudio, "prepare", lambda self, ps: None),
                mock.patch.object(recognition, "RecognitionController", FakeRecognizer),
                mock.patch.object(ttsmod.TtsQueue, "_make_synth", lambda self: FakeSynth()),
                mock.patch.object(audio, "Player", FakePlayer),
                mock.patch.object(audio, "list_devices", return_value=devices),
                mock.patch("pyaudiowpatch.PyAudio"),
                mock.patch("builtins.print"),
            ]
            for p in patches:
                p.start()
            try:
                rc = measure.main(["--source", "synthetic", "--phrases", str(tsv)])
            finally:
                for p in reversed(patches):
                    p.stop()
            self.assertEqual(rc, 0)
            run = next(Path(d).glob("measure-*-synthetic-auto"))
            md = (run / "summary.md").read_text(encoding="utf-8")
            rows = [json.loads(x) for x in
                    (run / "rows.jsonl").read_text(encoding="utf-8").splitlines()]
            csv_lines = (run / "phrases.csv").read_text(encoding="utf-8-sig").splitlines()
            log_text = (run / "run.log").read_text(encoding="utf-8")

        self.assertIn("**2/2 correct (100%)**", md)
        self.assertEqual([(r["src"], r["outcome"]) for r in rows],
                         [("en", "played"), ("th", "played")])
        for r in rows:
            # The fake finalizes 300 ms after speech ends (+ up to one 100 ms
            # chunk of delivery). The clock must map Azure's offset back to
            # the capture time, so this comes out right.
            self.assertGreaterEqual(r["recognize_s"], 0.25)
            self.assertLessEqual(r["recognize_s"], 0.5)
            self.assertGreater(r["total_s"], r["recognize_s"] + 0.05)  # + synthesis
            self.assertAlmostEqual(r["synth_s"], 0.05, delta=0.05)
        self.assertEqual(len(csv_lines), 3)
        # Speech end from the recognizer vs the true end of the clip: same here.
        self.assertIn("recognizer's speech end minus true end: median 0.00 s", md)
        self.assertIn("latency seg 1 en>th played", log_text)
        self.assertEqual(len(FakePlayer.played), 2)


if __name__ == "__main__":
    unittest.main()
