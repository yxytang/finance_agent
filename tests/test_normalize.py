"""商户串归一化 —— 三层分类的地基。

这个文件的重点全在**误删**上。漏删只是多花一次 LLM 调用，可恢复；
误删会让两个不同商户共用一个缓存 key，然后**永久性地**互相污染。
"""

import pytest

from fa.normalize import normalize


@pytest.mark.parametrize(
    "raw,expected",
    [
        # 门店号
        ("CHIPOTLE 1234", "chipotle"),
        ("PEETS #88", "peets"),
        ("BEST BUY #245", "best buy"),
        ("TARGET T-1234", "target"),
        # 随机码
        ("SPOTIFY P0A1B2C3", "spotify"),
        ("SHELL OIL 57440", "shell oil"),
        ("AMZN Mktp US*2H4KJ", "amzn mktp us"),
        ("DELTA AIR LINES 0062", "delta air lines"),
        # 城市 + 州
        ("STARBUCKS #1234 SEATTLE WA", "starbucks"),
        ("SAFEWAY #1234 SEATTLE WA", "safeway"),
        # 纯字母的原样保留
        ("NETFLIX.COM", "netflix.com"),
        ("TICKETMASTER", "ticketmaster"),
        ("PACIFIC PROPERTY MGMT RENT", "pacific property mgmt rent"),
    ],
)
def test_normalizes_real_bank_strings(raw, expected):
    assert normalize(raw) == expected


def test_same_merchant_with_different_codes_collapses():
    """同一个商户的随机码不同 —— 归一化之后必须落成同一个 key。

    不合并的话缓存永远命中不了，每笔都要问 LLM。
    """
    assert normalize("AMZN Mktp US*2H4KJ") == normalize("AMZN Mktp US*9X2LP")


def test_star_becomes_a_space_not_a_deletion():
    """**这个文件里最要紧的一条。**

    `UBER *EATS` 和 `UBER *TRIP` 是两回事（外卖 vs 打车，两个不同类目）。
    如果把 `*` 后面的内容当随机码删掉，两个都变成 `uber`，缓存会把它们
    混成一个类目 —— 而且从此永远错下去，因为缓存命中了。
    """
    assert normalize("UBER *EATS") == "uber eats"
    assert normalize("UBER *TRIP") == "uber trip"
    assert normalize("UBER *EATS") != normalize("UBER *TRIP")


def test_only_uppercase_state_codes_are_stripped():
    """只认**大写**的两字母州码。

    银行格式里州名就是大写写的，所以这个限制能挡掉一部分误判。但它挡不住
    全部 —— 见下面那条。
    """
    assert normalize("SOMETHING SEATTLE WA") == "something"


def test_the_state_rule_is_a_heuristic_and_does_overstrip():
    """**这条测试记录的是一个已知的过度删减，不是在庆祝它。**

    `FEDEX OFFICE IN` 里的 `IN` 跟印第安纳州没关系，但它长得和州码一模一样，
    没有任何句法手段能把两者分开。

    为什么还是留着这条规则：银行商户串基本都是「商户名 城市 州」的格式，
    收益（缓存 key 干净、少送一个城市给 LLM）大于代价。而且代价是**缓存 key
    变粗**，不是算错钱 —— 真出问题时用户可以手工改缓存，错误不会扩散。
    """
    assert normalize("FEDEX OFFICE IN") == "fedex"


def test_stripping_never_empties_the_key():
    """全砍光说明这不是「城市+州」，是个恰好以州码结尾的商户名。"""
    assert normalize("RENT WA") == "rent wa"


def test_all_codes_falls_back_to_the_original():
    """整串都是编号时回退到小写原串。

    宁可留一个丑 key，也不能让一堆不同的商户共用空字符串这个 key ——
    那会让它们全部拿到同一个类目。
    """
    assert normalize("#1234") == "#1234"
    assert normalize("1234") == "1234"


def test_is_case_insensitive():
    assert normalize("Starbucks") == normalize("STARBUCKS") == "starbucks"


def test_idempotent():
    """归一化两次和一次结果一样。它会被反复调用，不该越用越短。"""
    for raw in ("AMZN Mktp US*2H4KJ", "STARBUCKS #1234 SEATTLE WA", "UBER *EATS"):
        once = normalize(raw)
        assert normalize(once) == once
