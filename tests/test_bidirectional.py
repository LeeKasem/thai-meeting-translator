"""Two-way translation and voice-per-language, offline (no key or network)."""
import os
import tempfile
import threading
import time
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import azure.cognitiveservices.speech as speechsdk

from translator import config
from translator.audio import AudioPump
from translator.recognition import RecognitionController, Segment
from translator.transcript import Transcript
from translator.tts import TtsQueue, build_ssml

from test_lifecycle import (FakePlayer, FakeSynth, Fakes, headless_app, make_tts,
                            run_ui_until, wait_until)
from translator.session import State

PID = speechsdk.PropertyId


class ConfigTest(unittest.TestCase):
    def test_modes(self):
        self.assertEqual(config.direction(config.MODE_EN_TH), ("en", "th"))
        self.assertEqual(config.direction(config.MODE_TH_EN), ("th", "en"))
        self.assertIsNone(config.direction(config.MODE_AUTO))
        for bad in ("en>en", "fr>th", "th", ""):
            with self.assertRaises(ValueError):
                config.direction(bad)
        self.assertEqual(config.other("en"), "th")
        self.assertEqual(config.other("th"), "en")
        self.assertEqual(config.lang_for_locale("th-TH"), "th")
        self.assertEqual(config.lang_for_locale("EN-us"), "en")
        self.assertIsNone(config.lang_for_locale("fr-FR"))
        self.assertIsNone(config.lang_for_locale(""))

    def _voices(self, **env):
        clean = {k: v for k, v in os.environ.items() if not k.startswith("TTS_VOICE_")}
        clean.update(env)
        with mock.patch.dict(os.environ, clean, clear=True), \
             mock.patch.object(config, "load_dotenv"):
            return config.load_voices()

    def test_default_voices(self):
        self.assertEqual(self._voices(), {"en": "en-US-JennyNeural", "th": "th-TH-NiwatNeural"})

    def test_voice_override(self):
        v = self._voices(TTS_VOICE_EN="en-GB-SoniaNeural", TTS_VOICE_TH="th-TH-PremwadeeNeural")
        self.assertEqual(v, {"en": "en-GB-SoniaNeural", "th": "th-TH-PremwadeeNeural"})
        v = self._voices(TTS_VOICE_TH="en-US-AvaMultilingualNeural")  # multilingual is allowed
        self.assertEqual(v["th"], "en-US-AvaMultilingualNeural")

    def test_bad_voices_rejected(self):
        with self.assertRaises(config.ConfigError):
            self._voices(TTS_VOICE_TH="en-US-JennyNeural")  # English voice for Thai
        with self.assertRaises(config.ConfigError):
            self._voices(TTS_VOICE_EN="Jenny")
        with self.assertRaises(config.ConfigError):
            self._voices(TTS_VOICE_EN="en-US-Jenny'/><x")


class SegmentAndTranscriptTest(unittest.TestCase):
    def test_segment_languages(self):
        a = Segment(0, 1, "Hello", "สวัสดี", "en", "th")
        b = Segment(0, 1, "ขอบคุณ", "Thank you", "th", "en")
        self.assertEqual((a.english, a.thai), ("Hello", "สวัสดี"))
        self.assertEqual((b.english, b.thai), ("Thank you", "ขอบคุณ"))
        self.assertEqual(Segment(0, 1, "x", "y").source_lang, "en")  # old positional form

    def test_transcript_both_directions(self):
        with tempfile.TemporaryDirectory() as d:
            t0 = 1_700_000_000.0
            tr = Transcript(Path(d), t0)
            tr.add(Segment(t0 + 1, t0 + 2, "Hello.", "สวัสดี", "en", "th"))
            tr.add(Segment(t0 + 3, t0 + 4, "ราคาเท่าไร", "How much?", "th", "en"))
            txt = tr.txt_path.read_bytes().decode("utf-8-sig")
            self.assertIn("EN -> TH\r\nEN: Hello.\r\nTH: สวัสดี", txt)
            self.assertIn("TH -> EN\r\nTH: ราคาเท่าไร\r\nEN: How much?", txt)
            srt = tr.srt_path.read_bytes().decode("utf-8-sig")
            # translation first (what the listener reads), then the original
            self.assertIn("สวัสดี\r\nHello.", srt)
            self.assertIn("How much?\r\nราคาเท่าไร", srt)


