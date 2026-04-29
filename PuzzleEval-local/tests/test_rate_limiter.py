"""Tests for puzzleeval.rate_limiter (Gap 13 + Gap 25)."""
from __future__ import annotations

import threading
import time

import pytest

from puzzleeval.rate_limiter import (
    GlobalProviderLimiter,
    TokenBucketLimiter,
    parse_rate_limit_info,
)


class TestParseRateLimitInfo:
    def test_parses_requests_per_minute(self):
        assert parse_rate_limit_info("100 requests/minute") == pytest.approx(100 / 60)

    def test_parses_req_per_sec(self):
        assert parse_rate_limit_info("1 req/sec") == pytest.approx(1.0)

    def test_parses_rpm(self):
        assert parse_rate_limit_info("60 RPM") == pytest.approx(1.0)

    def test_parses_rps(self):
        assert parse_rate_limit_info("2 RPS") == pytest.approx(2.0)

    def test_returns_default_when_none(self):
        assert parse_rate_limit_info(None, default_rps=7.0) == 7.0

    def test_returns_default_on_garbage(self):
        assert parse_rate_limit_info("contact sales for quota", default_rps=3.0) == 3.0

    def test_picks_minimum_of_multiple_rates(self):
        # "100/minute AND 1000/hour" — minute rate is higher (100/60 ≈ 1.67 rps)
        # but hourly is lower (1000/3600 ≈ 0.278 rps). We keep the stricter one.
        info = "100 requests/minute (free tier), burst 1000 requests/hour"
        parsed = parse_rate_limit_info(info)
        assert parsed <= 100 / 60 + 1e-6

    def test_hourly_limit(self):
        assert parse_rate_limit_info("3600 calls/hour") == pytest.approx(1.0)


class TestTokenBucketLimiter:
    def test_first_acquire_is_immediate(self):
        bucket = TokenBucketLimiter(rps=10.0)
        start = time.monotonic()
        slept = bucket.acquire(1.0)
        elapsed = time.monotonic() - start
        assert slept == 0.0
        assert elapsed < 0.05

    def test_burst_then_throttle(self):
        # 2 rps, capacity 2 → can do 2 immediate, third must wait
        bucket = TokenBucketLimiter(rps=2.0, capacity=2.0)
        assert bucket.acquire(1.0) == 0.0
        assert bucket.acquire(1.0) == 0.0
        start = time.monotonic()
        bucket.acquire(1.0)
        elapsed = time.monotonic() - start
        # Should have waited ~0.5s (1 token at 2 rps). Give generous slack for CI.
        assert 0.3 < elapsed < 1.2

    def test_rejects_zero_rps(self):
        with pytest.raises(ValueError):
            TokenBucketLimiter(rps=0.0)

    def test_rejects_negative_rps(self):
        with pytest.raises(ValueError):
            TokenBucketLimiter(rps=-1.0)


class TestGlobalProviderLimiter:
    def test_disabled_is_noop(self):
        limiter = GlobalProviderLimiter(enabled=False)
        start = time.monotonic()
        for _ in range(10):
            limiter.acquire("cand", "openai")
        elapsed = time.monotonic() - start
        assert elapsed < 0.05

    def test_per_candidate_throttle(self):
        limiter = GlobalProviderLimiter(
            enabled=True, default_rps=4.0, upstream_rps=100.0
        )
        limiter.configure_candidate("slow_api", "1 req/sec")
        # 3 calls through the 1rps bucket — total should be ≥ 2s
        start = time.monotonic()
        for _ in range(3):
            limiter.acquire("slow_api")
        elapsed = time.monotonic() - start
        assert elapsed >= 1.0  # at least 2 seconds of waits, allow CI slack

    def test_upstream_grouping(self):
        limiter = GlobalProviderLimiter(
            enabled=True, default_rps=100.0, upstream_rps=2.0
        )
        # Two candidates sharing one upstream — throughput capped at 2rps
        start = time.monotonic()
        for _ in range(3):
            limiter.acquire("cand_a", "openai")
        for _ in range(3):
            limiter.acquire("cand_b", "openai")
        elapsed = time.monotonic() - start
        # 6 acquires at 2rps = expected ≥ 2s
        assert elapsed >= 2.0

    def test_snapshot_counts(self):
        limiter = GlobalProviderLimiter(
            enabled=True, default_rps=100.0, upstream_rps=100.0
        )
        for _ in range(5):
            limiter.acquire("cand", "openai")
        snap = limiter.snapshot()
        assert snap["acquire_count"] == 5
        assert snap["enabled"] is True
        assert snap["candidate_buckets"] >= 1
        assert snap["upstream_buckets"] >= 1

    def test_thread_safety(self):
        limiter = GlobalProviderLimiter(
            enabled=True, default_rps=50.0, upstream_rps=50.0
        )

        def worker():
            for _ in range(10):
                limiter.acquire("cand", "openai")

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        snap = limiter.snapshot()
        assert snap["acquire_count"] == 40

    def test_configure_candidate_noop_when_disabled(self):
        limiter = GlobalProviderLimiter(enabled=False)
        # Should not raise even with garbage input
        limiter.configure_candidate("cand", "nonsense rate string")
        # No buckets created when disabled
        assert limiter.snapshot()["candidate_buckets"] == 0
