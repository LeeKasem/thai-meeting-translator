"""Settings and Azure credentials.

The key and region come from environment variables or a .env file next to
main.py. They are never written anywhere by this app.
"""
import os
import re
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

APP_DIR = Path(__file__).resolve().parent.parent
TRANSCRIPT_DIR = APP_DIR / "transcripts"
USAGE_FILE = APP_DIR / "usage.json"

SOURCE_LANGUAGE = "en-US"
TARGET_LANGUAGE = "th"
TTS_VOICE = "th-TH-NiwatNeural"

# Azure Speech Free (F0) monthly allowances, checked 2026-10-16 at
# https://azure.microsoft.com/en-us/pricing/details/cognitive-services/speech-services/
F0_TRANSLATION_AUDIO_HOURS = 5
F0_NEURAL_TTS_CHARS = 500_000
# F0 request limits, from
# https://learn.microsoft.com/en-us/azure/ai-services/speech-service/speech-services-quotas-and-limits
F0_TTS_TRANSACTIONS_PER_MINUTE = 20
USAGE_WARN_FRACTION = 0.8

# Reconnect policy: only for genuine connection drops after a working session.
MAX_RECONNECT_ATTEMPTS = 5
RECONNECT_BACKOFF_S = (1, 2, 4, 8, 15)
STABLE_SESSION_S = 60  # a session this long resets the retry counter

# TTS: drop the oldest unspoken sentences beyond this many waiting.
TTS_MAX_BACKLOG = 2
# Extra time recognition stays paused after TTS ends (room echo tail).
PAUSE_TAIL_S = 0.4


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
