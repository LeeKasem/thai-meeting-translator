"""Redesigned UI: theme tokens, components, voice settings and the live state
shown on the record button."""
import json
import tempfile
import time
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from translator import config, paint, theme, ui, voices
from translator.session import State

from test_lifecycle import Fakes, headless_app, run_ui_until


class ThemeTest(unittest.TestCase):
    def test_tokens_and_tints(self):
        t = theme.Theme(None, dark=False, reduced_motion=False)
        self.assertEqual(t.c["primary"], "#1073E6")
        self.assertEqual(t.c["danger"], "#F25767")
        self.assertEqual(t.c["primary_10"], theme.mix("#1073E6", "#FFFFFF", 0.10))
        d = theme.Theme(None, dark=True, reduced_motion=False)
        self.assertNotEqual(d.c["bg"], t.c["bg"])
        self.assertEqual(set(d.c), set(t.c))  # dark mode defines every token

    def test_easing_curve(self):
        self.assertEqual(theme.ease(0), 0.0)
        self.assertEqual(theme.ease(1), 1.0)
        self.assertGreater(theme.ease(0.3), 0.3)  # fast start, soft landing
        xs = [theme.ease(i / 20) for i in range(21)]
        self.assertEqual(xs, sorted(xs))

    def test_reduced_motion_makes_durations_zero(self):
        t = theme.Theme(None, reduced_motion=True)
        self.assertEqual(t.dur(theme.DUR_BASE), 0)

    def test_lang_of(self):
        self.assertEqual(theme.lang_of("hello"), "en")
        self.assertEqual(theme.lang_of("ok สวัสดี"), "th")


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


