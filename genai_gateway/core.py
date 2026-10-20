"""Single-process, localhost demo gateway with deterministic failover controls.

This intentionally uses no real model endpoint, credentials, or customer data.
An in-memory store is not durable and does not make this production-ready.
"""
from __future__ import annotations

from collections import defaultdict
import hashlib
import json
import re
import threading
import time
import uuid
from typing import Protocol


class RetryableProviderError(Exception):
    def __init__(self, kind: str = 'temporary_failure', retry_after: float | None = None):
        self.kind = kind
        self.retry_after = retry_after
        super().__init__(kind)


class FatalProviderError(Exception):
    pass


class Provider(Protocol):
    def generate(self, prompt: str) -> str: ...


class MockProvider:
    """Provides fixed demo results without echoing prompts or issuing HTTP calls."""
    def generate(self, prompt: str) -> str:
        if '[SIMULATE_429]' in prompt:
            raise RetryableProviderError('rate_limited', retry_after=0.01)
        if '[SIMULATE_500]' in prompt:
            raise RetryableProviderError('upstream_5xx')
        if '[SIMULATE_400]' in prompt:
            raise FatalProviderError('invalid_demo_request')
        return 'Synthetic GenAI response. No external provider was contacted.'


class RateLimiter:
    def __init__(self, capacity: int = 5, refill_per_second: float = 1):
        if capacity < 1 or refill_per_second <= 0:
            raise ValueError('Invalid limiter settings')
        self.capacity = capacity
        self.refill = refill_per_second
        self.buckets: dict[str, tuple[float,float]] = {}

    def allow(self, client: str, now: float) -> bool:
        tokens, updated = self.buckets.get(client, (float(self.capacity), now))
        tokens = min(float(self.capacity), tokens + max(0, now - updated) * self.refill)
        if tokens < 1:
            self.buckets[client] = (tokens, now)
            return False
        self.buckets[client] = (tokens - 1, now)
        return True


class CircuitBreaker:
    def __init__(self, threshold: int = 2, cooldown: float = 10):
        if threshold < 1 or cooldown < 0:
            raise ValueError('Invalid breaker configuration')
        self.threshold = threshold
        self.cooldown = cooldown
        self.failures = 0
        self.opened_at: float | None = None
        self.state = 'closed'

    def allow(self, now: float) -> bool:
        if self.state == 'open':
            if self.opened_at is not None and now - self.opened_at >= self.cooldown:
                self.state = 'half_open'
                return True
            return False
        return True

    def succeeded(self):
        self.failures = 0
        self.state = 'closed'
        self.opened_at = None

    def failed(self, now: float):
        self.failures += 1
        if self.state == 'half_open' or self.failures >= self.threshold:
            self.state = 'open'
            self.opened_at = now


class Gateway:
    def __init__(self, provider: Provider | None = None, *, capacity: int = 5,
                 refill_per_second: float = 1, max_attempts: int = 3,
                 breaker_threshold: int = 2, breaker_cooldown: float = 10,
                 cache_ttl: float = 60, now=None, sleep=None):
        if not 1 <= max_attempts <= 5 or cache_ttl < 0:
            raise ValueError('Invalid gateway settings')
        self.provider = provider or MockProvider()
        self.limiter = RateLimiter(capacity, refill_per_second)
        self.breaker = CircuitBreaker(breaker_threshold, breaker_cooldown)
        self.max_attempts = max_attempts
        self.cache_ttl = cache_ttl
        self.now = now or time.monotonic
        self.sleep = sleep or time.sleep
        self._cache: dict[str, tuple[str, float, dict]] = {}
        self._metrics: dict[str, int] = defaultdict(int)
        # Keep idempotency check and provider dispatch serialized in this
        # educational single-process implementation; not horizontally scalable.
        self._lock = threading.RLock()

    def metrics(self) -> dict:
        with self._lock:
            return {**{k: self._metrics[k] for k in (
                'requests', 'successes', 'failures', 'rate_rejected', 'circuit_rejected',
                'provider_attempts', 'retries', 'cache_hits')},
                'circuit_state': self.breaker.state}

    def generate(self, prompt: str, *, client: str = 'demo', idempotency_key: str | None = None) -> dict:
        if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 2000:
            return self._error('invalid_prompt', 400)
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,48}', client):
            return self._error('invalid_client', 400)
        if idempotency_key is not None and not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', idempotency_key):
            return self._error('invalid_idempotency_key', 400)
        with self._lock:
            current = self.now()
            self._metrics['requests'] += 1
            key = (client + ':' + idempotency_key) if idempotency_key else None
            prompt_hash = hashlib.sha256(prompt.encode('utf-8')).hexdigest()
            if key is not None and key in self._cache:
                fingerprint, created, result = self._cache[key]
                if current - created < self.cache_ttl:
                    if prompt_hash != fingerprint:
                        self._metrics['failures'] += 1
                        return self._error('idempotency_conflict', 409)
                    self._metrics['cache_hits'] += 1
                    return {**result, 'cached': True}
                del self._cache[key]
            if not self.limiter.allow(client, current):
                self._metrics['rate_rejected'] += 1
                self._metrics['failures'] += 1
                return self._error('rate_limit', 429)
            if not self.breaker.allow(current):
                self._metrics['circuit_rejected'] += 1
                self._metrics['failures'] += 1
                return self._error('circuit_open', 503)
            request_id = 'req_' + uuid.uuid4().hex[:12]
            for attempt in range(1, self.max_attempts + 1):
                self._metrics['provider_attempts'] += 1
                try:
                    result_text = self.provider.generate(prompt)
                except RetryableProviderError as exc:
                    if attempt < self.max_attempts:
                        self._metrics['retries'] += 1
                        # Bound provider Retry-After guidance; deterministic backoff.
                        wait = min(1.0, max(0.0, exc.retry_after)) if exc.retry_after is not None else min(1.0, 0.1 * 2 ** (attempt - 1))
                        self.sleep(wait)
                        continue
                    self.breaker.failed(self.now())
                    self._metrics['failures'] += 1
                    return self._error('provider_unavailable', 503, request_id=request_id, attempts=attempt)
                except FatalProviderError:
                    self._metrics['failures'] += 1
                    return self._error('provider_rejected', 502, request_id=request_id, attempts=attempt)
                except Exception:
                    # Do not reflect provider error text; unknown exceptions
                    # are not retried automatically.
                    self.breaker.failed(self.now())
                    self._metrics['failures'] += 1
                    return self._error('upstream_error', 502, request_id=request_id, attempts=attempt)
                self.breaker.succeeded()
                self._metrics['successes'] += 1
                result = {'ok': True, 'status': 200, 'text': result_text, 'request_id': request_id,
                          'attempts': attempt, 'cached': False, 'model': 'synthetic-mock'}
                if key is not None:
                    if len(self._cache) >= 256:
                        self._cache.pop(next(iter(self._cache)))
                    self._cache[key] = (prompt_hash, self.now(), result)
                return result
            raise AssertionError('unreachable')

    @staticmethod
    def _error(code: str, status: int, **extra) -> dict:
        return {'ok': False, 'status': status, 'error': code, **extra}
