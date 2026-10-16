"""Session lifecycle: explicit states and per-session resource ownership.

States:  IDLE -> STARTING -> RUNNING -> STOPPING -> IDLE
                 STARTING ------------> STOPPING          (stop/fail during start)

Rules:
- `StateMachine.transition()` is atomic and refuses anything not in ALLOWED,
  so a double Start, a double Stop, or a late "started" after Stop are no-ops.
- Each Start creates one `Session`. Everything it opens is registered with
  `own()` *after* it has been acquired, so teardown never races an open in
  progress. If the session was already torn down, `own()` closes the new
  resource at once and raises `Cancelled`.
- Only `Session.stop()` tears down: cancel -> interrupts (silence TTS, abort a
  pending connect) -> wait for the start thread -> close in reverse order.
"""
import enum
import itertools
import logging
import threading
from typing import Callable, List, Optional, Tuple

log = logging.getLogger(__name__)


class State(enum.Enum):
    IDLE = "idle"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"


ALLOWED = {
    State.IDLE: {State.STARTING},
    State.STARTING: {State.RUNNING, State.STOPPING},
    State.RUNNING: {State.STOPPING},
    State.STOPPING: {State.IDLE},
}


class StateMachine:
    def __init__(self):
        self._lock = threading.Lock()
        self._state = State.IDLE

    @property
    def state(self) -> State:
        return self._state

    def transition(self, to: State) -> bool:
        with self._lock:
            if to not in ALLOWED[self._state]:
                log.debug("refused transition %s -> %s", self._state.value, to.value)
                return False
            log.debug("transition %s -> %s", self._state.value, to.value)
            self._state = to
            return True


class Cancelled(Exception):
    """The session was stopped while it was starting."""


class Session:
    _ids = itertools.count(1)
    JOIN_TIMEOUT_S = 10.0  # how long stop() waits for a blocked start thread

    def __init__(self):
        self.id = next(self._ids)
        self.cancelled = threading.Event()
        self._lock = threading.Lock()
        self._stop_lock = threading.Lock()  # a second stop() waits for the first
        self._owned: List[Tuple[str, object, Callable]] = []
        self._interrupts: List[Callable[[], None]] = []
        self._torn_down = False
        self._start_thread: Optional[threading.Thread] = None
        # Handles the UI reads while the session is live (set by the starter).
        self.tts = None
        self.pump = None
        self.controller = None
        self.transcript = None
        self.latency = None

    def run_start(self, target: Callable, *args) -> None:
        self._start_thread = threading.Thread(target=target, args=(self, *args),
                                              name=f"start-{self.id}", daemon=True)
        self._start_thread.start()

    def check(self) -> None:
        if self.cancelled.is_set():
            raise Cancelled()

    def own(self, name: str, obj, close: Callable, interrupt: Optional[Callable] = None):
        """Register an acquired resource; closes it now if Stop has begun.

        `cancelled` is checked under the same lock stop() takes to snapshot the
        interrupts, so a resource is either interrupted+closed by stop() or
        closed here, never missed."""
        with self._lock:
            if not self._torn_down and not self.cancelled.is_set():
                self._owned.append((name, obj, close))
                if interrupt is not None:
                    self._interrupts.append(interrupt)
                return obj
        _safe_close(name, obj, close)
        raise Cancelled()

    def stop(self, join_timeout: Optional[float] = None) -> None:
        """Blocking and idempotent; call from a worker thread (or on exit).
        Concurrent callers all return only once teardown has finished."""
        self.cancelled.set()
        if join_timeout is None:
            join_timeout = self.JOIN_TIMEOUT_S
        if not self._stop_lock.acquire(timeout=join_timeout + 5):
            log.warning("session %d: another stop() is still tearing down", self.id)
            return
        try:
            self._stop_locked(join_timeout)
        finally:
            self._stop_lock.release()

    def _stop_locked(self, join_timeout: float) -> None:
        with self._lock:
            interrupts = list(self._interrupts)
        for fn in interrupts:
            try:
                fn()
            except Exception:
                log.exception("session %d interrupt failed", self.id)
        t = self._start_thread
        if t is not None and t is not threading.current_thread():
            t.join(join_timeout)
            if t.is_alive():
                log.warning("session %d: start thread still busy after %.0fs; "
                            "tearing down anyway (late resources are closed by own())",
                            self.id, join_timeout)
        with self._lock:
            owned, self._owned = self._owned, []
            self._torn_down = True
        for name, obj, close in reversed(owned):
            _safe_close(name, obj, close)

    @property
    def torn_down(self) -> bool:
        return self._torn_down


def _safe_close(name: str, obj, close: Callable) -> None:
    try:
        close(obj)
    except Exception:
        log.exception("closing %s failed", name)
