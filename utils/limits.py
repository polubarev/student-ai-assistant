"""Bound authentication attempts, paid jobs, and instance concurrency."""

from collections import deque
from contextlib import contextmanager
import hashlib
import threading
import time


class LimitExceeded(ValueError):
    """A safe, user-facing limit failure."""


class RateLimiter:
    def __init__(self, limit, window_seconds, *, clock=time.monotonic, max_keys=2048):
        self.limit = limit
        self.window = window_seconds
        self.clock = clock
        self.max_keys = max_keys
        self._events = {}
        self._lock = threading.Lock()

    def consume(self, identity):
        key = hashlib.sha256(str(identity).encode()).digest()
        with self._lock:
            now = self.clock()
            for old_key in list(self._events):
                events = self._events[old_key]
                while events and events[0] <= now - self.window:
                    events.popleft()
                if not events:
                    del self._events[old_key]
            if key not in self._events and len(self._events) >= self.max_keys:
                return False
            events = self._events.setdefault(key, deque())
            if len(events) >= self.limit:
                return False
            events.append(now)
            return True


LOGIN_ATTEMPTS = RateLimiter(5, 300)
LOGIN_TOTAL = RateLimiter(100, 60)
UPLOAD_REQUESTS = RateLimiter(10, 3600)
PAID_JOBS = RateLimiter(10, 3600)
_JOB_SLOT = threading.BoundedSemaphore(1)


@contextmanager
def processing_slot():
    if not _JOB_SLOT.acquire(blocking=False):
        raise LimitExceeded("Сервис занят. Попробуйте через несколько минут.")
    try:
        yield
    finally:
        _JOB_SLOT.release()


@contextmanager
def paid_job(identity):
    with processing_slot():
        if not PAID_JOBS.consume(identity):
            raise LimitExceeded("Лимит обработки достигнут. Попробуйте через час.")
        yield
