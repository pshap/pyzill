import os
import random
import threading
import time

DEFAULT_MIN_INTERVAL = 1.5


class RateLimiter:
    """Thread-safe minimum-interval rate limiter with optional jitter.

    Enforces that consecutive calls to :meth:`wait` are spaced at least
    ``min_interval`` seconds apart, plus a random jitter of up to ``jitter``
    seconds.  This is deliberately conservative -- Zillow's PerimeterX will
    return HTTP 403 for requests that arrive too quickly from one IP.
    """

    def __init__(self, min_interval: float = DEFAULT_MIN_INTERVAL, jitter: float = 0.5):
        self.min_interval = min_interval
        self.jitter = jitter
        self._lock = threading.Lock()
        self._last = float("-inf")

    def wait(self) -> None:
        """Block until at least ``min_interval`` (+ jitter) since the last call.

        If ``min_interval`` is 0 or negative, throttling is disabled and this
        returns immediately.
        """
        if self.min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            target = self.min_interval + random.uniform(0, self.jitter)  # noqa: S311 (jitter, not crypto)
            elapsed = now - self._last
            if elapsed < target:
                time.sleep(target - elapsed)
            self._last = time.monotonic()

    def set_min_interval(self, min_interval: float) -> None:
        self.min_interval = max(0.0, min_interval)


def _read_min_interval() -> float:
    raw = os.environ.get("ZILLOW_MIN_INTERVAL")
    if raw is None:
        return DEFAULT_MIN_INTERVAL
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_MIN_INTERVAL


# Shared limiter used by both search and detail requests so that all traffic
# to Zillow is throttled together.
_limiter = RateLimiter(min_interval=_read_min_interval())


def wait_for_slot() -> None:
    """Wait until it is safe to issue the next Zillow request."""
    _limiter.wait()


def set_min_interval(min_interval: float) -> None:
    """Set the global minimum interval (seconds) between Zillow requests.

    Args:
        min_interval: Seconds to wait between requests. Use 0 to disable
            throttling (not recommended against Zillow).
    """
    _limiter.set_min_interval(min_interval)


def get_min_interval() -> float:
    """Return the current global minimum interval in seconds."""
    return _limiter.min_interval