class SsmlTest(unittest.TestCase):
    def test_ssml_selects_voice_and_escapes(self):
        ssml = build_ssml("A & B <tag> \"q\" 'x'", "en", "en-US-JennyNeural")
        root = ET.fromstring(ssml)
        self.assertEqual(root.get("{http://www.w3.org/XML/1998/namespace}lang"), "en-US")
        voice = root[0]
        self.assertEqual(voice.get("name"), "en-US-JennyNeural")
        self.assertEqual(voice.text, "A & B <tag> \"q\" 'x'")
        th = ET.fromstring(build_ssml("สวัสดีครับ", "th", "th-TH-NiwatNeural"))
        self.assertEqual(th[0].get("name"), "th-TH-NiwatNeural")
        self.assertEqual(th.get("{http://www.w3.org/XML/1998/namespace}lang"), "th-TH")


class TtsVoicePerUtteranceTest(unittest.TestCase):
    def test_each_utterance_uses_its_target_voice(self):
        events, synth, player = [], FakeSynth(), FakePlayer()
        q = make_tts(synth, events)
        q.start(player)
        q.enqueue("สวัสดี", "th")
        q.enqueue("Hello", "en")
        self.assertTrue(wait_until(lambda: player.plays == 2))
        q.stop()
        voices = [ET.fromstring(x)[0].get("name") for x in synth.ssml]
        self.assertEqual(voices, ["th-TH-NiwatNeural", "en-US-JennyNeural"])
        self.assertIn(("chars", len("สวัสดี")), events)

    def test_custom_voice_map_and_unknown_language(self):
        synth, player = FakeSynth(), FakePlayer()
        q = TtsQueue(SimpleNamespace(key="k", region="r"), on_queue=lambda *a: None,
                     on_spoken_chars=lambda n: None, on_error=lambda m: None,
                     voices={"en": "en-GB-SoniaNeural", "th": "th-TH-PremwadeeNeural"})
        q._make_synth = lambda: synth
        q.start(player)
        with self.assertRaises(ValueError):
            q.enqueue("Bonjour", "fr")
        q.enqueue("Hi", "en")
        self.assertTrue(wait_until(lambda: player.plays == 1))
        q.stop()
        self.assertEqual(ET.fromstring(synth.ssml[0])[0].get("name"), "en-GB-SoniaNeural")


def result(text, translations, detected=None, reason=speechsdk.ResultReason.TranslatedSpeech):
    props = {}
    if detected is not None:
        props[PID.SpeechServiceConnection_AutoDetectSourceLanguageResult] = detected
    return SimpleNamespace(result=SimpleNamespace(
        text=text, translations=translations, reason=reason, offset=10_000_000,
        duration=5_000_000, properties=props))


