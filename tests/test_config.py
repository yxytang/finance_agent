"""配置读取。

这个文件测两件事，都是这次改动里最容易被「修坏」的地方：

1. **向量检索没配不是错误。** 将来读到 `rag_settings()` 返回 None 的人，很自然
   的反应是「这里该报错吧，`build_model()` 就是报错的」—— 然后加一行 raise，
   于是所有没配向量检索的机器都跑不起来了，而它们本来只是没有向量检索而已。
2. **重排配不全要降级，不是报错。** 同上：重排是锦上添花，缺一个变量不该让
   整条检索不可用。
"""

import pytest

from fa.config import (
    DEFAULT_EMBED_BASE_URL,
    DEFAULT_EMBED_MODEL,
    rag_settings,
)

_ENV_NAMES = (
    "RAG_API_KEY",
    "RAG_EMBED_BASE_URL",
    "RAG_EMBED_MODEL",
    "RAG_RERANK_BASE_URL",
    "RAG_RERANK_MODEL",
)


@pytest.fixture(autouse=True)
def _no_rag_env(monkeypatch):
    """每个测试从「什么都没配」开始，不靠运行环境碰巧干净。"""
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def test_no_key_means_not_configured_rather_than_an_error():
    """**没 key 返回 None，不抛。**

    和 `build_model()` 反着来是有意的：那个缺 key 必须报，因为没有模型整个
    agent 一个字都答不出来；而向量检索是加强项，缺了它退回纯 BM25（这个功能
    加进来之前的行为），agent 照常跑。

    做成报错的话，等于让一个可选功能把整个程序拦下来 —— 而且是在用户根本
    没要求过它的场景里（比如只想问一句账单）。
    """
    assert rag_settings() is None


def test_a_key_turns_it_on_with_the_documented_defaults(monkeypatch):
    """填了 key 就有 embedding 的开箱默认值，不用把变量都写全。"""
    monkeypatch.setenv("RAG_API_KEY", "sk-abc")

    settings = rag_settings()

    assert settings is not None
    assert settings.api_key == "sk-abc"
    assert settings.embed_base_url == DEFAULT_EMBED_BASE_URL
    assert settings.embed_model == DEFAULT_EMBED_MODEL


def test_whitespace_around_the_key_is_trimmed(monkeypatch):
    """`.env` 里手滑多打一个空格是很常见的事。

    不 strip 的话，一个末尾带空格的 key 会一路带进 HTTP 头 —— 表现是 401，
    而错误信息里看不到那个空格，排查会往「key 是不是失效了」那条路上去。
    同一个约定在 `build_model()` 里也有。
    """
    monkeypatch.setenv("RAG_API_KEY", "  sk-abc  ")

    assert rag_settings().api_key == "sk-abc"


def test_a_key_of_only_whitespace_counts_as_not_configured(monkeypatch):
    """`.env` 里留一行 `RAG_API_KEY=` 后面跟了空格，等于没填。"""
    monkeypatch.setenv("RAG_API_KEY", "   ")

    assert rag_settings() is None


def test_the_endpoint_and_model_can_be_overridden(monkeypatch):
    """换 provider 只改环境变量，不动代码。"""
    monkeypatch.setenv("RAG_API_KEY", "sk-abc")
    monkeypatch.setenv("RAG_EMBED_BASE_URL", "https://example.com/v1")
    monkeypatch.setenv("RAG_EMBED_MODEL", "some-embedding")

    settings = rag_settings()

    assert settings.embed_base_url == "https://example.com/v1"
    assert settings.embed_model == "some-embedding"


# --- 重排是单独配的 -----------------------------------------------------
#
# embedding 是 OpenAI 兼容形状，重排是百炼自己那套 —— **不是同一个端点**。
# 一个 base_url 罩两件事在某个 provider 上碰巧成立，但那是巧合，不是设计。


def test_rerank_has_a_working_default(monkeypatch):
    """光填 key 就能用重排 —— 端点和模型有实测过的默认值。

    （默认值空着、非填不可的那种写法也合理，但那是因为端点是猜的。这里
    2026-09-23 拿真 key 打过一次确认了形状，所以可以给默认值。）
    """
    monkeypatch.setenv("RAG_API_KEY", "sk-abc")

    settings = rag_settings()

    assert settings.rerank_enabled is True
    assert settings.rerank_base_url.startswith("https://")
    assert settings.rerank_model


def test_blanking_the_rerank_model_turns_it_off(monkeypatch):
    """想关掉重排就把变量置空 —— 不用改代码，也不用删 key。"""
    monkeypatch.setenv("RAG_API_KEY", "sk-abc")
    monkeypatch.setenv("RAG_RERANK_MODEL", "")

    assert rag_settings().rerank_enabled is False


def test_half_configured_rerank_stays_off_rather_than_breaking(monkeypatch):
    """只填了 URL 没填模型（或者反过来）→ 关掉重排，**不是报错**。

    重排是锦上添花。为它缺一个变量就让整条检索不可用，那是把配错一个可选
    变量的代价放大到了搜不到东西。
    """
    monkeypatch.setenv("RAG_API_KEY", "sk-abc")

    monkeypatch.setenv("RAG_RERANK_MODEL", "")
    assert rag_settings().rerank_enabled is False

    monkeypatch.setenv("RAG_RERANK_MODEL", "some-reranker")
    monkeypatch.setenv("RAG_RERANK_BASE_URL", "")
    assert rag_settings().rerank_enabled is False


def test_the_rerank_endpoint_and_model_can_be_overridden(monkeypatch):
    monkeypatch.setenv("RAG_API_KEY", "sk-abc")
    monkeypatch.setenv("RAG_RERANK_BASE_URL", "https://example.com/rerank")
    monkeypatch.setenv("RAG_RERANK_MODEL", "some-reranker")

    settings = rag_settings()

    assert settings.rerank_enabled is True
    assert settings.rerank_base_url == "https://example.com/rerank"
    assert settings.rerank_model == "some-reranker"
