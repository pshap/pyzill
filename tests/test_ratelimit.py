import time

import pytest

from pyzill.ratelimit import RateLimiter


def test_disabled_when_interval_zero():
    limiter = RateLimiter(min_interval=0, jitter=0.5)
    start = time.monotonic()
    for _ in range(5):
        limiter.wait()
    assert time.monotonic() - start < 0.1


def test_new_limiter_has_no_previous_slot():
    limiter = RateLimiter(min_interval=1, jitter=0)

    assert limiter._last == float("-inf")


def test_enforces_min_interval():
    limiter = RateLimiter(min_interval=0.2, jitter=0)
    limiter.wait()
    start = time.monotonic()
    limiter.wait()
    elapsed = time.monotonic() - start
    assert elapsed >= 0.18


def test_minimum_delay_is_measured_from_now(monkeypatch):
    """A Retry-After delay must elapse from now, not from the previous request."""
    limiter = RateLimiter(min_interval=0, jitter=0)
    limiter.wait()  # first request establishes the previous slot
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", slept.append)

    limiter.wait(2.0)

    assert slept == [2.0]


def test_minimum_delay_does_not_double_count_interval(monkeypatch):
    """When the interval already covers the delay, no extra sleep is needed."""
    limiter = RateLimiter(min_interval=5, jitter=0)
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", slept.append)
    monkeypatch.setattr(time, "monotonic", lambda: 0.0)
    limiter._last = 0.0  # pretend the previous request started at 0.0

    limiter.wait(1.0)

    assert slept == [5.0]


def test_set_min_interval_zero_disables():
    limiter = RateLimiter(min_interval=5, jitter=0)
    limiter.set_min_interval(0)
    start = time.monotonic()
    limiter.wait()
    limiter.wait()
    assert time.monotonic() - start < 0.1


def test_pyzill_rate_limit_env_precedes_legacy(monkeypatch):
    from pyzill.ratelimit import _read_min_interval

    monkeypatch.setenv("PYZILL_RATE_LIMIT", "2.5")
    monkeypatch.setenv("ZILLOW_MIN_INTERVAL", "9")

    assert _read_min_interval() == 2.5


@pytest.mark.parametrize(("raw", "expected"), [("bad", 1.5), ("-3", 0.0), ("0", 0.0)])
def test_min_interval_environment_parsing(monkeypatch, raw, expected):
    from pyzill.ratelimit import _read_min_interval

    monkeypatch.setenv("PYZILL_RATE_LIMIT", raw)
    monkeypatch.delenv("ZILLOW_MIN_INTERVAL", raising=False)

    assert _read_min_interval() == expected


class TestSharedBudgetLimiter:
    """The shared limiter bounds the whole process; a client may be stricter."""

    def test_local_and_shared_intervals_do_not_stack(self):
        from pyzill.ratelimit import _SharedBudgetLimiter

        shared = RateLimiter(min_interval=0.2, jitter=0)
        limiter = _SharedBudgetLimiter(shared, min_interval=0.2)
        limiter.wait()
        start = time.monotonic()
        limiter.wait()
        elapsed = time.monotonic() - start

        assert 0.18 <= elapsed < 0.35  # one interval, not two

    def test_stricter_local_interval_wins(self):
        from pyzill.ratelimit import _SharedBudgetLimiter

        shared = RateLimiter(min_interval=0.1, jitter=0)
        limiter = _SharedBudgetLimiter(shared, min_interval=0.5)
        limiter.wait()
        start = time.monotonic()
        limiter.wait()
        elapsed = time.monotonic() - start

        assert elapsed >= 0.45

    def test_looser_local_interval_cannot_beat_the_shared_budget(self):
        from pyzill.ratelimit import _SharedBudgetLimiter

        shared = RateLimiter(min_interval=0.5, jitter=0)
        limiter = _SharedBudgetLimiter(shared, min_interval=0.1)
        limiter.wait()
        start = time.monotonic()
        limiter.wait()
        elapsed = time.monotonic() - start

        assert elapsed >= 0.45

    def test_separate_clients_share_one_budget(self):
        from pyzill.ratelimit import _SharedBudgetLimiter

        shared = RateLimiter(min_interval=0.2, jitter=0)
        first = _SharedBudgetLimiter(shared, min_interval=0)
        second = _SharedBudgetLimiter(shared, min_interval=0)
        first.wait()
        start = time.monotonic()
        second.wait()
        elapsed = time.monotonic() - start

        assert elapsed >= 0.18

    def test_retry_after_delay_is_applied_once(self, monkeypatch):
        from pyzill.ratelimit import _SharedBudgetLimiter

        slept: list[float] = []
        monkeypatch.setattr(time, "sleep", slept.append)
        shared = RateLimiter(min_interval=0, jitter=0)
        limiter = _SharedBudgetLimiter(shared, min_interval=0)

        limiter.wait(5.0)

        assert slept == [5.0]
