"""Bounded retries and process-local circuit breakers for read-only RAG APIs.

Never wrap an agent turn or a tool with financial side effects in this policy.
"""
from collections import OrderedDict
import hashlib
import math
import random
import threading
import time


class CircuitOpen(RuntimeError):
    pass


def transient(exc):
    import httpx
    from openai import APIConnectionError, APITimeoutError
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    if status is not None:
        return status in {408, 429} or 500 <= status < 600
    return isinstance(exc, (httpx.TransportError, APIConnectionError, APITimeoutError))


class RemotePolicy:
    def __init__(self, *, retries=1, threshold=3, cooldown=30.0,
                 clock=time.monotonic, sleep=time.sleep, jitter=random.random):
        if (type(retries) is not int or retries < 0 or retries > 3
                or type(threshold) is not int or threshold < 1
                or not math.isfinite(cooldown) or cooldown <= 0):
            raise ValueError("invalid remote policy")
        self.retries, self.threshold, self.cooldown = retries, threshold, cooldown
        self.clock, self.sleep, self.jitter = clock, sleep, jitter
        self._lock = threading.Lock()
        self._failures = 0
        self._opened = None
        self._probe = False
        self._generation = 0

    def _enter(self):
        with self._lock:
            probing = self._opened is not None
            if probing:
                if self._probe or self.clock() - self._opened < self.cooldown:
                    raise CircuitOpen("检索依赖暂时不可用，正在使用备用检索结果")
                self._probe = True
            return self._generation, probing

    def _finish(self, generation, probing, failed):
        with self._lock:
            # An in-flight old success must not close a newly opened circuit.
            if generation != self._generation:
                return
            if not failed:
                self._failures = 0
                if probing:
                    self._opened, self._probe = None, False
                    self._generation += 1
                return
            self._failures += 1
            if probing or self._failures >= self.threshold:
                self._opened, self._probe = self.clock(), False
                self._generation += 1

    def call(self, operation):
        generation, probing = self._enter()
        try:
            for attempt in range(self.retries + 1):
                try:
                    result = operation()
                    break
                except Exception as exc:
                    if not transient(exc) or probing or attempt == self.retries:
                        raise
                    headers = getattr(getattr(exc, "response", None), "headers", {})
                    retry_after = headers.get("retry-after")
                    try:
                        wait = float(retry_after) if retry_after is not None else 0
                    except ValueError:
                        # HTTP-date values are left for a later request.
                        raise exc
                    if not math.isfinite(wait) or wait > 2:
                        raise
                    # Short exponential backoff with jitter; long Retry-After
                    # values are left to the caller's next explicit request.
                    self.sleep(max(wait, min(2.0, 0.25 * 2 ** attempt) + 0.15 * self.jitter()))
        except BaseException as exc:
            # Deterministic 4xx/config errors neither retry nor trip the circuit.
            self._finish(generation, probing, transient(exc))
            raise
        else:
            self._finish(generation, probing, False)
            return result

    def snapshot(self):
        with self._lock:
            state = "closed" if self._opened is None else ("half_open" if self._probe else "open")
            return {"state": state, "failures": self._failures}


_registry = OrderedDict()
_registry_lock = threading.Lock()


def remote_policy(kind, url, model, credential):
    # Scope failures to one service/model/credential; never retain plaintext keys
    # in registry keys or diagnostics. Bounded registry for model reconfiguration.
    key = (kind, url.rstrip("/"), model, hashlib.sha256(credential.encode()).hexdigest())
    with _registry_lock:
        policy = _registry.pop(key, None) or RemotePolicy()
        _registry[key] = policy
        while len(_registry) > 32:
            _registry.popitem(last=False)
        return policy
