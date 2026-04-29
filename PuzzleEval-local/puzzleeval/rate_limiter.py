"""Rate limiting for Agent 5 test execution.

Gap 13 (HIGH): per-candidate rate limiting so free-tier APIs aren't hammered.
Gap 25 (MEDIUM): cross-candidate rate limiting for shared upstream providers.

Two composable layers:

1. ``TokenBucketLimiter`` — classic token-bucket, one per rate key. Tokens
   refill at ``rps`` per second up to ``capacity`` (= rps by default — a
   1-second burst). ``acquire()`` sleeps until a token is available.

2. ``GlobalProviderLimiter`` — holds one bucket per candidate name (derived
   from ``ScreenedCandidate.rate_limit_info``) AND one bucket per
   ``upstream_provider``. ``acquire(candidate, upstream)`` blocks on BOTH
   relevant buckets so neither the candidate's own quota nor the upstream
   LLM's shared pool is exceeded.

Both are thread-safe — Agent 5 uses ``ThreadPoolExecutor`` across candidates
and calls from multiple threads hit the same GlobalProviderLimiter.

Disabled globally when ``config.RATE_LIMIT_ENABLED`` is False — ``acquire()``
becomes a no-op and tests run at whatever pace the network allows.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field

from puzzleeval import config


# ---------------------------------------------------------------------------
# rate_limit_info parsing
# ---------------------------------------------------------------------------

# Conservative defaults matching the API tier docs we've seen most often.
_KNOWN_UNIT_TO_RPS = {
    "second": 1.0,
    "sec": 1.0,
    "s": 1.0,
    "minute": 1.0 / 60.0,
    "min": 1.0 / 60.0,
    "m": 1.0 / 60.0,
    "hour": 1.0 / 3600.0,
    "hr": 1.0 / 3600.0,
    "h": 1.0 / 3600.0,
    "day": 1.0 / 86400.0,
    "d": 1.0 / 86400.0,
}

_RATE_PATTERNS = [
    # "100 requests/minute", "60 req/sec", "1000 calls/hour"
    re.compile(
        r"(\d+(?:\.\d+)?)\s*(?:requests?|reqs?|calls?|queries?|qps|rpm|rps|tpm)?\s*/\s*"
        r"(second|sec|minute|min|hour|hr|day|d|h|m|s)\b",
        re.IGNORECASE,
    ),
    # "1 req/s" without spaces
    re.compile(
        r"(\d+(?:\.\d+)?)\s*(?:requests?|reqs?|calls?)?\s*per\s+"
        r"(second|minute|hour|day)\b",
        re.IGNORECASE,
    ),
    # "60 RPM" / "1 RPS" / "1000 RPH"
    re.compile(r"(\d+(?:\.\d+)?)\s*(rps|rpm|rph)\b", re.IGNORECASE),
]

_UNIT_ALIASES = {
    "rps": "second",
    "rpm": "minute",
    "rph": "hour",
}


def parse_rate_limit_info(info: str | None, default_rps: float | None = None) -> float:
    """Parse a free-text rate_limit_info string into requests-per-second.

    Examples
    --------
    >>> parse_rate_limit_info("100 requests/minute") == 100 / 60
    True
    >>> parse_rate_limit_info("60 RPM") == 1.0
    True
    >>> parse_rate_limit_info("1 req/sec") == 1.0
    True
    >>> parse_rate_limit_info(None) == config.DEFAULT_RPS
    True

    Picks the *lowest* rate found when multiple are present (conservative).
    Returns ``default_rps`` (or ``config.DEFAULT_RPS``) on unparseable input.
    """
    if default_rps is None:
        default_rps = config.DEFAULT_RPS
    if not info:
        return default_rps

    text = info.strip()
    rates: list[float] = []
    for pattern in _RATE_PATTERNS:
        for match in pattern.finditer(text):
            raw = float(match.group(1))
            unit = match.group(2).lower()
            unit = _UNIT_ALIASES.get(unit, unit)
            per_second = _KNOWN_UNIT_TO_RPS.get(unit)
            if per_second is not None and raw > 0:
                rates.append(raw * per_second)

    if not rates:
        return default_rps
    return min(rates)


# ---------------------------------------------------------------------------
# Token bucket
# ---------------------------------------------------------------------------


@dataclass
class TokenBucketLimiter:
    """Thread-safe token bucket.

    ``rps`` tokens accumulate each second, clamped at ``capacity``. Each
    ``acquire(n=1)`` call blocks until ``n`` tokens are available, then
    withdraws them. Capacity defaults to ``ceil(rps)`` so a cold start
    can burst ~1 second of requests before throttling kicks in.
    """

    rps: float
    capacity: float | None = None
    tokens: float = field(init=False)
    last_refill: float = field(init=False)
    lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def __post_init__(self) -> None:
        if self.rps <= 0:
            raise ValueError(f"rps must be > 0, got {self.rps}")
        if self.capacity is None or self.capacity <= 0:
            # Minimum capacity 1 so extremely slow limits (e.g. 1 req / hour)
            # still permit one token up front instead of deadlocking on refill.
            self.capacity = max(1.0, self.rps)
        self.tokens = self.capacity
        self.last_refill = time.monotonic()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self.last_refill
        if elapsed <= 0:
            return
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rps)
        self.last_refill = now

    def acquire(self, n: float = 1.0) -> float:
        """Block until ``n`` tokens are available, then subtract them.

        Returns the number of seconds slept (0 if none).
        """
        if n <= 0:
            return 0.0
        slept = 0.0
        while True:
            with self.lock:
                self._refill()
                if self.tokens >= n:
                    self.tokens -= n
                    return slept
                needed = n - self.tokens
                # Precise sleep to the moment this bucket will have enough
                sleep_for = needed / self.rps
            # Release lock while sleeping so other threads can hit other buckets
            time.sleep(sleep_for)
            slept += sleep_for


# ---------------------------------------------------------------------------
# Global provider limiter
# ---------------------------------------------------------------------------


class GlobalProviderLimiter:
    """Two-layer limiter: per-candidate + per-upstream.

    Use one instance per Agent 5 run. Call ``acquire(candidate_name,
    upstream_provider)`` immediately before each harness HTTP call.
    """

    def __init__(
        self,
        enabled: bool | None = None,
        default_rps: float | None = None,
        upstream_rps: float | None = None,
    ) -> None:
        self.enabled = config.RATE_LIMIT_ENABLED if enabled is None else enabled
        self.default_rps = config.DEFAULT_RPS if default_rps is None else default_rps
        self.upstream_rps = (
            config.UPSTREAM_RPS if upstream_rps is None else upstream_rps
        )
        # Bucket keys use lowercase for case-insensitive dedup.
        self._candidate_buckets: dict[str, TokenBucketLimiter] = {}
        self._upstream_buckets: dict[str, TokenBucketLimiter] = {}
        self._lock = threading.Lock()
        # Observability counters — surfaced on Agent5Result for the fingerprint.
        self.acquire_count = 0
        self.total_wait_seconds = 0.0

    def configure_candidate(
        self, candidate_name: str, rate_limit_info: str | None
    ) -> None:
        """Pre-register a candidate's bucket from its rate_limit_info.

        Called once per candidate before tests start. If the bucket already
        exists it's replaced (useful on retry). No-op when disabled.
        """
        if not self.enabled or not candidate_name:
            return
        rps = parse_rate_limit_info(rate_limit_info, self.default_rps)
        key = candidate_name.strip().lower()
        with self._lock:
            self._candidate_buckets[key] = TokenBucketLimiter(rps=rps)

    def _get_or_create_candidate_bucket(
        self, candidate_name: str
    ) -> TokenBucketLimiter:
        key = candidate_name.strip().lower()
        with self._lock:
            bucket = self._candidate_buckets.get(key)
            if bucket is None:
                bucket = TokenBucketLimiter(rps=self.default_rps)
                self._candidate_buckets[key] = bucket
            return bucket

    def _get_or_create_upstream_bucket(
        self, upstream: str
    ) -> TokenBucketLimiter:
        key = upstream.strip().lower()
        with self._lock:
            bucket = self._upstream_buckets.get(key)
            if bucket is None:
                bucket = TokenBucketLimiter(rps=self.upstream_rps)
                self._upstream_buckets[key] = bucket
            return bucket

    def acquire(
        self, candidate_name: str, upstream_provider: str | None = None
    ) -> float:
        """Block until both buckets allow one token. Returns total seconds waited."""
        if not self.enabled:
            return 0.0
        waited = 0.0
        if candidate_name:
            waited += self._get_or_create_candidate_bucket(candidate_name).acquire(1.0)
        if upstream_provider:
            waited += self._get_or_create_upstream_bucket(upstream_provider).acquire(1.0)
        with self._lock:
            self.acquire_count += 1
            self.total_wait_seconds += waited
        return waited

    def snapshot(self) -> dict:
        """Observability snapshot for pipeline_summary.json."""
        with self._lock:
            return {
                "enabled": self.enabled,
                "acquire_count": self.acquire_count,
                "total_wait_seconds": round(self.total_wait_seconds, 3),
                "candidate_buckets": len(self._candidate_buckets),
                "upstream_buckets": len(self._upstream_buckets),
            }
