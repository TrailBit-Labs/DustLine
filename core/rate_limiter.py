"""Token-bucket rate limiter with asyncio semaphore.

Combines concurrency limiting (semaphore) with throughput limiting
(token bucket) to respect API rate limits precisely.
"""

import asyncio
import time


class RateLimiter:
    """Async-safe token-bucket rate limiter.

    Args:
        tokens_per_second: Sustained request rate ceiling.
        max_concurrent: Maximum in-flight requests.
        burst: Maximum tokens that can accumulate (allows short bursts).
    """

    def __init__(
        self,
        tokens_per_second: float = 8.0,
        max_concurrent: int = 5,
        burst: int = 10,
    ):
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._tokens = float(burst)
        self._max_tokens = float(burst)
        self._rate = tokens_per_second
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Wait until both a concurrency slot and a rate token are available."""
        await self._semaphore.acquire()
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_refill
            self._tokens = min(
                self._max_tokens,
                self._tokens + elapsed * self._rate,
            )
            self._last_refill = now

            if self._tokens < 1.0:
                wait_time = (1.0 - self._tokens) / self._rate
                await asyncio.sleep(wait_time)
                self._tokens = 0.0
            else:
                self._tokens -= 1.0

    def release(self) -> None:
        self._semaphore.release()

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, *exc):
        self.release()


# Pre-configured limiters for each API provider
MEMPOOL_LIMITER = RateLimiter(tokens_per_second=8.0, max_concurrent=5, burst=10)
BLOCKSTREAM_LIMITER = RateLimiter(tokens_per_second=8.0, max_concurrent=5, burst=10)
WALLETEXPLORER_LIMITER = RateLimiter(tokens_per_second=0.8, max_concurrent=1, burst=2)
ARKHAM_LIMITER = RateLimiter(tokens_per_second=5.0, max_concurrent=3, burst=5)
