"""Azure neural voices for the voice picker: list (cached), saved choice and
a short spoken preview.

The list comes from the Speech service's voices/list endpoint for the
configured region and is cached in voices-cache.json next to the app (it is
public catalogue data, no secrets). The chosen voices are saved in
settings.json and take precedence over TTS_VOICE_* in .env.
"""
import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from . import config

CACHE_FILE = config.APP_DIR / "voices-cache.json"
SETTINGS_FILE = config.APP_DIR / "settings.json"
CACHE_MAX_AGE_S = 30 * 24 * 3600

PREVIEW_TEXT = {
    "en": "Hello! This is how I will read your translations.",
    "th": "สวัสดีค่ะ นี่คือเสียงที่จะใช้อ่านคำแปลของคุณ",
}


@dataclass(frozen=True)
class Voice:
    name: str          # ShortName, e.g. en-US-JennyNeural
    display: str       # "Jenny"
    gender: str        # "Female" | "Male" | ""
    locale: str        # "en-US"
    locale_name: str   # "English (United States)"
    lang: str          # "en" | "th"
    kind: str = "Neural"

    @property
    def initials(self) -> str:
        return (self.display[:1] or "?").upper()


def _from_api(d: dict) -> Optional[Voice]:
    name = d.get("ShortName", "")
    lang = name[:2].lower()
    if lang not in config.LANGUAGES or d.get("VoiceType", "Neural") != "Neural":
        return None
    display = d.get("DisplayName") or name.split("-")[-1].replace("Neural", "")
    return Voice(name=name, display=display, gender=d.get("Gender", ""),
                 locale=d.get("Locale", name[:5]), locale_name=d.get("LocaleName", ""),
                 lang=lang)


def fetch(creds: config.Credentials, timeout: float = 10.0) -> List[Voice]:
    url = f"https://{creds.region}.tts.speech.microsoft.com/cognitiveservices/voices/list"
    req = urllib.request.Request(url, headers={"Ocp-Apim-Subscription-Key": creds.key})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise config.ConfigError("Azure rejected the key while loading voices. "
                                     "Check AZURE_SPEECH_KEY and AZURE_SPEECH_REGION.") from None
        raise ConnectionError(f"Loading voices failed: HTTP {e.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ConnectionError(f"Can't reach Azure to load voices: {e}") from None
    voices = [v for v in (_from_api(d) for d in data) if v is not None]
    voices.sort(key=lambda v: (v.lang, v.locale != config.LANGUAGES[v.lang].locale,
                               v.locale, v.display.lower()))
    return voices


def load_cached(max_age_s: float = CACHE_MAX_AGE_S) -> Optional[List[Voice]]:
    try:
        raw = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        if time.time() - float(raw.get("fetched", 0)) > max_age_s:
            return None
        return [Voice(**v) for v in raw["voices"]]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save_cache(voices: List[Voice]) -> None:
    try:
        CACHE_FILE.write_text(json.dumps({"fetched": time.time(),
                                          "voices": [v.__dict__ for v in voices]},
                                         ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def get_voices(refresh: bool = False) -> List[Voice]:
    """Cached list, or fetched from Azure (needs the key). Raises ConfigError
    / ConnectionError."""
    if not refresh:
        cached = load_cached()
        if cached:
            return cached
    voices = fetch(config.load_credentials())
    if voices:
        save_cache(voices)
    return voices


# ------------------------------------------------------------- saved choice
def load_settings() -> dict:
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(data: dict) -> None:
    try:
        SETTINGS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def chosen() -> Dict[str, str]:
    v = load_settings().get("voices", {})
    return {k: s for k, s in v.items() if k in config.LANGUAGES and isinstance(s, str) and s}


def save_choice(lang: str, name: str) -> None:
    data = load_settings()
    data.setdefault("voices", {})[lang] = name
    save_settings(data)


def short_label(name: str) -> str:
    """'en-US-JennyNeural' -> 'Jenny'."""
    last = name.split("-")[-1]
    for suffix in ("MultilingualNeural", "Neural"):
        if last.endswith(suffix) and last != suffix:
            return last[: -len(suffix)] + (" (Multilingual)" if suffix.startswith("Multi") else "")
    return last


# --------------------------------------------------------------- preview
class Previewer:
    """Speaks a short sample with a voice on the chosen output device. One at
    a time; a new preview cancels the previous one."""

    def __init__(self, make_player: Callable[[], object],
                 on_state: Callable[[Optional[str], Optional[str]], None]):
        self.make_player = make_player
        self.on_state = on_state  # (voice name playing or None, error or None)
        self._cancel = threading.Event()
        self._lock = threading.Lock()

    def play(self, voice: Voice) -> None:
        self.stop()
        cancel = self._cancel = threading.Event()
        threading.Thread(target=self._run, args=(voice, cancel), daemon=True,
                         name="voice-preview").start()

    def stop(self) -> None:
        self._cancel.set()

    def _run(self, voice: Voice, cancel: threading.Event) -> None:
        from .tts import TTS_RATE, build_ssml
        with self._lock:
            if cancel.is_set():
                return
            self.on_state(voice.name, None)
            player = None
            try:
                import azure.cognitiveservices.speech as speechsdk
                creds = config.load_credentials()
                cfg = speechsdk.SpeechConfig(subscription=creds.key, region=creds.region)
                cfg.set_speech_synthesis_output_format(
                    speechsdk.SpeechSynthesisOutputFormat.Raw24Khz16BitMonoPcm)
                synth = speechsdk.SpeechSynthesizer(speech_config=cfg, audio_config=None)
                ssml = build_ssml(PREVIEW_TEXT[voice.lang], voice.lang, voice.name)
                result = synth.speak_ssml_async(ssml).get()
                if result.reason != speechsdk.ResultReason.SynthesizingAudioCompleted:
                    detail = getattr(result, "cancellation_details", None)
                    raise RuntimeError(getattr(detail, "error_details", "") or "synthesis failed")
                if cancel.is_set():
                    self.on_state(None, None)
                    return
                player = self.make_player()
                player.play(result.audio_data, TTS_RATE, cancel)
                self.on_state(None, None)
            except config.ConfigError as e:
                self.on_state(None, str(e))
            except Exception as e:
                self.on_state(None, f"Preview failed: {e}")
            finally:
                if player is not None:
                    try:
                        player.close()
                    except Exception:
                        pass
