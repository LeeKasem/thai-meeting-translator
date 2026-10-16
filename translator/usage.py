"""Usage counter: this session, plus a month-to-date total kept in usage.json.

The month total only counts usage made by this app on this PC. The Azure
portal (resource > Metrics) is the authoritative number.
"""
import json
from datetime import datetime
from pathlib import Path

from . import config


class Usage:
    def __init__(self, path: Path = config.USAGE_FILE):
        self.path = path
        self.month = datetime.now().strftime("%Y-%m")
        self._prev_audio_s = 0.0
        self._prev_chars = 0
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            m = data.get(self.month, {})
            self._prev_audio_s = float(m.get("audio_seconds", 0))
            self._prev_chars = int(m.get("tts_chars", 0))
        except (OSError, ValueError):
            pass
        self.session_audio_s = 0.0
        self.session_chars = 0

    def new_session(self) -> None:
        self.save()
        self._prev_audio_s += self.session_audio_s
        self._prev_chars += self.session_chars
        self.session_audio_s = 0.0
        self.session_chars = 0

    @property
    def month_audio_s(self) -> float:
        return self._prev_audio_s + self.session_audio_s

    @property
    def month_chars(self) -> int:
        return self._prev_chars + self.session_chars

    def warnings(self) -> list:
        out = []
        audio_limit = config.F0_TRANSLATION_AUDIO_HOURS * 3600
        if self.month_audio_s >= audio_limit * config.USAGE_WARN_FRACTION:
            out.append(f"Translation audio {self.month_audio_s / 3600:.1f} h of "
                       f"{config.F0_TRANSLATION_AUDIO_HOURS} h free this month")
        if self.month_chars >= config.F0_NEURAL_TTS_CHARS * config.USAGE_WARN_FRACTION:
            out.append(f"TTS {self.month_chars:,} of {config.F0_NEURAL_TTS_CHARS:,} "
                       f"free characters this month")
        return out

    def save(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        data[self.month] = {"audio_seconds": round(self.month_audio_s, 1),
                            "tts_chars": self.month_chars}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(self.path)