class RecognitionDirectionTest(unittest.TestCase):
    def make(self, mode):
        ev = {"final": [], "interim": [], "fatal": [], "status": []}
        c = RecognitionController(
            config.Credentials("k", "southeastasia"), AudioPump(lambda: False),
            on_interim=lambda *a: ev["interim"].append(a),
            on_final=ev["final"].append,
            on_status=lambda st, m: ev["status"].append((st, m)),
            on_fatal=ev["fatal"].append, mode=mode)
        c._gen = 1
        return c, ev

    def test_recognizer_config_per_mode(self):
        """Builds real SDK objects (no connection is made until start)."""
        c, _ = self.make(config.MODE_EN_TH)
        fmt = speechsdk.audio.AudioStreamFormat(samples_per_second=16000, bits_per_sample=16,
                                                channels=1)
        for mode, reco, targets in ((config.MODE_EN_TH, "en-US", ["th"]),
                                    (config.MODE_TH_EN, "th-TH", ["en"])):
            push = speechsdk.audio.PushAudioInputStream(stream_format=fmt)
            rec = c._make_recognizer(mode, push)
            self.assertEqual(rec.properties.get_property(PID.SpeechServiceConnection_RecoLanguage),
                             reco)
            self.assertEqual(rec.target_languages, targets)
            push.close()
        push = speechsdk.audio.PushAudioInputStream(stream_format=fmt)
        rec = c._make_recognizer(config.MODE_AUTO, push)
        self.assertEqual(sorted(rec.target_languages), ["en", "th"])
        self.assertEqual(rec.properties.get_property(PID.SpeechServiceConnection_LanguageIdMode),
                         "Continuous")
        self.assertEqual(
            rec.properties.get_property(PID.SpeechServiceConnection_AutoDetectSourceLanguages),
            "en-US,th-TH")
        self.assertIn("southeastasia.stt.speech.microsoft.com/speech/universal/v2",
                      rec.properties.get_property(PID.SpeechServiceConnection_Endpoint))
        push.close()

    def test_manual_th_to_en(self):
        c, ev = self.make(config.MODE_TH_EN)
        c._on_recognizing(1, config.MODE_TH_EN, result("ราคา", {"en": "Price"}))
        c._on_recognized(1, config.MODE_TH_EN, result("ราคาเท่าไร", {"en": "How much?"}))
        self.assertEqual(ev["interim"], [("ราคา", "Price", "th", "en")])
        seg = ev["final"][0]
        self.assertEqual((seg.source_lang, seg.target_lang), ("th", "en"))
        self.assertEqual((seg.source_text, seg.translated_text), ("ราคาเท่าไร", "How much?"))

    def test_auto_picks_direction_per_utterance(self):
        c, ev = self.make(config.MODE_AUTO)
        both = lambda en, th: {"en": en, "th": th}
        c._on_recognized(1, config.MODE_AUTO, result("ขอบคุณครับ", both("Thank you.", "ขอบคุณครับ"),
                                                      detected="th-TH"))
        c._on_recognized(1, config.MODE_AUTO, result("See you.", both("See you.", "แล้วพบกัน"),
                                                      detected="en-US"))
        c._on_recognized(1, config.MODE_AUTO, result("Bonjour", both("Hello", "สวัสดี"),
                                                      detected="fr-FR"))
        c._on_recognized(1, config.MODE_AUTO, result("???", both("", ""), detected="Unknown"))
        got = [(s.source_lang, s.target_lang, s.translated_text) for s in ev["final"]]
        self.assertEqual(got, [("th", "en", "Thank you."), ("en", "th", "แล้วพบกัน")])

    def test_auto_interim_without_detection_shows_source_only(self):
        c, ev = self.make(config.MODE_AUTO)
        c._on_recognizing(1, config.MODE_AUTO, result("hel", {"en": "hel", "th": "x"}))
        self.assertEqual(ev["interim"], [("hel", "", "", "")])

    def test_stale_generation_and_empty_translation_ignored(self):
        c, ev = self.make(config.MODE_EN_TH)
        c._on_recognized(0, config.MODE_EN_TH, result("old", {"th": "เก่า"}))
        c._on_recognized(1, config.MODE_EN_TH, result("Hello", {"th": "  "}))
        self.assertEqual(ev["final"], [])


