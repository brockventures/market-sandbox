"""
agora.ticker - Background round-advance ticker with inactivity watchdog.

Decouples game time progression from Discord turns (Issue #61): a daemon
thread periodically calls AgoraReferee.step_round() on a fixed interval,
independent of any HTTP request arriving. An inactivity watchdog auto-pauses
the ticker after N consecutive quiet rounds (no order/fill/transit activity,
detected via referee.current_seq not advancing) to avoid burning Railway
compute/budget on an idle table.
"""

import threading
import time
import uuid
from typing import Any, Dict, Optional


DEFAULT_TICK_INTERVAL_SEC = 60.0
DEFAULT_INACTIVITY_ROUNDS = 5  # consecutive quiet rounds before auto-pause
MAX_BURST_ROUNDS = 50
MAX_BURST_INTERVAL_SEC = 600.0


class TickerEngine:
    """
    Owns a single background thread that calls referee.step_round() on a
    fixed cadence. Thread-safe start/stop/pause/resume; status is a cheap
    snapshot read under a lock, safe to poll from an HTTP handler thread.
    """

    def __init__(
        self,
        referee,
        interval_sec: float = DEFAULT_TICK_INTERVAL_SEC,
        inactivity_rounds: int = DEFAULT_INACTIVITY_ROUNDS,
        on_tick: Optional[Any] = None,
    ):
        self.referee = referee
        self.interval_sec = max(1.0, float(interval_sec))
        self.inactivity_rounds = max(1, int(inactivity_rounds))
        self.on_tick = on_tick  # optional callback(round_result: dict) for broadcast hooks

        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._running = False
        self._paused = False
        self._pause_reason: Optional[str] = None
        self._quiet_round_count = 0
        self._last_seq: Optional[int] = None
        self._next_tick_at: Optional[float] = None
        self._last_round_result: Optional[Dict[str, Any]] = None

        # Burst-run state (Issue #62): a discrete N-round human-triggered run,
        # independent of (and mutually exclusive with) the continuous ticker.
        self._burst_lock = threading.Lock()
        self._burst_thread: Optional[threading.Thread] = None
        self._burst_stop_event = threading.Event()
        self._burst_active = False
        self._burst_id: Optional[str] = None
        self._burst_rounds_total = 0
        self._burst_rounds_remaining = 0
        self._burst_resume_ticker_after = False

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
            self._paused = False
            self._pause_reason = None
            self._quiet_round_count = 0
            self._last_seq = getattr(self.referee, "current_seq", 0)
            self._stop_event.clear()
            self._next_tick_at = time.time() + self.interval_sec
        self._thread = threading.Thread(target=self._run_loop, name="agora-ticker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        with self._lock:
            self._running = False
            self._next_tick_at = None

    def pause(self, reason: str = "manual") -> None:
        with self._lock:
            self._paused = True
            self._pause_reason = reason

    def resume(self) -> None:
        with self._lock:
            self._paused = False
            self._pause_reason = None
            self._quiet_round_count = 0
            self._last_seq = getattr(self.referee, "current_seq", 0)
            self._next_tick_at = time.time() + self.interval_sec

    def status(self) -> Dict[str, Any]:
        with self._lock:
            base = {
                "running": self._running,
                "paused": self._paused,
                "pause_reason": self._pause_reason,
                "interval_sec": self.interval_sec,
                "inactivity_rounds": self.inactivity_rounds,
                "quiet_round_count": self._quiet_round_count,
                "current_round": getattr(self.referee, "current_round", None),
                "next_tick_eta_sec": (
                    max(0.0, round(self._next_tick_at - time.time(), 1))
                    if self._next_tick_at and not self._paused
                    else None
                ),
            }
        with self._burst_lock:
            base.update({
                "burst_active": self._burst_active,
                "burst_id": self._burst_id,
                "rounds_remaining": self._burst_rounds_remaining,
                "burst_rounds_total": self._burst_rounds_total,
            })
        return base

    # -- Burst run (Issue #62) -------------------------------------------

    def start_burst(self, rounds: int, interval_sec: float = 30.0) -> Dict[str, Any]:
        """
        Start a discrete N-round burst run. Pauses the continuous ticker for
        the duration (resuming it afterward iff it was running when the burst
        started) so the two never advance rounds concurrently. Raises
        ValueError on invalid input or if a burst is already in flight.
        """
        rounds = int(rounds)
        interval_sec = float(interval_sec)
        if rounds < 1 or rounds > MAX_BURST_ROUNDS:
            raise ValueError(f"rounds must be between 1 and {MAX_BURST_ROUNDS}")
        if interval_sec <= 0 or interval_sec > MAX_BURST_INTERVAL_SEC:
            raise ValueError(f"interval_sec must be between 0 and {MAX_BURST_INTERVAL_SEC}")

        with self._burst_lock:
            if self._burst_active:
                raise ValueError(f"burst {self._burst_id} already in progress")
            burst_id = f"burst-{int(time.time())}-{uuid.uuid4().hex[:6]}"
            self._burst_active = True
            self._burst_id = burst_id
            self._burst_rounds_total = rounds
            self._burst_rounds_remaining = rounds
            self._burst_stop_event.clear()

        with self._lock:
            was_running_unpaused = self._running and not self._paused
            self._burst_resume_ticker_after = was_running_unpaused
        self.pause(reason=f"burst:{burst_id}")

        seq = self.referee.record_burst_event("start", {
            "burst_id": burst_id, "rounds": rounds, "interval_sec": interval_sec,
        })

        self._burst_thread = threading.Thread(
            target=self._run_burst_loop, args=(burst_id, rounds, interval_sec), name="agora-burst", daemon=True
        )
        self._burst_thread.start()
        return {"burst_id": burst_id, "rounds": rounds, "interval_sec": interval_sec, "start_seq": seq}

    def cancel_burst(self) -> bool:
        """Cancel an in-flight burst early. Returns False if none was active."""
        with self._burst_lock:
            if not self._burst_active:
                return False
        self._burst_stop_event.set()
        return True

    def _run_burst_loop(self, burst_id: str, rounds: int, interval_sec: float) -> None:
        for i in range(rounds):
            if self._burst_stop_event.wait(interval_sec):
                break  # cancel_burst() was called
            try:
                self.referee.step_round()
            except Exception as exc:
                self.referee.record_burst_event("tick", {
                    "burst_id": burst_id, "round_index": i + 1, "error": str(exc),
                })
                continue
            with self._burst_lock:
                self._burst_rounds_remaining = max(0, rounds - (i + 1))
                remaining = self._burst_rounds_remaining
            self.referee.record_burst_event("tick", {
                "burst_id": burst_id, "round_index": i + 1, "rounds_remaining": remaining,
            })

        with self._burst_lock:
            self._burst_active = False
            self._burst_id = None
            self._burst_rounds_remaining = 0
        self.referee.record_burst_event("conclude", {"burst_id": burst_id})

        with self._lock:
            resume_after = self._burst_resume_ticker_after
            self._burst_resume_ticker_after = False
        if resume_after:
            self.resume()

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            woke = self._stop_event.wait(self.interval_sec)
            if woke:
                break  # stop() was called

            with self._lock:
                paused = self._paused
                self._next_tick_at = time.time() + self.interval_sec

            if paused:
                continue

            try:
                result = self.referee.step_round()
            except Exception as exc:  # never let a bad round kill the ticker thread
                with self._lock:
                    self._last_round_result = {"status": "error", "error": str(exc)}
                continue

            with self._lock:
                self._last_round_result = result
                new_seq = getattr(self.referee, "current_seq", self._last_seq)
                if new_seq == self._last_seq:
                    self._quiet_round_count += 1
                else:
                    self._quiet_round_count = 0
                self._last_seq = new_seq

                if self._quiet_round_count >= self.inactivity_rounds:
                    self._paused = True
                    self._pause_reason = (
                        f"inactivity_watchdog: {self._quiet_round_count} consecutive "
                        f"quiet rounds (no order/fill/transit activity)"
                    )

            if self.on_tick:
                try:
                    self.on_tick(result)
                except Exception:
                    pass  # broadcast/telemetry hooks must never break the ticker
