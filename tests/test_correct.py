"""纠正分类 —— 用户教的东西里唯一会改变系统行为的那条链路。

它同时写缓存和记忆，所以这个文件要验的就是**两处都写对了**，以及
**在写之前问过用户**。
"""

import pytest

from fa import config
from fa.categorize import CategoryCache
from fa.memory import load as load_memories
from fa.tools.categorize import build_categorize_tools


@pytest.fixture
def paths(tmp_path, monkeypatch):
    """把缓存和记忆都换到临时目录。

    不换的话这个测试会写真实的 `data/category_cache.json` —— 测试改坏生产
    数据，而且事后完全看不出来。
    """
    cache = tmp_path / "cache.json"
    facts = tmp_path / "facts.md"
    cache.write_text('{"target": "超市", "starbucks": "超市"}', encoding="utf-8")
    monkeypatch.setattr(config, "CATEGORY_CACHE", cache)
    monkeypatch.setattr(config, "MEMORY_FILE", facts)
    return cache, facts


def tool(confirm=None):
    return build_categorize_tools(confirm)[0]


def test_writes_both_the_cache_and_the_memory(paths):
    """两处都要写，这不是重复。

    只写缓存，用户下次问「你为什么把 target 算成购物」agent 答不上来；
    只写记忆，得等下一次分类批处理跑完才生效。
    """
    cache_path, memory_path = paths

    out = tool().invoke({"merchant": "target", "category": "购物"})

    assert "已改" in out
    assert CategoryCache.load(cache_path).get("target") == "购物"
    assert any("target" in m.text for m in load_memories(memory_path))


def test_the_memory_records_the_old_value_too(paths):
    """记下「从什么改成什么」，而不是只记新值。

    只记新值的话，下次看到它会以为这本来就是我的偏好，而不是一次纠正 ——
    而这两者的可信度不一样。
    """
    _, memory_path = paths

    tool().invoke({"merchant": "target", "category": "购物"})

    text = load_memories(memory_path)[0].text
    assert "购物" in text and "超市" in text
    assert load_memories(memory_path)[0].kind == "纠正"


def test_asks_before_changing(paths):
    """这个操作会改变以后**所有**查询的结果，所以必须问一句。

    读操作不问（无脑按 y 的确认等于没有确认），但改变行为的事要问。
    """
    seen: list[str] = []

    def confirm(summary: str) -> bool:
        seen.append(summary)
        return True

    tool(confirm).invoke({"merchant": "target", "category": "购物"})

    assert len(seen) == 1
    assert "target" in seen[0]
    assert "购物" in seen[0] and "超市" in seen[0]  # 问的时候要说清改成什么


def test_says_nothing_changed_when_refused(paths):
    """用户拒绝之后必须明说没改，不能默默返回成功。"""
    cache_path, memory_path = paths

    out = tool(lambda summary: False).invoke({"merchant": "target", "category": "购物"})

    assert "拒绝" in out
    assert CategoryCache.load(cache_path).get("target") == "超市"  # 原样
    assert load_memories(memory_path) == []


def test_unknown_merchant_lists_what_can_be_corrected(paths):
    """纠正的前提是这个商户已经被判过类。

    报错里要列出可纠正的商户 —— 用户说的是中文品牌名，缓存里存的是归一化后的
    英文串，不列出来他没法对上。
    """
    out = tool().invoke({"merchant": "星巴克", "category": "咖啡"})

    assert "没法纠正" in out
    assert "target" in out and "starbucks" in out


def test_rule_covered_merchant_says_why_it_cannot_be_corrected(paths):
    """**规则层管的商户进不了缓存，所以纠正不了。**

    规则排在缓存前面，命中了就不会问 LLM，也就永远不会被写进缓存。而
    `correct_category` 只改缓存 —— 对这类商户是**静默无效**的。

    所以错误信息必须说清楚「它归规则管，去改代码」，而不是含糊的「缓存里没有」。
    后者会让用户以为这是 bug，然后反复重试。

    注意这里用的 `netflix` 不在 fixture 的缓存里 —— 那正是真实情况：规则命中的
    商户根本不会进缓存。用 fixture 里已有的商户测这条会走进另一个分支。
    """
    out = tool().invoke({"merchant": "netflix", "category": "娱乐"})  # 规则说它是「订阅」

    assert "规则层" in out
    assert "fa/categorize.py" in out  # 得告诉他去哪改


def test_invalid_category_lists_the_valid_ones(paths):
    out = tool().invoke({"merchant": "target", "category": "伙食费"})

    assert "伙食费" in out
    assert "餐饮外卖" in out
    cache_path, _ = paths
    assert CategoryCache.load(cache_path).get("target") == "超市"  # 没动


def test_correcting_to_the_same_value_is_a_no_op(paths):
    cache_path, memory_path = paths

    out = tool().invoke({"merchant": "target", "category": "超市"})

    assert "本来就是" in out
    assert load_memories(memory_path) == []  # 没写记忆
