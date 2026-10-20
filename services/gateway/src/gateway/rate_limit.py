import time
from collections.abc import Callable, Hashable
from dataclasses import dataclass


@dataclass
class Bucket:
    tokens: float
    updated_at: float


class TokenBucketLimiter:
    """In-memory token buckets keyed by user or API key."""

    def __init__(self, per_minute: int, burst: int, clock: Callable[[], float] = time.monotonic):
        self.per_minute = per_minute
        self.capacity = burst
        self.refill_per_second = per_minute / 60
        self.clock = clock
        self.buckets: dict[Hashable, Bucket] = {}

    def consume(self, key: Hashable) -> float:
        """Consume one token; return zero or the seconds until one token is available."""
        now = self.clock()
        bucket = self.buckets.get(key)
        if bucket is None:
            bucket = Bucket(float(self.capacity), now)
            self.buckets[key] = bucket
        elapsed = max(0.0, now - bucket.updated_at)
        bucket.tokens = min(float(self.capacity), bucket.tokens + elapsed * self.refill_per_second)
        bucket.updated_at = now
        if bucket.tokens >= 1:
            bucket.tokens -= 1
            return 0.0
        return (1 - bucket.tokens) / self.refill_per_second