class ComponentTest(TkCase):
    def test_toggle_follows_variable_and_click(self):
        from translator.widgets import Toggle
        var = tk.BooleanVar(value=False)
        calls = []
        t = Toggle(self.root, var, command=lambda: calls.append(var.get()))
        t.pack()
        self.root.update()
        t.activate(None)
        self.assertTrue(var.get())
        self.assertEqual(calls, [True])
        var.set(False)  # set by the app (e.g. onsite forces pause on)
        self.root.update()
        self.assertEqual(t.anim.value, 0.0)
        t.configure(state="disabled")
        t._key_activate()
        self.assertFalse(var.get())

    def test_pill_tabs_skip_disabled_options(self):
        from translator.widgets import PillTabs
        var = tk.StringVar(value="a")
        tabs = PillTabs(self.root, (("a", "A", None), ("b", "B", None), ("c", "C", None)), var,
                        stretch=True)
        tabs.pack(fill="x")
        self.root.update()
        tabs.option("b").configure(state="disabled")
        tabs._step(1)
        self.assertEqual(var.get(), "c")
        tabs._pick(1)
        self.assertEqual(var.get(), "c")
        tabs.option("b").configure(state="normal")
        tabs._pick(1)
        self.assertEqual(var.get(), "b")

    def test_pill_select_is_combobox_compatible(self):
        from translator.widgets import PillSelect
        s = PillSelect(self.root)
        s.pack(fill="x")
        fired = []
        s.bind("<<ComboboxSelected>>", lambda e: fired.append(s.get()))
        s["values"] = ["One", "Two"]
        self.assertEqual(s["values"], ("One", "Two"))
        s.set("One")
        self.assertEqual(s.get(), "One")
        s.configure(state="readonly")
        self.assertTrue(s.enabled)
        s.configure(state="disabled")
        self.assertFalse(s.enabled)
        s.configure(state="readonly")
        self.root.update()
        s._picked("Two")
        self.root.update()
        self.assertEqual((s.get(), fired), ("Two", ["Two"]))

    def test_record_button_blocks_clicks_while_busy(self):
        from translator.widgets import RecordButton
        clicks = []
        b = RecordButton(self.root, command=lambda: clicks.append(1))
        b.pack()
        self.root.update()
        for st in ("starting", "stopping"):
            b.set_state(st)
            b._key_activate()
        self.assertEqual(clicks, [])
        b.set_state("listening", level=0.5)
        b._key_activate()
        self.assertEqual(clicks, [1])
        self.assertEqual(b.label, "Stop translating")
        b.set_state("speaking", mic_muted=True)
        self.root.update()  # draws the muted-mic badge without errors

    def test_modal_closes_on_escape_and_backdrop(self):
        from translator.widgets import Modal, Overlay
        closed = []
        m = Modal(self.root, "Title", lambda body: tk.Label(body, text="x").pack(),
                  on_close=lambda: closed.append(1))
        self.root.update()
        self.assertIs(Overlay.topmost(), m)
        x, y, w, h = m.box
        m._backdrop_click(SimpleNamespace(x=x + w // 2, y=y + h // 2))  # inside: stays
        self.assertFalse(m.closed)
        m._backdrop_click(SimpleNamespace(x=2, y=2))
        self.assertTrue(m.closed)
        self.assertEqual(closed, [1])
        self.assertIsNone(Overlay.topmost())


class MessageListTest(TkCase):
    def make(self):
        from translator.messages import MessageList
        frame = tk.Frame(self.root, bg="#FFFFFF")
        frame.pack(fill="both", expand=True)
        ml = MessageList(frame, on_replay=lambda i: None)
        ml.pack(fill="both", expand=True)
        self.root.update()
        return ml

    def add(self, ml, n, start=1):
        for i in range(start, start + n):
            ml.add_message(i, f"Sentence number {i} in English.", f"ประโยคที่ {i}", "en", "th",
                           time.time())
        self.root.update()

    def test_follows_newest_until_user_scrolls_up(self):
        ml = self.make()
        self.add(ml, 12)
        self.assertGreaterEqual(ml.cv.yview()[1], 0.999)
        self.assertFalse(ml.jump.winfo_ismapped())
        ml._scroll(-1, "pages")
        self.root.update()
        self.assertFalse(ml.stick)
        self.add(ml, 2, start=13)
        self.assertLess(ml.cv.yview()[1], 0.999)  # did not jump away from the reader
        self.assertEqual(ml.unseen, 2)
        self.assertTrue(ml.jump.winfo_ismapped())
        ml.scroll_to_end()
        self.root.update()
        self.assertTrue(ml.stick)
        self.assertFalse(ml.jump.winfo_ismapped())

    def test_interim_card_is_replaced_by_final(self):
        ml = self.make()
        ml.set_interim("Hel", "สวั", "en", "th")
        self.root.update()
        self.assertTrue(ml.cv.find_withtag("live"))
        self.add(ml, 1)
        self.assertFalse(ml.cv.find_withtag("live"))

    def test_large_text_and_two_sided_relayout(self):
        ml = self.make()
        self.add(ml, 2)
        h = ml.items[0]["h"]
        ml.set_large(True)
        self.assertGreater(ml.items[0]["h"], h)
        ml.set_two_sided(True)
        self.assertTrue(ml.replay_bbox(1))


class VoicesTest(unittest.TestCase):
    def test_api_rows_filtered_to_pair_and_neural(self):
        rows = [{"ShortName": "en-US-JennyNeural", "DisplayName": "Jenny", "Gender": "Female",
                 "Locale": "en-US", "LocaleName": "English (US)", "VoiceType": "Neural"},
                {"ShortName": "fr-FR-DeniseNeural", "VoiceType": "Neural"},
                {"ShortName": "en-US-OldStandard", "VoiceType": "Standard"},
                {"ShortName": "th-TH-NiwatNeural", "DisplayName": "Niwat", "Gender": "Male",
                 "Locale": "th-TH", "VoiceType": "Neural"}]
        out = [v for v in map(voices._from_api, rows) if v]
        self.assertEqual([(v.name, v.lang) for v in out],
                         [("en-US-JennyNeural", "en"), ("th-TH-NiwatNeural", "th")])

    def test_short_label(self):
        self.assertEqual(voices.short_label("en-US-JennyNeural"), "Jenny")
        self.assertEqual(voices.short_label("en-US-AvaMultilingualNeural"), "Ava (Multilingual)")

    def test_choice_saved_and_takes_precedence(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(voices, "SETTINGS_FILE", Path(d) / "settings.json"), \
                mock.patch.dict("os.environ", {"TTS_VOICE_TH": "th-TH-AcharaNeural"}):
            voices.save_choice("en", "en-GB-SoniaNeural")
            self.assertEqual(voices.chosen(), {"en": "en-GB-SoniaNeural"})
            data = json.loads((Path(d) / "settings.json").read_text(encoding="utf-8"))
            self.assertEqual(data["voices"]["en"], "en-GB-SoniaNeural")
            got = config.load_voices(voices.chosen())
            self.assertEqual(got, {"en": "en-GB-SoniaNeural", "th": "th-TH-AcharaNeural"})

    def test_chosen_voice_still_validated(self):
        with self.assertRaises(config.ConfigError):
            config.load_voices({"th": "en-US-JennyNeural"})

    def test_cache_round_trip_and_expiry(self):
        v = voices.Voice("th-TH-NiwatNeural", "Niwat", "Male", "th-TH", "Thai", "th")
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(voices, "CACHE_FILE", Path(d) / "c.json"):
            voices.save_cache([v])
            self.assertEqual(voices.load_cached(), [v])
            self.assertIsNone(voices.load_cached(max_age_s=-1))


class LiveStateTest(unittest.TestCase):
    """What the record button shows, from the session state."""

    def test_states(self):
        with Fakes() as f:
            app = headless_app("onsite")
            app._apply_setting()
            self.assertEqual(app._live_state(), ("idle", False))
            app.on_start()
            self.assertEqual(app._live_state()[0], "starting")
            self.assertTrue(run_ui_until(app, lambda: app.state is State.RUNNING))
            tts = f.ttss[0]
            tts.tts_state = "idle"
            self.assertEqual(app._live_state(), ("listening", False))
            app._show_interim("Hel", "สวั", "en", "th")
            self.assertEqual(app._live_state(), ("translating", False))
            tts.tts_state = "speaking"
            self.assertEqual(app._live_state(), ("speaking", True))  # onsite: mic gated
            app.set_status = ui.App.set_status.__get__(app)
            app.set_status("reconnecting", "Reconnecting…")
            self.assertEqual(app._live_state()[0], "reconnecting")
            app.on_stop()
            self.assertEqual(app._live_state()[0], "stopping")
            self.assertTrue(run_ui_until(app, lambda: app.state is State.IDLE))

    def test_meeting_speaking_without_pause_keeps_mic_open(self):
        with Fakes() as f:
            app = headless_app("meeting")
            app._apply_setting()
            app.on_start()
            self.assertTrue(run_ui_until(app, lambda: app.state is State.RUNNING))
            f.ttss[0].tts_state = "speaking"
            self.assertEqual(app._live_state(), ("speaking", False))
            app.on_stop()
            run_ui_until(app, lambda: app.state is State.IDLE)

    def test_playing_message_is_highlighted(self):
        with Fakes() as f:
            app = headless_app()
            app.on_start()
            self.assertTrue(run_ui_until(app, lambda: app.state is State.RUNNING))
            app._on_voice_playing(SimpleNamespace(text="x", lang="th", seg_ids=(1, 2)))
            f.ttss[0].tts_state = "speaking"
            app._refresh_live()
            app.messages.set_playing.assert_called_with((1, 2))
            f.ttss[0].tts_state = "idle"
            app._refresh_live()
            app.messages.set_playing.assert_called_with(())
            app.on_stop()
            run_ui_until(app, lambda: app.state is State.IDLE)


class MicPermissionTest(unittest.TestCase):
    def test_blocked_mic_explains_and_does_not_start(self):
        with Fakes() as f, mock.patch.object(ui.winmic, "mic_blocked", return_value=True):
            app = headless_app()
            shown = []
            app._show_mic_blocked = lambda: shown.append(1)
            app.on_start()
            self.assertEqual(shown, [1])
            self.assertIs(app.state, State.IDLE)
            self.assertEqual(f.pumps, [])

    def test_loopback_is_not_affected(self):
        with Fakes(), mock.patch.object(ui.winmic, "mic_blocked", return_value=True):
            app = headless_app()
            dev = SimpleNamespace(name="dev", render_name="dev", kind="loopback")
            app._selected = lambda combo, devs: dev
            app.on_start()
            self.assertTrue(run_ui_until(app, lambda: app.state is State.RUNNING))
            app.on_stop()
            run_ui_until(app, lambda: app.state is State.IDLE)


class LanguageRowTest(unittest.TestCase):
    def test_picks_map_to_modes(self):
        with Fakes():
            app = headless_app()
            app.mode_var = mock.MagicMock()
            mode = {"v": config.MODE_EN_TH}
            app.mode_var.get.side_effect = lambda: mode["v"]
            app.mode_var.set.side_effect = lambda v: mode.update(v=v)
            app._on_tgt_pick("English")
            self.assertEqual(mode["v"], config.MODE_TH_EN)
            app._swap()
            self.assertEqual(mode["v"], config.MODE_EN_TH)
            app._on_src_pick(ui.SRC_LABELS[config.MODE_AUTO])
            self.assertEqual(mode["v"], config.MODE_AUTO)
            app._swap()  # no-op in auto
            self.assertEqual(mode["v"], config.MODE_AUTO)
            app._on_src_pick("Thai")
            self.assertEqual(mode["v"], config.MODE_TH_EN)


if __name__ == "__main__":
    unittest.main()
