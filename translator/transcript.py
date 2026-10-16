"""Transcript storage, auto-save and .txt/.srt export.

Files are UTF-8 with BOM and CRLF line endings so Notepad, Word and video
players on Windows show Thai correctly.
"""
import os
from datetime import datetime
from pathlib import Path
from typing import List

from .recognition import Segment


def _clock(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def _srt_time(seconds: float) -> str:
    ms = max(0, int(round(seconds * 1000)))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _txt_entry(seg: Segment) -> str:
    return f"[{_clock(seg.start)}]\nEN: {seg.english}\nTH: {seg.thai}\n\n"


class Transcript:
    def __init__(self, directory: Path, session_start: float):
        self.session_start = session_start
        self.segments: List[Segment] = []
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.fromtimestamp(session_start).strftime("%Y%m%d_%H%M%S")
        self.txt_path = directory / f"meeting_{stamp}.txt"
        self.srt_path = directory / f"meeting_{stamp}.srt"
        self._header = (f"Meeting transcript (English -> Thai), started "
                        f"{datetime.fromtimestamp(session_start):%Y-%m-%d %H:%M:%S}\n"
                        f"Machine translation: an aid, not an official record.\n\n")

    def add(self, seg: Segment) -> None:
        """Record a final segment and auto-save it immediately."""
        new_file = not self.segments
        self.segments.append(seg)
        mode, enc = ("w", "utf-8-sig") if new_file else ("a", "utf-8")
        with open(self.txt_path, mode, encoding=enc, newline="\r\n") as f:
            if new_file:
                f.write(self._header)
            f.write(_txt_entry(seg))
            f.flush()
            os.fsync(f.fileno())
        _atomic_write(self.srt_path, self.to_srt())

    def to_txt(self) -> str:
        return self._header + "".join(_txt_entry(s) for s in self.segments)

    def to_srt(self) -> str:
        blocks = []
        for i, s in enumerate(self.segments, 1):
            start = s.start - self.session_start
            end = max(s.end - self.session_start, start + 1.0)
            blocks.append(f"{i}\n{_srt_time(start)} --> {_srt_time(end)}\n{s.thai}\n{s.english}\n")
        return "\n".join(blocks)

    def export(self, path: Path) -> None:
        text = self.to_srt() if path.suffix.lower() == ".srt" else self.to_txt()
        _atomic_write(path, text)


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8-sig", newline="\r\n") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
