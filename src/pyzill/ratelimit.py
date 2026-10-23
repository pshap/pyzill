import threading
import time

from pyzill.config import read_env
from pyzill.utils import jitter

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

    def _reserve_locked(
        self,
        now: float,
        minimum_delay: float,
        interval: float | None,
    ) -> tuple[float, float]:
        """Reserve a slot while the caller holds ``_lock``.

        Returns the reserved start time and how long to wait for it, both
        measured on the monotonic clock.
        """
        gap = max(0.0, self.min_interval if interval is None else interval) + jitter(self.jitter)
        target = max(now + max(0.0, minimum_delay), self._last + gap)
        self._last = target
        return target, target - now

    def reserve(
        self,
        minimum_delay: float = 0.0,
        *,
        interval: float | None = None,
        now: float | None = None,
    ) -> tuple[float, float]:
        """Reserve the next request slot without sleeping.

        The lock is held only long enough to move the shared timeline forward;
        the caller sleeps after it is released, so a second thread can reserve
        the following slot instead of waiting for the first one to wake.

        Args:
            minimum_delay: Delay measured from now (a ``Retry-After`` value).
            interval: Override this limiter's interval for this reservation.
            now: The monotonic instant the delay is measured from (default:
                the current instant).

        Returns:
            tuple: the reserved monotonic start time and the seconds to wait.
        """
        with self._lock:
            return self._reserve_locked(time.monotonic() if now is None else now, minimum_delay, interval)

    def mark(self, started: float) -> None:
        """Record an actual start time on the timeline."""
        with self._lock:
            self._last = started

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
        _, delay = self.reserve(minimum_delay=minimum_delay)
        time.sleep(delay)

    def set_min_interval(self, min_interval: float) -> None:
        self.min_interval = max(0.0, min_interval)

    def reset(self, min_interval: float | None = None) -> None:
        """Forget the previous request slot (and optionally set a new interval)."""
        with self._lock:
            self._last = float("-inf")
            if min_interval is not None:
                self.min_interval = max(0.0, min_interval)


class SharedBudgetLimiter(RateLimiter):
    """Throttle against the process-wide request budget and a client interval.

    Every pyzill client in a process shares one request budget for Zillow, so
    running several clients cannot multiply the request rate against the site.
    The shared limiter owns the timeline; this client may additionally ask for
    a larger gap between its own consecutive requests.

    Both limits are satisfied by a *single* sleep: they are compared, never
    added, so the stricter of the two wins instead of the two waits stacking
    up.  Each budget draws its own jitter, so a client's gap stays inside the
    shared limiter's jitter band and its own.
    """

    def __init__(self, shared: RateLimiter, min_interval: float = 0.0) -> None:
        super().__init__(min_interval=min_interval, jitter=shared.jitter)
        self._shared = shared

    def wait(self, minimum_delay: float = 0.0) -> None:
        if self.min_interval <= 0 and minimum_delay <= 0 and self._shared.min_interval <= 0:
            return
        # The shared limiter's reserve method serializes every client in the
        # process, so two clients cannot reserve the same instant. Sleeping
        # happens after the local lock is released: reserving the timeline is
        # what has to be atomic, not the wait for one's own slot.
        with self._lock:
            now = time.monotonic()
            shared_target, _ = self._shared.reserve(
                minimum_delay,
                interval=max(0.0, self._shared.min_interval),
                now=now,
            )
            local_target, _ = self._reserve_locked(now, 0.0, max(0.0, self.min_interval))
            target = max(shared_target, local_target)
            self._last = target
            # The shared timeline has to reflect the slot the request will
            # actually start in, not the earlier local one.
            self._shared.mark(target)
            delay = target - now
        time.sleep(max(0.0, delay))


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

# Compatibility alias: the class used to be private.
_SharedBudgetLimiter = SharedBudgetLimiter


def default_limiter() -> RateLimiter:
    """Return the process-wide request-budget limiter.

    Every client in a process shares this budget unless it opts out with an
    explicit ``rate_limit``, so it is the limiter used whenever no other one
    is supplied.
    """
    return _limiter


def reset_default_limiter(min_interval: float | None = None) -> RateLimiter:
    """Reset the process-wide limiter and return it.

    A long-lived process (or a test) can call this to forget the previous
    request timeline. The object is kept, so clients already holding it see
    the reset rather than a stale budget.
    """
    _limiter.reset(_read_min_interval() if min_interval is None else min_interval)
    return _limiter


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
