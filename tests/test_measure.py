"""The measurement harness (tools/measure.py), offline: fake Azure, fake audio."""
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools import measure  # noqa: E402
from tools.measure import (Phrase, Runner, SyntheticSource, load_phrases,  # noqa: E402
                           norm, phrase_rows, similarity, summary_md, write_csv)
from translator import config  # noqa: E402
from translator.latency import LatencyLog  # noqa: E402
from translator.recognition import Segment  # noqa: E402

from test_lifecycle import wait_until  # noqa: E402

THAI = range(0x0E00, 0x0E80)


class PhraseFileTest(unittest.TestCase):
    def test_shipped_phrases_are_valid(self):
        ps = load_phrases(measure.PHRASES)
        self.assertGreaterEqual(len(ps), 24)
        self.assertEqual(len({p.id for p in ps}), len(ps))
        langs = [p.lang for p in ps]
        self.assertEqual(set(langs), {"en", "th"})
        switches = sum(1 for a, b in zip(langs, langs[1:]) if a != b)
        self.assertGreater(switches, len(ps) // 2)  # mostly alternating
        for p in ps:
            has_thai = any(ord(c) in THAI for c in p.text)
            self.assertEqual(has_thai, p.lang == "th", p.id)
        self.assertTrue({"normal", "short", "number", "mixed"} <= {p.category for p in ps})

    def test_bad_file_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "p.tsv"
            for body in ("x\ten\tnormal\n", "x\tfr\tnormal\tBonjour\n",
                         "x\ten\tnormal\tA\nx\ten\tnormal\tB\n"):
                f.write_text(body, encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_phrases(f)


class SimilarityTest(unittest.TestCase):
    def test_norm_keeps_thai_marks_drops_punctuation(self):
        self.assertEqual(norm("Hello, World!"), "helloworld")
        self.assertEqual(norm("ได้ค่ะ"), "ได้ค่ะ")  # tone marks/vowels kept (category M)
        self.assertEqual(similarity("สวัสดีครับ", "สวัสดี ครับ"), 1.0)
        self.assertLess(similarity("ได้ค่ะ", "ไดคะ"), 1.0)  # marks matter
        self.assertEqual(similarity("", ""), 1.0)


def phrases():
    return [Phrase("e1", "en", "normal", "Good morning everyone."),
            Phrase("t1", "th", "normal", "สวัสดีครับ"),
            Phrase("s1", "en", "short", "Yes."),
            Phrase("t2", "th", "number", "สามพันบาท")]


class FakeSource:
    """Each phrase is 'heard' and answered by a fake Azure after a short delay."""

    def __init__(self, runner_ref, answers):
        self.runner_ref = runner_ref
        self.answers = answers  # id -> list of (source_text, translated, src_lang) or []

    def present(self, p):
        start = time.monotonic()
        time.sleep(0.05)  # the phrase's audio
        end = time.monotonic()
        for k, (text, tr, lang) in enumerate(self.answers.get(p.id, [])):
            seg = Segment(0, 1, text, tr, lang, config.other(lang),
                          timing={"speech_start": start, "speech_end": end,
                                  "final": end + 0.1, "first_interim": start + 0.02})
            threading.Timer(0.02 * (k + 1), self.runner_ref[0].on_final, args=(seg,)).start()
        return end


class FakeController:
    def __init__(self, mode):
        self.mode = mode
        self.modes = []
        self.is_switching = False

    def set_mode(self, mode):
        self.modes.append(mode)
        self.mode = mode


class RunnerTest(unittest.TestCase):
    def run_trials(self, answers, mode="auto"):
        ref = [None]
        lat = LatencyLog(None, include_text=True)
        r = Runner(phrases(), mode, FakeSource(ref, answers), lat, tts=None,
                   out=lambda s: None, settle_s=0.05, timeout_s=0.3)
        ref[0] = r
        r.controller = FakeController(config.MODE_EN_TH)
        r.run()
        lat.close()
        return r, lat

    def test_accuracy_counts_and_report(self):
        r, lat = self.run_trials({
            "e1": [("Good morning everyone.", "สวัสดีตอนเช้าทุกคน", "en")],
            "t1": [("สวัสดีครับ", "Hello", "th")],
            "s1": [("เยส", "Yes", "th")],                 # wrong language
            # t2: missed
        })
        self.assertEqual([t.detected for t in r.trials], ["en", "th", "th", None])
        md = summary_md({"started": "now", "mode": "auto", "source": "fake"},
                        r.trials, lat.rows, r.stray)
        self.assertIn("**2/4 correct (50%)**, 1 wrong language, 1 missed", md)
        self.assertIn("`s1` said en, detected th", md)
        self.assertIn("`t2` (th, number)", md)
        self.assertIn("| spoken Thai | 2 | 1 | 0 | 1 |", md)
        self.assertIn("Speech end → final text (Azure) | 3 |", md)
        rows = phrase_rows(r.trials, lat.rows)
        self.assertEqual(rows[0]["similarity"], 1.0)
        self.assertEqual(rows[0]["recognize_s"], 0.1)
        self.assertEqual(rows[0]["speech_end_vs_ref_s"], 0.0)
        with tempfile.TemporaryDirectory() as d:
            write_csv(Path(d) / "p.csv", rows)
            raw = (Path(d) / "p.csv").read_bytes()
            self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))  # Excel opens Thai correctly
            self.assertIn("สวัสดีครับ", raw.decode("utf-8-sig"))

    def test_split_result_and_majority_language(self):
        r, lat = self.run_trials({"e1": [("Good morning", "สวัสดี", "en"),
                                         ("everyone", "ทุกคน", "th")]})
        self.assertEqual(len(r.trials[0].segs), 2)
        self.assertEqual(r.trials[0].detected, "en")  # more characters in English
        md = summary_md({"started": "now", "mode": "auto"}, r.trials, lat.rows, r.stray)
        self.assertIn("Split into several results: e1", md)

    def test_late_tail_goes_to_previous_phrase(self):
        r = Runner(phrases(), "auto", None, LatencyLog(None), out=lambda s: None)
        r.trials[0].t_prompt, r.trials[1].t_prompt = 100.0, 110.0
        r._cur = 1
        early = Segment(0, 1, "tail", "x", "en", "th", timing={"speech_start": 108.0})
        on_time = Segment(0, 1, "new", "y", "th", "en", timing={"speech_start": 110.5})
        self.assertEqual(r._assign(early), 0)
        self.assertEqual(r._assign(on_time), 1)
        r._cur = None
        self.assertIsNone(r._assign(on_time))

    def test_floor_mode_sets_direction_per_phrase(self):
        r, _ = self.run_trials({}, mode="floor")
        self.assertEqual(r.controller.modes, ["th>en", "en>th", "th>en"])  # starts en>th
        self.assertIsNotNone(r.trials[1].switch_s)

    def test_fixed_mode_report_says_detection_not_tested(self):
        r, lat = self.run_trials({"e1": [("Good morning everyone.", "x", "en")]}, mode="en>th")
        md = summary_md({"started": "now", "mode": "en>th"}, r.trials, lat.rows, r.stray)
        self.assertIn("detection was not tested", md)
        self.assertNotIn("correct (", md)

    def test_fatal_stops_the_run(self):
        ref = [None]
        r = Runner(phrases(), "auto", FakeSource(ref, {}), LatencyLog(None),
                   out=lambda s: None, settle_s=0.05, timeout_s=5)
        ref[0] = r
        threading.Timer(0.1, r.on_fatal, args=("boom",)).start()
        t0 = time.monotonic()
        r.run()
        self.assertLess(time.monotonic() - t0, 2)
        self.assertEqual(r.fatal_msg, "boom")


class SyntheticSourceTest(unittest.TestCase):
    def test_feeds_real_time_chunks_and_reports_end(self):
        fed = []
        src = SyntheticSource(lambda b: fed.append((time.monotonic(), b)), prompts=None)
        pcm = b"\x01\x00" * 4000  # 0.25 s at 16 kHz
        src.prompts = type("P", (), {"get": lambda self, p: pcm})()
        src.start()
        self.addCleanup(src.stop)
        t0 = time.monotonic()
        end = src.present(Phrase("x", "en", "normal", "x"))
        took = time.monotonic() - t0
        self.assertGreater(took, 0.2)  # real time, not instant
        self.assertTrue(all(len(b) == 3200 for _, b in fed))  # whole 100 ms chunks
        speech = b"".join(b for _, b in fed)
        self.assertIn(pcm, speech)
        last_t = next(t for t, b in reversed(fed) if b"\x01" in b)
        self.assertAlmostEqual(end, last_t - 0.05, delta=0.02)  # 50 ms of padding in last chunk
        self.assertTrue(wait_until(lambda: len(fed) > 6))  # silence keeps flowing


if __name__ == "__main__":
    unittest.main()
