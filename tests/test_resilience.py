import threading
from types import SimpleNamespace

import httpx
import pytest

from fa.resilience import CircuitOpen, RemotePolicy, remote_policy


def failure(status, headers=None):
    request = httpx.Request("POST", "https://test.invalid/rerank")
    response = httpx.Response(status, request=request, headers=headers)
    return httpx.HTTPStatusError("test failure", request=request, response=response)


def test_transient_failure_retries_once_then_succeeds():
    waits, calls = [], []
    policy = RemotePolicy(sleep=waits.append, jitter=lambda: 0)
    def request():
        calls.append(1)
        if len(calls) == 1:
            raise failure(503)
        return "recovered"
    assert policy.call(request) == "recovered"
    assert len(calls) == 2 and waits == [0.25]
    assert policy.snapshot() == {"state": "closed", "failures": 0}


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_configuration_and_invalid_requests_are_never_retried_or_counted(status):
    policy = RemotePolicy(sleep=lambda _: pytest.fail("must not sleep"))
    for _ in range(4):
        with pytest.raises(httpx.HTTPStatusError):
            policy.call(lambda: (_ for _ in ()).throw(failure(status)))
    assert policy.snapshot() == {"state": "closed", "failures": 0}


def test_exhausted_requests_open_circuit_and_one_probe_recovers():
    now, calls = [0], []
    policy = RemotePolicy(clock=lambda: now[0], sleep=lambda _: None)
    def request():
        calls.append(1)
        raise failure(503)
    for _ in range(3):
        with pytest.raises(httpx.HTTPStatusError):
            policy.call(request)
    for _ in range(10):
        with pytest.raises(CircuitOpen):
            policy.call(request)
    assert len(calls) == 6  # 3 logical requests, at most 2 attempts each
    now[0] = 31
    entered, release = threading.Event(), threading.Event()
    def probe():
        entered.set()
        assert release.wait(3)
        return "OK"
    thread = threading.Thread(target=lambda: policy.call(probe))
    thread.start()
    assert entered.wait(3)
    assert policy.snapshot()["state"] == "half_open"
    with pytest.raises(CircuitOpen):
        policy.call(lambda: pytest.fail("second probe must not execute"))
    release.set()
    thread.join(3)
    assert not thread.is_alive()
    assert policy.call(lambda: "ready") == "ready"


def test_probe_failure_opens_circuit_again_without_retry():
    now, calls = [0], []
    policy = RemotePolicy(threshold=1, clock=lambda: now[0], sleep=lambda _: None)
    def request():
        calls.append(1)
        raise failure(429)
    with pytest.raises(httpx.HTTPStatusError):
        policy.call(request)
    now[0] = 31
    with pytest.raises(httpx.HTTPStatusError):
        policy.call(request)
    assert len(calls) == 3  # first normal call twice, half-open probe once
    with pytest.raises(CircuitOpen):
        policy.call(request)


def test_long_retry_after_defers_instead_of_immediate_retry():
    policy = RemotePolicy(sleep=lambda _: pytest.fail("must not sleep"))
    with pytest.raises(httpx.HTTPStatusError):
        policy.call(lambda: (_ for _ in ()).throw(failure(429, {"Retry-After": "60"})))


def test_failures_are_scoped_to_service_model_and_credential():
    first = remote_policy("test", "https://test.invalid/", "model", "credential-a")
    assert first is remote_policy("test", "https://test.invalid", "model", "credential-a")
    assert first is not remote_policy("test", "https://test.invalid", "model", "credential-b")
    assert first is not remote_policy("test", "https://test.invalid", "other", "credential-a")


def test_real_reranker_http_failures_short_circuit_and_existing_fallback_works(monkeypatch):
    from fa.config import RagSettings
    from fa.retrieval.chunk import Chunk
    from fa.retrieval.hybrid import Hit
    from fa.retrieval.rerank import HttpReranker, rerank
    client = HttpReranker(RagSettings("test", "", "", "https://test.invalid", "test-model"))
    client._policy = RemotePolicy(sleep=lambda _: None)
    calls = []
    def post(*args, **kwargs):
        calls.append(1)
        return httpx.Response(503, request=httpx.Request("POST", "https://test.invalid"))
    monkeypatch.setattr(httpx, "post", post)
    hits = [Hit(Chunk("a.md", "标题", "测试依据", "a"), 2, ("body",)),
            Hit(Chunk("b.md", "标题", "另一依据", "b"), 1, ("body",))]
    for _ in range(8):
        assert rerank("查询", hits, reranker=client) == hits
    assert len(calls) == 6


def test_embedding_uses_one_retry_layer_and_preserves_result_alignment():
    from fa.config import RagSettings
    from fa.retrieval.dense import HttpEmbedder
    embedder = HttpEmbedder(RagSettings("test", "https://test.invalid", "test", "", ""))
    assert embedder._client.max_retries == 0
    embedder._policy = RemotePolicy(sleep=lambda _: None)
    calls = []
    def create(**kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("temporary")
        return SimpleNamespace(data=[SimpleNamespace(index=1, embedding=[2]), SimpleNamespace(index=0, embedding=[1])])
    embedder._client = SimpleNamespace(embeddings=SimpleNamespace(create=create))
    assert embedder.embed(["a", "b"]) == [[1], [2]]
    assert len(calls) == 2
