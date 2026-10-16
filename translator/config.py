"""Settings and Azure credentials.

The key and region come from environment variables or a .env file next to
main.py (or next to the .exe). They are never written anywhere by this app.
"""
import os
import re
import socket
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

if getattr(sys, "frozen", False):
    # Packaged .exe: the code is unpacked to a temp folder, so keep .env,
    # transcripts, logs and usage.json next to the .exe instead.
    APP_DIR = Path(sys.executable).resolve().parent
else:
    APP_DIR = Path(__file__).resolve().parent.parent
TRANSCRIPT_DIR = APP_DIR / "transcripts"
USAGE_FILE = APP_DIR / "usage.json"
LOG_DIR = APP_DIR / "logs"  # per-session latency logs (latency-*.jsonl)


@dataclass(frozen=True)
class Language:
    code: str    # translation code, also the key used everywhere else ("th")
    locale: str  # recognition / SSML locale ("th-TH")
    name: str


LANGUAGES = {
    "en": Language("en", "en-US", "English"),
    "th": Language("th", "th-TH", "Thai"),
}
PAIR = ("en", "th")  # Language A <-> Language B

# Direction modes. Manual modes fix source and target. AUTO lets Azure detect
# which of the pair is being spoken per utterance and translates into the
# other one (continuous language ID). AUTO is experimental until it has been
# measured with real speech (accuracy and latency).
MODE_EN_TH = "en>th"
MODE_TH_EN = "th>en"
MODE_AUTO = "auto"
MODES = (MODE_EN_TH, MODE_TH_EN, MODE_AUTO)
DEFAULT_MODE = MODE_EN_TH


def direction(mode: str):
    """(source, target) codes for a manual mode; None for MODE_AUTO."""
    if mode == MODE_AUTO:
        return None
    src, _, tgt = mode.partition(">")
    if src not in LANGUAGES or tgt not in LANGUAGES or src == tgt:
        raise ValueError(f"unknown mode {mode!r}")
    return src, tgt


def other(lang: str) -> str:
    a, b = PAIR
    return b if lang == a else a


def lang_for_locale(locale: str):
    """'th-TH' -> 'th'; None if it isn't one of the pair."""
    loc = (locale or "").strip().lower()
    for code in PAIR:
        if LANGUAGES[code].locale.lower() == loc:
            return code
    return None


def mode_label(mode: str) -> str:
    if mode == MODE_AUTO:
        a, b = (LANGUAGES[c].name for c in PAIR)
        return f"Auto-detect {a}/{b}"
    src, tgt = direction(mode)
    return f"{LANGUAGES[src].name} → {LANGUAGES[tgt].name}"


# Voice per target language. Override in .env, e.g. TTS_VOICE_EN=en-US-AvaNeural.
DEFAULT_VOICES = {
    "en": "en-US-JennyNeural",
    "th": "th-TH-NiwatNeural",
}

# Azure Speech Free (F0) monthly allowances, checked 2026-10-16 at
# https://azure.microsoft.com/en-us/pricing/details/cognitive-services/speech-services/
F0_TRANSLATION_AUDIO_HOURS = 5
F0_NEURAL_TTS_CHARS = 500_000
# F0 request limits, from
# https://learn.microsoft.com/en-us/azure/ai-services/speech-service/speech-services-quotas-and-limits
F0_TTS_TRANSACTIONS_PER_MINUTE = 20
USAGE_WARN_FRACTION = 0.8

# Reconnect policy: only for genuine connection drops after a working session.
# Long-form use (lectures, all-day meetings): never give up on our own; keep
# retrying with backoff until the user presses Stop. Set a number to cap it.
MAX_RECONNECT_ATTEMPTS = None
RECONNECT_BACKOFF_S = (1, 2, 4, 8, 15, 30)
STABLE_SESSION_S = 60  # a session this long resets the backoff
# Audio kept while the recognizer is away (reconnect or direction switch) and
# replayed into the next one, so short outages lose no speech.
RECONNECT_BUFFER_S = 20.0

# TTS queue policy (see tts.py): meeting = catch up (drop the oldest unspoken
# sentences beyond TTS_MAX_BACKLOG waiting); onsite = every sentence in order,
# same-language waiting sentences merged into one request up to this size.
TTS_MAX_BACKLOG = 2
TTS_MERGE_MAX_CHARS = 400
TTS_BEHIND_WARN_S = 15.0  # onsite: show "voice is N s behind" beyond this
TTS_CACHE_ITEMS = 50      # synthesized utterances kept for instant replay
# Extra time recognition stays paused after TTS ends (room echo tail).
PAUSE_TAIL_S = 0.4

# Direction switch: how long to wait for the old recognizer's last sentence,
# and how much of the next speaker's audio to buffer meanwhile.
SWITCH_FLUSH_TIMEOUT_S = 2.0
SWITCH_BUFFER_S = 5.0

# Onsite (one shared device): speakers in the room ring longer than headphones,
# so the mic stays gated longer after the voice ends.
ONSITE_TAIL_S = 0.7
# After a translation has been read, give the floor to the other side.
ONSITE_AUTO_ALTERNATE = True
# Who has the floor when onsite starts: None = automatic (the detected
# language of each sentence decides; the mic stays open for both people).
ONSITE_DEFAULT_SIDE = None
# Text-based echo guard: a final result this similar (0..1) to something the
# voice said within ECHO_WINDOW_S is treated as the app hearing itself.
ECHO_SIMILARITY = 0.8
ECHO_WINDOW_S = 15.0

