"""三层分类。

这个文件里最要紧的两条：

1. **规则必须排在缓存前面。** 顺序反了的话，一条写错的规则被缓存固化之后就
   再也改不回来了 —— 而且没有任何症状。
2. **降级要计数。** 模型返回了不存在的类目、漏了商户、或者压根没跑，
   都会静默变成「其他」。不计数的话，「一半商户其实是降级的」永远浮不出来。
"""

from datetime import date
from types import SimpleNamespace

import pytest

from fa.categorize import CategoryCache, categorize, match_rule
from fa.models import Transaction, money


def txn(merchant: str, txn_id: str = "T1") -> Transaction:
    return Transaction(
        date=date(2026, 1, 5),
        merchant=merchant,
        amount=money("10.00"),
        account="credit",
        txn_id=txn_id,
    )


class _FakeModel:
    """按剧本回复的假模型。把收到的 prompt 记下来，好断言隐私边界。"""

    def __init__(self, reply: str):
        self.reply = reply
        self.calls: list = []

    def invoke(self, messages):
        self.calls.append(messages)
        return SimpleNamespace(content=self.reply)


def cache_at(tmp_path, entries=None) -> CategoryCache:
    return CategoryCache(tmp_path / "cache.json", entries or {})


# --- 规则层 -------------------------------------------------------------


def test_match_rule_hits_known_merchants():
    assert match_rule("starbucks") == "咖啡"
    assert match_rule("safeway") == "超市"
    assert match_rule("netflix.com") == "订阅"


def test_match_rule_returns_none_for_unknown():
    assert match_rule("某个没见过的店") is None


def test_longest_keyword_wins(monkeypatch):
    """规则表里同时有长短两条时，更长的更具体，应该赢。

    真实的规则表里目前没有这种重叠（`uber eats` 和 `uber trip` 是平级的），
    但一旦有人加了一条宽的进去，取错哪条就是静默的错分类 —— 所以在这里
    用一对人造的规则把它钉住。
    """
    monkeypatch.setattr(
        "fa.categorize.RULES",
        (("uber", "交通"), ("uber eats", "餐饮外卖")),
    )

    assert match_rule("uber eats") == "餐饮外卖"
    assert match_rule("uber trip") == "交通"


# --- 顺序：规则 > 缓存 --------------------------------------------------


def test_rules_beat_the_cache(tmp_path):
    """**这个文件里最重要的一条。**

    缓存里写着 starbucks 是「超市」（可能是一条写错的旧结论），但规则表说它是
    「咖啡」。规则必须赢 —— 否则改规则永远不会生效，错误被缓存永久掩盖，
    而且没有任何症状。
    """
    cache = cache_at(tmp_path, {"starbucks": "超市"})

    tagged, stats = categorize([txn("STARBUCKS #1234")], cache=cache, use_llm=False)

    assert tagged[0].category == "咖啡"
    assert stats.by_rule == 1
    assert stats.by_cache == 0


def test_cache_is_only_written_by_the_llm_layer(tmp_path):
    """缓存不存规则的结论。

    存了的话，改规则就得同时去清缓存 —— 而没人会记得清，于是规则改了也不生效。
    """
    cache = cache_at(tmp_path)
    categorize([txn("STARBUCKS #1234"), txn("某店", "T2")],
               model=_FakeModel('{"某店": "其他"}'), cache=cache)

    assert "starbucks" not in cache.entries
    assert cache.entries["某店"] == "其他"


# --- 缓存层 -------------------------------------------------------------


def test_cache_avoids_the_llm_entirely(tmp_path):
    cache = cache_at(tmp_path, {"某店": "购物"})
    model = _FakeModel('{"某店": "医疗"}')

    tagged, stats = categorize([txn("某店")], model=model, cache=cache)

    assert tagged[0].category == "购物"
    assert stats.by_cache == 1
    assert stats.llm_calls == 0
    assert model.calls == []  # 压根没问


def test_cache_hit_rate_excludes_the_rule_layer(tmp_path):
    """分母是「规则没搞定、真需要查的那部分」。

    把规则命中的算进分母，会把命中率稀释成一个偏低的值，然后你会为了
    「提高命中率」去干些没意义的事。
    """
    cache = cache_at(tmp_path, {"a": "购物"})
    txns = [txn("STARBUCKS #1234"), txn("a", "T2"), txn("b", "T3")]

    _, stats = categorize(txns, model=_FakeModel('{"b": "其他"}'), cache=cache)

    assert stats.by_rule == 1
    assert stats.by_cache == 1
    assert stats.by_llm == 1
    assert stats.cache_hit_rate == 0.5  # 1 / (1 + 1)，那个规则命中的不算


