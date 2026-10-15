import time

from pyzill.ratelimit import RateLimiter


def test_disabled_when_interval_zero():
    limiter = RateLimiter(min_interval=0, jitter=0.5)
    start = time.monotonic()
    for _ in range(5):
        limiter.wait()
    assert time.monotonic() - start < 0.1


def test_enforces_min_interval():
    limiter = RateLimiter(min_interval=0.2, jitter=0)
    limiter.wait()
    start = time.monotonic()
    limiter.wait()
    elapsed = time.monotonic() - start
    assert elapsed >= 0.18


def test_set_min_interval_zero_disables():
    limiter = RateLimiter(min_interval=5, jitter=0)
    limiter.set_min_interval(0)
    start = time.monotonic()
    limiter.wait()
    limiter.wait()
    assert time.monotonic() - start < 0.1