# End-of-speech silence before Azure finalizes a sentence, in ms. Unset = the
# service default. Shorter gives earlier translations but splits sentences at
# pauses. Set SEGMENTATION_SILENCE_MS in .env to experiment (see step 5 notes
# in README); the measurement tool has --silence-ms.
SEGMENTATION_SILENCE_RANGE = (100, 5000)


def segmentation_silence_ms():
    raw = os.environ.get("SEGMENTATION_SILENCE_MS", "").strip()
    if not raw:
        return None
    try:
        ms = int(raw)
    except ValueError:
        raise ConfigError(f"SEGMENTATION_SILENCE_MS='{raw}' must be a whole number of ms.")
    lo, hi = SEGMENTATION_SILENCE_RANGE
    if not lo <= ms <= hi:
        raise ConfigError(f"SEGMENTATION_SILENCE_MS must be between {lo} and {hi}.")
    return ms


class ConfigError(Exception):
    """Credentials are missing or wrong. Never retried."""


@dataclass(frozen=True)
class Credentials:
    key: str
    region: str


def load_credentials() -> Credentials:
    load_dotenv(APP_DIR / ".env")
    key = os.environ.get("AZURE_SPEECH_KEY", "").strip()
    region = os.environ.get("AZURE_SPEECH_REGION", "").strip().lower()
    if not key or not region:
        raise ConfigError(
            "AZURE_SPEECH_KEY and AZURE_SPEECH_REGION are not set. "
            "Copy .env.example to .env and fill them in (see README)."
        )
    if not re.fullmatch(r"[a-z0-9]+", region):
        raise ConfigError(
            f"AZURE_SPEECH_REGION '{region}' doesn't look like a region name. "
            "Use the short form shown in the Azure portal, e.g. 'southeastasia'."
        )
    return Credentials(key, region)


def load_voices(chosen: dict = None) -> dict:
    """Voice name per language: DEFAULT_VOICES overridden by TTS_VOICE_<CODE>,
    overridden by `chosen` (the voices picked in the app, see voices.py).

    A voice must belong to its language's locale family (th-* for Thai) unless
    it is a Multilingual voice, so a typo can't make English text be read by
    the Thai voice or vice versa."""
    load_dotenv(APP_DIR / ".env")
    voices = {}
    for code in PAIR:
        name = ((chosen or {}).get(code) or os.environ.get(f"TTS_VOICE_{code.upper()}", "").strip()
                or DEFAULT_VOICES[code])
        if not re.fullmatch(r"[a-z]{2,3}-[A-Za-z]{2,4}(-[A-Za-z]+)?-[A-Za-z0-9]+", name):
            raise ConfigError(f"TTS_VOICE_{code.upper()}='{name}' doesn't look like an Azure "
                              "voice name, e.g. 'en-US-JennyNeural'.")
        if not name.lower().startswith(code + "-") and "multilingual" not in name.lower():
            raise ConfigError(f"TTS_VOICE_{code.upper()}='{name}' is not a "
                              f"{LANGUAGES[code].name} voice.")
        voices[code] = name
    return voices


def preflight_check(creds: Credentials, timeout: float = 8.0) -> None:
    """Validate key + region before opening a recognizer.

    A wrong region makes the Speech SDK report ConnectionFailure, which looks
    exactly like a network drop. Asking the token endpoint first lets us tell
    "wrong key/region" (ConfigError, no retry) from "network problem"
    (ConnectionError).
    """
    url = f"https://{creds.region}.api.cognitive.microsoft.com/sts/v1.0/issueToken"
    req = urllib.request.Request(
        url, data=b"", method="POST",
        headers={"Ocp-Apim-Subscription-Key": creds.key},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout):
            return
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise ConfigError(
                f"Azure rejected the key for region '{creds.region}'. Check "
                "AZURE_SPEECH_KEY, and that AZURE_SPEECH_REGION is the region "
                "of that same Speech resource."
            ) from None
        if e.code == 403:
            raise ConfigError(
                "Azure refused access (403). The resource may be disabled or "
                "the free monthly quota may be used up. Check the Azure portal."
            ) from None
        raise ConnectionError(f"Azure token check failed: HTTP {e.code}") from None
    except urllib.error.URLError as e:
        if isinstance(e.reason, socket.gaierror):
            # Either the region name doesn't exist or there's no DNS at all.
            if _internet_reachable():
                raise ConfigError(
                    f"Region '{creds.region}' was not found. Check "
                    "AZURE_SPEECH_REGION (e.g. 'southeastasia', 'eastus')."
                ) from None
        raise ConnectionError(f"Can't reach Azure: {e.reason}") from None
    except (socket.timeout, TimeoutError):
        raise ConnectionError("Azure didn't respond (timeout).") from None


def _internet_reachable() -> bool:
    try:
        socket.getaddrinfo("eastus.api.cognitive.microsoft.com", 443)
        return True
    except OSError:
        return False
