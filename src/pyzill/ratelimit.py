import random
import threading
import time

from pyzill.config import read_env

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

    def wait(self, minimum_delay: float = 0.0) -> None:
        """Block until the next request may start.

        Two constraints are combined:

        * the minimum interval (+ jitter) measured from the previous request's
          start, and
        * ``minimum_delay`` seconds measured **from now** (used for a
          ``Retry-After`` delay, which the server expresses relative to the
          response that carried it, not to the previous request).

        A zero interval only disables the rate-limit component.
        """
        if self.min_interval <= 0 and minimum_delay <= 0:
            return
        with self._lock:
            now = time.monotonic()
            interval = max(0.0, self.min_interval)
            interval_target = max(0.0, interval + random.uniform(0, self.jitter))  # noqa: S311 (not crypto)
            interval_wait = max(0.0, interval_target - (now - self._last))
            time.sleep(max(interval_wait, minimum_delay, 0.0))
            self._last = time.monotonic()

    def set_min_interval(self, min_interval: float) -> None:
        self.min_interval = max(0.0, min_interval)


def _read_min_interval() -> float:
    raw = read_env("PYZILL_RATE_LIMIT")
    if raw is None:
        return DEFAULT_MIN_INTERVAL
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_MIN_INTERVAL


# Shared limiter used by both search and detail requests so that all traffic
# to Zillow is throttled together.
_limiter = RateLimiter(min_interval=_read_min_interval())


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
