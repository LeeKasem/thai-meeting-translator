"""Audio devices, capture (mic or WASAPI loopback), resampling and playback.

Everything goes through pyaudiowpatch on the WASAPI host API so that mic,
loopback and output devices share one naming scheme, which lets us detect
"TTS plays on the device we're capturing".

WASAPI shared-mode streams must run at the device's own mix rate, so capture
is opened at the native rate and converted here to 16 kHz 16-bit mono for the
Speech SDK, and TTS audio (24 kHz mono) is converted to the output's format.
"""
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, List, Optional

import numpy as np
import pyaudiowpatch as pyaudio

from .latency import StreamClock

SDK_RATE = 16000
LOOPBACK_SUFFIX = " [Loopback]"


@dataclass(frozen=True)
class Device:
    index: int
    name: str
    kind: str  # "mic" | "loopback" | "output"
    rate: int
    channels: int

    @property
    def render_name(self) -> str:
        """Name of the playback endpoint this device belongs to."""
        if self.kind == "loopback" and self.name.endswith(LOOPBACK_SUFFIX):
            return self.name[: -len(LOOPBACK_SUFFIX)]
        return self.name


@dataclass
class DeviceList:
    mics: List[Device]
    loopbacks: List[Device]
    outputs: List[Device]
    default_mic: Optional[int]
    default_loopback: Optional[int]
    default_output: Optional[int]


def list_devices(pa: "pyaudio.PyAudio") -> DeviceList:
    wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
    mics, loops, outs = [], [], []
    for i in range(wasapi["deviceCount"]):
        d = pa.get_device_info_by_host_api_device_index(wasapi["index"], i)
        rate = int(d["defaultSampleRate"])
        if d.get("isLoopbackDevice"):
            loops.append(Device(d["index"], d["name"], "loopback", rate, d["maxInputChannels"]))
        elif d["maxInputChannels"] > 0:
            mics.append(Device(d["index"], d["name"], "mic", rate, d["maxInputChannels"]))
        elif d["maxOutputChannels"] > 0:
            outs.append(Device(d["index"], d["name"], "output", rate, d["maxOutputChannels"]))

    def _safe(fn):
        try:
            return fn()
        except (IOError, OSError, LookupError):
            return None

    default_mic = wasapi["defaultInputDevice"] if wasapi["defaultInputDevice"] >= 0 else None
    default_out = wasapi["defaultOutputDevice"] if wasapi["defaultOutputDevice"] >= 0 else None
    lb = _safe(pa.get_default_wasapi_loopback)
    return DeviceList(mics, loops, outs, default_mic, lb["index"] if lb else None, default_out)


class StreamResampler:
    """Streaming int16 interleaved PCM -> 16 kHz mono int16.

    Downmix, anti-alias low-pass (windowed-sinc FIR), then linear-interpolation
    resampling. Filter history and fractional position carry over between
    chunks, so there are no clicks at chunk boundaries.
    """

    def __init__(self, in_rate: int, channels: int, out_rate: int = SDK_RATE, taps: int = 63):
        self.channels = channels
        self.step = in_rate / out_rate  # input samples per output sample
        if in_rate > out_rate:
            cutoff = 0.45 * out_rate / in_rate  # cycles/sample, just under the new Nyquist
            n = np.arange(taps) - (taps - 1) / 2
            h = 2 * cutoff * np.sinc(2 * cutoff * n) * np.hamming(taps)
            self.fir = (h / h.sum()).astype(np.float32)
            self.hist = np.zeros(taps - 1, np.float32)
        else:
            self.fir = None
        self.buf = np.zeros(0, np.float32)
        self.pos = 0.0

    def process(self, data: bytes) -> bytes:
        x = np.frombuffer(data, np.int16).astype(np.float32)
        if self.channels > 1:
            usable = len(x) - len(x) % self.channels
            x = x[:usable].reshape(-1, self.channels).mean(axis=1)
        if self.fir is not None:
            xx = np.concatenate([self.hist, x])
            x = np.convolve(xx, self.fir, mode="valid")
            self.hist = xx[len(xx) - len(self.hist):]
        buf = np.concatenate([self.buf, x])
        last = len(buf) - 1
        if last <= self.pos:
            self.buf = buf
            return b""
        n_out = int(np.ceil((last - self.pos) / self.step))
        p = self.pos + np.arange(n_out) * self.step
        idx = p.astype(np.int64)
        frac = (p - idx).astype(np.float32)
        y = buf[idx] * (1 - frac) + buf[idx + 1] * frac
        nxt = self.pos + n_out * self.step
        drop = int(nxt)
        self.buf = buf[drop:]
        self.pos = nxt - drop
        return np.clip(np.rint(y), -32768, 32767).astype(np.int16).tobytes()


