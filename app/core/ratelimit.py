"""In-process sliding-window rate limiter for authentication endpoints.

State lives in this process: with several API processes behind a load balancer each
enforces its own limit (still bounded), or put a shared limiter at the proxy.
"""

import threading
import time
from collections import defaultdict, deque

from app.config import get_settings
from app.core.errors import ServiceError


class RateLimited(ServiceError):
    status_code = 429


class SlidingWindowLimiter:
    def __init__(self):
        self._hits: dict[tuple[str, str], deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def hit(self, bucket: str, key: str, limit: int, window: float = 60.0) -> None:
        now = time.monotonic()
        with self._lock:
            q = self._hits[(bucket, key)]
            while q and q[0] <= now - window:
                q.popleft()
            if len(q) >= limit:
                raise RateLimited("Too many attempts. Please wait a minute and try again.")
            q.append(now)
            if len(self._hits) > 50_000:  # bound memory under attack
                self._hits = defaultdict(deque, {k: v for k, v in self._hits.items() if v and v[-1] > now - window})

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


limiter = SlidingWindowLimiter()


def check_auth_rate(bucket: str, ip: str | None) -> None:
    limiter.hit(bucket, ip or "unknown", get_settings().auth_rate_limit_per_minute)