class ModeSwitchTest(unittest.TestCase):
    def make(self):
        opened = []
        torn = []
        c = RecognitionController(
            config.Credentials("k", "r"), AudioPump(lambda: False),
            on_interim=lambda *a: None, on_final=lambda s: None,
            on_status=lambda *a: None, on_fatal=lambda m: None)

        def fake_open():
            with c._lock:
                if c._stop.is_set():
                    return
                c._gen += 1
                c._rec, c._push, c._conn = f"rec{c._gen}", None, None
                opened.append((c._gen, c._mode))
        c._open = fake_open
        c._teardown = lambda rec, push, conn: torn.append(rec)
        return c, opened, torn

    def wait_switches(self):
        for t in [t for t in threading.enumerate() if t.name == "switch"]:
            t.join(2)

    def test_switch_before_start_just_changes_mode(self):
        c, opened, torn = self.make()
        c.set_mode(config.MODE_TH_EN)
        self.wait_switches()
        self.assertEqual((opened, torn), ([], []))
        c.start()
        self.assertEqual(opened, [(1, config.MODE_TH_EN)])

    def test_switch_while_live_reopens_with_new_mode(self):
        c, opened, torn = self.make()
        c.start()
        old_gen = c._gen
        c.set_mode(config.MODE_TH_EN)
        self.wait_switches()
        self.assertEqual(torn, [f"rec{old_gen}"])
        self.assertEqual(opened[-1][1], config.MODE_TH_EN)
        self.assertGreater(c._gen, old_gen + 1)  # old callbacks are stale
        c.set_mode(config.MODE_TH_EN)  # same mode: no-op
        self.wait_switches()
        self.assertEqual(len(opened), 2)

    def test_rapid_switches_end_on_last_mode_with_one_recognizer(self):
        c, opened, torn = self.make()
        c.start()
        for m in [config.MODE_TH_EN, config.MODE_AUTO, config.MODE_EN_TH, config.MODE_TH_EN] * 5:
            c.set_mode(m)
        self.wait_switches()
        self.assertEqual(opened[-1][1], config.MODE_TH_EN)
        self.assertEqual(len(opened) - len(torn), 1)  # exactly one live recognizer

    def test_switch_after_stop_does_nothing(self):
        c, opened, torn = self.make()
        c.start()
        c.stop()
        n = len(opened)
        c.set_mode(config.MODE_AUTO)
        self.wait_switches()
        self.assertEqual(len(opened), n)

    def test_invalid_mode_rejected(self):
        c, _, _ = self.make()
        with self.assertRaises(ValueError):
            c.set_mode("fr>th")


class AppDirectionTest(unittest.TestCase):
    def test_th_to_en_final_is_spoken_in_english(self):
        with Fakes() as f:
            app = headless_app()
            app.on_start()
            self.assertTrue(run_ui_until(app, lambda: app.state is State.RUNNING))
            app._show_final(Segment(0, 1, "ขอบคุณ", "Thank you", "th", "en"))
            app._show_final(Segment(0, 1, "Hello", "สวัสดี", "en", "th"))
            self.assertEqual(f.ttss[0].enqueued, [("Thank you", "en"), ("สวัสดี", "th")])
            app.main_label.configure.assert_called_with(text="สวัสดี", fg="#000000", font="big-th")
            app.on_stop()
            run_ui_until(app, lambda: app.state is State.IDLE)

    def test_direction_change_while_running_reaches_controller(self):
        with Fakes() as f:
            app = headless_app()
            mode = {"v": config.MODE_EN_TH}
            app.mode_var = SimpleNamespace(get=lambda: mode["v"])
            app.on_start()
            self.assertTrue(run_ui_until(app, lambda: app.state is State.RUNNING))
            mode["v"] = config.MODE_TH_EN
            app._on_mode_change()
            self.assertEqual(f.controllers[0].modes, [config.MODE_TH_EN])
            app.on_stop()
            run_ui_until(app, lambda: app.state is State.IDLE)

    def test_direction_chosen_during_start_is_applied(self):
        with Fakes() as f:
            f.preflight_gate = threading.Event()
            app = headless_app()
            mode = {"v": config.MODE_EN_TH}
            app.mode_var = SimpleNamespace(get=lambda: mode["v"])
            app.on_start()
            self.assertTrue(f.preflight_entered.wait(2))
            mode["v"] = config.MODE_AUTO
            app._on_mode_change()  # no controller yet
            f.preflight_gate.set()
            self.assertTrue(run_ui_until(app, lambda: app.state is State.RUNNING))
            self.assertEqual(f.controllers[0].mode, config.MODE_AUTO)
            app.on_stop()
            run_ui_until(app, lambda: app.state is State.IDLE)


if __name__ == "__main__":
    unittest.main()