def test_cache_survives_a_round_trip(tmp_path):
    cache = cache_at(tmp_path, {"某店": "购物"})
    cache.save()

    again = CategoryCache.load(tmp_path / "cache.json")

    assert again.get("某店") == "购物"


def test_broken_cache_file_degrades_to_empty(tmp_path):
    """坏掉的缓存不该让整个流程挂掉 —— 它只是个加速器，丢了重问一遍就是。"""
    path = tmp_path / "cache.json"
    path.write_text("{ 这不是 JSON", encoding="utf-8")

    assert CategoryCache.load(path).entries == {}


# --- LLM 层的健壮性 -----------------------------------------------------


def test_parses_json_wrapped_in_a_markdown_fence(tmp_path):
    """模型很爱包一层 ```json。不处理的话整批都会降级成「其他」。"""
    model = _FakeModel('好的：\n```json\n{"某店": "购物"}\n```\n')
    tagged, stats = categorize([txn("某店")], model=model, cache=cache_at(tmp_path))

    assert tagged[0].category == "购物"
    assert stats.fallback == 0


def test_invented_category_falls_back_and_is_counted(tmp_path):
    """模型自己发明一个「伙食费」时不能直接写进缓存 —— 那个类目在
    固定类目表里不存在，会让 per-class 报告多出一行谁也不认识的东西。"""
    model = _FakeModel('{"某店": "伙食费"}')

    tagged, stats = categorize([txn("某店")], model=model, cache=cache_at(tmp_path))

    assert tagged[0].category == "其他"
    assert stats.fallback == 1


def test_missing_merchant_falls_back_and_is_counted(tmp_path):
    """模型漏掉某个商户时也是降级，也要计数。"""
    model = _FakeModel('{"另一个店": "购物"}')

    tagged, stats = categorize([txn("某店")], model=model, cache=cache_at(tmp_path))

    assert tagged[0].category == "其他"
    assert stats.fallback == 1


def test_garbage_reply_degrades_instead_of_crashing(tmp_path):
    model = _FakeModel("我不太确定这些商户该怎么归类。")
    tagged, stats = categorize([txn("某店")], model=model, cache=cache_at(tmp_path))

    assert tagged[0].category == "其他"
    assert stats.fallback == 1


def test_without_a_model_everything_pending_degrades(tmp_path):
    """没模型时（比如查询路径上的 `use_llm=False`）不是崩，是降级。

    查询是交互式的，不该因为分类没跑完就报错 —— 用户会看到一堆「其他」，
    那是诚实的；报错则让他以为整个程序坏了。
    """
    tagged, stats = categorize([txn("某店")], cache=cache_at(tmp_path), use_llm=False)

    assert tagged[0].category == "其他"
    assert stats.by_llm == 1
    assert stats.fallback == 1


# --- 隐私边界 -----------------------------------------------------------


def test_only_the_merchant_string_goes_to_the_model(tmp_path):
    """送进 LLM 的只有归一化后的商户串。

    这条边界还有个好的副作用：模型拿不到金额，就没法用「金额小 = 订阅」这种
    启发式 —— 而那正是 `AMZN Mktp` 那个坑想诱导它犯的错。
    """
    model = _FakeModel('{"某店": "购物"}')
    categorize([txn("某店")], model=model, cache=cache_at(tmp_path))

    sent = str(model.calls[0])
    assert "某店" in sent
    assert "10.00" not in sent  # 金额
    assert "2026-01-05" not in sent  # 日期
    assert "credit" not in sent  # 账户


# --- 返回值 -------------------------------------------------------------


def test_returns_new_transactions_and_leaves_the_input_alone(tmp_path):
    original = [txn("STARBUCKS #1234")]
    tagged, _ = categorize(original, cache=cache_at(tmp_path), use_llm=False)

    assert tagged[0].category == "咖啡"
    assert original[0].category is None  # 原对象没被动过


def test_one_decision_covers_every_transaction_of_a_merchant(tmp_path):
    """按商户归类，不是按交易 —— 同一个商户的几百笔只需要判一次。

    反过来说：一次判错会让这个商户的**所有**交易都错。实测里 `target` 判错
    一次，30 笔交易跟着错。
    """
    txns = [txn("STARBUCKS #1234 SEATTLE WA", f"T{i}") for i in range(5)]

    _, stats = categorize(txns, cache=cache_at(tmp_path), use_llm=False)

    assert stats.merchants == 1
    assert stats.by_rule == 1


def test_different_store_numbers_share_one_decision(tmp_path):
    """归一化的意义：不同门店号还是同一个商户。"""
    txns = [
        txn("SAFEWAY #1234 SEATTLE WA", "T1"),
        txn("SAFEWAY #5678 PORTLAND OR", "T2"),
    ]

    _, stats = categorize(txns, cache=cache_at(tmp_path), use_llm=False)

    assert stats.merchants == 1