def convert_for_output(pcm: bytes, in_rate: int, out_rate: int, out_channels: int) -> bytes:
    """Mono int16 PCM -> int16 at the output device's rate and channel count."""
    x = np.frombuffer(pcm, np.int16).astype(np.float32)
    if len(x) == 0:
        return b""
    if in_rate != out_rate:
        n_out = int(round(len(x) * out_rate / in_rate))
        x = np.interp(np.linspace(0, len(x) - 1, n_out), np.arange(len(x)), x)
    y = np.clip(np.rint(x), -32768, 32767).astype(np.int16)
    if out_channels > 1:
        y = np.repeat(y, out_channels)
    return y.tobytes()


class AudioCapture:
    """Captures a mic or loopback device and emits 16 kHz mono chunks.

    WASAPI loopback delivers nothing while the render device is silent. A
    silent output stream on the same device keeps it ticking, so the SDK gets
    a continuous real-time stream (otherwise silence gaps vanish and offsets
    drift).
    """

    def __init__(self, pa: "pyaudio.PyAudio"):
        self.pa = pa
        self._in = None
        self._keepalive = None

    def start(self, dev: Device, on_chunk: Callable[[bytes], None],
              render_dev: Optional[Device] = None) -> None:
        resampler = StreamResampler(dev.rate, dev.channels)

        def callback(in_data, frame_count, time_info, status):
            out = resampler.process(in_data)
            if out:
                on_chunk(out)
            return (None, pyaudio.paContinue)

        if dev.kind == "loopback" and render_dev is not None:
            silence = bytes(render_dev.rate // 10 * render_dev.channels * 2)

            def keepalive_cb(in_data, frame_count, time_info, status):
                need = frame_count * render_dev.channels * 2
                return (silence[:need] if need <= len(silence) else bytes(need),
                        pyaudio.paContinue)

        try:
            if dev.kind == "loopback" and render_dev is not None:
                self._keepalive = self.pa.open(
                    format=pyaudio.paInt16, channels=render_dev.channels,
                    rate=render_dev.rate, output=True,
                    output_device_index=render_dev.index,
                    frames_per_buffer=render_dev.rate // 10,
                    stream_callback=keepalive_cb,
                )
            self._in = self.pa.open(
                format=pyaudio.paInt16, channels=dev.channels, rate=dev.rate,
                input=True, input_device_index=dev.index,
                frames_per_buffer=dev.rate // 10, stream_callback=callback,
            )
        except Exception:
            self.stop()  # don't leave the keepalive open if the input failed
            raise

    def stop(self) -> None:
        for s in (self._in, self._keepalive):
            if s is not None:
                try:
                    s.stop_stream()
                    s.close()
                except OSError:
                    pass
        self._in = self._keepalive = None


class AudioPump:
    """Moves captured audio to the current SDK push stream on its own thread.

    - Feedback protection: `is_paused()` is checked when a chunk is *captured*
      (in `feed`), not when the pump thread gets to it, so queueing delay can't
      let TTS audio slip through after the gate reopens. Gated chunks become
      silence, which keeps the recognizer session alive.
    - The sink (push stream) can be swapped during reconnects and direction
      switches. While there is no sink, audio is buffered (up to
      `buffer_s` seconds) and flushed into the next sink, so speech that
      starts while the recognizer is reopening isn't lost.
    - Counts the audio actually sent (that's what Azure bills).
    - Latency: each chunk carries its capture time; every sink gets a
      StreamClock mapping stream position -> capture time (returned by
      set_sink), so a result's audio offset gives the real end-of-speech time
      even for audio that waited in the buffer.
    """

    def __init__(self, is_paused: Callable[[], bool], buffer_s: float = 5.0):
        self._q: "queue.Queue[Optional[tuple]]" = queue.Queue()  # (chunk, captured_at)
        self._sink = None
        self._clock: Optional[StreamClock] = None
        self._lock = threading.Lock()
        self._is_paused = is_paused
        self._pending: "deque[tuple]" = deque()
        self._pending_bytes = 0
        self._max_pending = int(buffer_s * SDK_RATE * 2)
        self.bytes_sent = 0
        self.bytes_dropped = 0
        self._thread: Optional[threading.Thread] = None

    def feed(self, chunk: bytes) -> None:
        """Called from the capture callback: keep it cheap."""
        try:
            gated = self._is_paused()
        except Exception:
            gated = True  # fail closed: never risk feeding our own voice back
        self._q.put((bytes(len(chunk)) if gated else chunk, time.monotonic()))

    def set_sink(self, sink) -> Optional[StreamClock]:
        """Returns the new sink's clock (None when clearing the sink)."""
        with self._lock:
            self._sink = sink
            if sink is None:
                self._clock = None
                return None
            clock = self._clock = StreamClock()
            while self._pending:
                chunk, at = self._pending.popleft()
                self._pending_bytes -= len(chunk)
                if not self._write(chunk, at):
                    break
            self._pending.clear()
            self._pending_bytes = 0
            return clock

    def clear_buffer(self) -> None:
        with self._lock:
            self._pending.clear()
            self._pending_bytes = 0

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="audio-pump", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._q.put(None)
        if self._thread:
            self._thread.join(timeout=2)
        self.clear_buffer()

    @property
    def seconds_sent(self) -> float:
        return self.bytes_sent / (SDK_RATE * 2)

    def _write(self, chunk: bytes, captured_at: float) -> bool:
        """Call with the lock held."""
        try:
            self._sink.write(chunk)
        except Exception:  # stream already closed by a reconnect/stop
            return False
        self.bytes_sent += len(chunk)
        if self._clock is not None:
            self._clock.add(len(chunk), captured_at)
        return True

    def _run(self) -> None:
        while True:
            item = self._q.get()
            if item is None:
                return
            chunk, at = item
            with self._lock:
                if self._sink is not None:
                    self._write(chunk, at)
                    continue
                # Between recognizers: keep the most recent audio.
                self._pending.append(item)
                self._pending_bytes += len(chunk)
                while self._pending_bytes > self._max_pending:
                    old, _ = self._pending.popleft()
                    self._pending_bytes -= len(old)
                    self.bytes_dropped += len(old)


class Player:
    """Plays mono int16 PCM on a chosen output device, interruptibly.

    `play()` and `close()` share a lock so the stream is never closed while a
    write is in flight. Callers set the cancel event first; `play()` then
    returns within one 100 ms block. `close()` aborts the stream (discarding
    anything still buffered) instead of draining it, so nothing is heard
    after Stop.
    """

    def __init__(self, pa: "pyaudio.PyAudio", dev: Device):
        self.dev = dev
        self._lock = threading.Lock()
        self._closed = False
        self._stream = pa.open(
            format=pyaudio.paInt16, channels=dev.channels, rate=dev.rate,
            output=True, output_device_index=dev.index,
        )
        try:  # PortAudio's estimate of write -> sound; used in latency logs
            self.output_latency = float(self._stream.get_output_latency())
        except Exception:
            self.output_latency = 0.0

    def play(self, pcm: bytes, rate: int, cancel: threading.Event) -> bool:
        """Returns True if the whole clip was played."""
        data = convert_for_output(pcm, rate, self.dev.rate, self.dev.channels)
        step = self.dev.rate // 10 * self.dev.channels * 2  # 100 ms
        with self._lock:
            if self._closed:
                return False
            for i in range(0, len(data), step):
                if cancel.is_set():
                    return False
                self._stream.write(data[i:i + step])
            # write() returns when the last block is queued; let it drain.
            return not cancel.wait(0.15)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                # Pa_CloseStream on an active stream aborts it: pending buffers
                # are discarded rather than played out (stop_stream would drain).
                self._stream.close()
            except OSError:
                pass
