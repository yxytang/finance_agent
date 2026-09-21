"""确定性查询引擎。

这个文件是**整个项目最该被测透的地方** —— 它同时是 pytest 的主战场和第八天
问答评测的 oracle。oracle 本身有 bug 的话，评测分数是在测量错误的基准，
而且不会有任何东西报错。
"""

from datetime import date
from decimal import Decimal

import pytest

from fa.models import UNCATEGORIZED, money
from fa.query import Filters, QueryError, query


def txn(day, merchant, amount, category=None, account="credit", txn_id=None):
    from fa.models import Transaction

    return Transaction(
        date=day,
        merchant=merchant,
        amount=money(amount),
        account=account,
        txn_id=txn_id or f"{day}-{merchant}-{amount}",
        category=category,
    )


@pytest.fixture
def bill():
    """一份手写的小账单。范围刻意排得整齐，好手算。"""
    return [
        txn(date(2026, 1, 5), "STARBUCKS #1234", "5.00", "咖啡"),
        txn(date(2026, 1, 20), "STARBUCKS #1234", "6.00", "咖啡"),
        txn(date(2026, 2, 3), "SAFEWAY #9", "120.00", "超市"),
        txn(date(2026, 2, 14), "TST* SUSHI PALACE", "80.00", "餐饮外卖"),
        txn(date(2026, 2, 28), "SAFEWAY #9", "100.00", "超市"),
        txn(date(2026, 3, 1), "UBER *TRIP", "30.00", "交通", account="checking"),
        txn(date(2026, 3, 15), "AMZN Mktp US", "200.00", "购物"),
        txn(date(2026, 3, 31), "AMZN Mktp US", "-50.00", "购物"),  # 退款
    ]


# --- 筛选 ---------------------------------------------------------------


def test_no_filters_matches_everything(bill):
    result = query(bill)
    assert result.matched == 8
    assert result.value == money("491.00")


def test_date_range_is_inclusive_on_both_ends(bill):
    """「7 月」必须含 7 月 31 号那天的消费。

    闭区间写错成开区间，丢的是**边界那天**的消费 —— 用户不会注意到少了，
    因为他不知道应该有多少。
    """
    result = query(bill, date_from=date(2026, 2, 3), date_to=date(2026, 2, 28))
    assert result.matched == 3
    assert result.value == money("300.00")


def test_date_from_only(bill):
    assert query(bill, date_from=date(2026, 3, 1)).matched == 3


def test_date_to_only(bill):
    assert query(bill, date_to=date(2026, 1, 31)).matched == 2


def test_category_filter(bill):
    result = query(bill, categories=["咖啡"])
    assert result.matched == 2
    assert result.value == money("11.00")


def test_multiple_categories(bill):
    assert query(bill, categories=["咖啡", "超市"]).matched == 4


def test_merchant_match_is_case_insensitive(bill):
    """商户串都是大写的，而人（和模型）会写小写。"""
    assert query(bill, merchant_contains="starbucks").matched == 2
    assert query(bill, merchant_contains="Starbucks").matched == 2


def test_merchant_match_is_a_substring(bill):
    """真实账单的商户串带门店号，用户只会说品牌名。"""
    assert query(bill, merchant_contains="SAFEWAY").matched == 2


def test_account_filter(bill):
    assert query(bill, account="checking").matched == 1


def test_filters_combine_as_and(bill):
    result = query(bill, date_from=date(2026, 2, 1), categories=["超市"])
    assert result.matched == 2
    assert result.value == money("220.00")


def test_empty_result_is_zero_not_an_error(bill):
    """「你这个月没点外卖」是一个**真实且常见**的答案。

    返回错误会让模型以为查询失败然后重试，或者更糟 —— 换个说法再问一遍。
    """
    result = query(bill, categories=["旅行"])
    assert result.matched == 0
    assert result.value == Decimal("0")


def test_uncategorized_transactions_are_addressable(bill):
    """还没分类的交易要能单独查出来 —— 否则「还有多少笔没分类」就没法回答。"""
    rows = bill + [txn(date(2026, 4, 1), "某商户", "9.99")]
    assert query(rows, categories=[UNCATEGORIZED]).matched == 1


# --- 聚合 ---------------------------------------------------------------


@pytest.mark.parametrize(
    "agg,expected",
    [
        ("sum", "491.00"),
        ("count", "8"),
        ("avg", "61.38"),
        ("max", "200.00"),
        ("min", "-50.00"),
    ],
)
def test_aggregations(bill, agg, expected):
    assert query(bill, agg=agg).value == money(expected)


def test_max_sees_the_refund_as_smallest(bill):
    """符号约定错了这里就反了 —— 退款是负的，所以 min 是 -50 而不是 5。"""
    assert query(bill, agg="min").value == money("-50.00")
    assert query(bill, agg="max").value == money("200.00")


def test_matched_and_value_are_different_things(bill):
    """`matched` 永远是笔数，`value` 才是「问的那个数」。

    混起来的话，「外卖花了多少」会答成「3 笔」而没人发现 ——
    因为两者都是合法数字。
    """
    result = query(bill, categories=["咖啡"], agg="sum")
    assert result.matched == 2
    assert result.value == money("11.00")


# --- 分组 ---------------------------------------------------------------


def test_group_by_category_sorts_by_value_desc(bill):
    """排行榜要的是头部 —— 「哪类花得最多」问的就是前几名。"""
    result = query(bill, group_by="category")

    assert [(g.key, g.value) for g in result.groups] == [
        ("超市", money("220.00")),
        ("购物", money("150.00")),
        ("餐饮外卖", money("80.00")),
        ("交通", money("30.00")),
        ("咖啡", money("11.00")),
    ]


def test_group_by_month_sorts_chronologically(bill):
    """时间序列按时间排 —— 倒着排看不出趋势，而趋势正是要看的东西。"""
    assert [g.key for g in query(bill, group_by="month").groups] == [
        "2026-01",
        "2026-02",
        "2026-03",
    ]


def test_group_by_merchant_sorts_by_value_desc(bill):
    keys = [g.key for g in query(bill, group_by="merchant").groups]
    assert keys[0] == "SAFEWAY #9"  # 220
    assert keys[1] == "AMZN Mktp US"  # 150


def test_groups_carry_their_own_count(bill):
    by_key = {g.key: g for g in query(bill, group_by="category").groups}
    assert by_key["超市"].count == 2
    assert by_key["交通"].count == 1


def test_group_count_with_count_agg(bill):
    """「哪家来得最勤」—— agg=count + group_by=merchant。"""
    result = query(bill, agg="count", group_by="merchant")
    assert result.groups[0].key == "STARBUCKS #1234"
    assert result.groups[0].value == Decimal("2")


def test_grouping_does_not_change_the_headline(bill):
    """分组是明细，`value` 永远是「全部命中」的那个数 —— 两者不能串。"""
    flat = query(bill, categories=["超市"])
    grouped = query(bill, categories=["超市"], group_by="month")
    assert flat.value == grouped.value


def test_no_groups_when_not_grouping(bill):
    assert query(bill).groups == ()


# --- 参数校验 -----------------------------------------------------------


def test_unknown_category_is_rejected_not_silently_empty(bill):
    """**最值得单独拦一遍的参数。**

    传一个不存在的类目进去，查询不会报错，只会返回 0 笔 —— 然后模型会自信地
    告诉用户「你这个月没吃饭」。一个错的枚举值安静地返回空，是最难发现的
    一类错误，因为输出看起来完全正常。
    """
    with pytest.raises(QueryError) as exc:
        query(bill, categories=["伙食费"])

    message = str(exc.value)
    assert "伙食费" in message
    assert "餐饮外卖" in message  # 得告诉他合法值有哪些


def test_unknown_agg_is_rejected(bill):
    with pytest.raises(QueryError) as exc:
        query(bill, agg="median")
    assert "avg" in str(exc.value)


def test_unknown_group_by_is_rejected(bill):
    with pytest.raises(QueryError) as exc:
        query(bill, group_by="week")
    assert "month" in str(exc.value)


def test_reversed_date_range_is_rejected(bill):
    with pytest.raises(QueryError):
        query(bill, date_from=date(2026, 3, 1), date_to=date(2026, 1, 1))


# --- 纯函数 -------------------------------------------------------------


def test_input_is_not_mutated(bill):
    before = list(bill)
    query(bill, group_by="category", agg="count")
    assert bill == before


def test_same_arguments_give_the_same_answer(bill):
    """oracle 得可复现，否则第八天的评测分数每次跑都不一样。"""
    a = query(bill, categories=["超市"], group_by="month")
    b = query(bill, categories=["超市"], group_by="month")
    assert a == b


# --- describe：把条件说成人话 -------------------------------------------


def test_describe_spells_out_every_condition(bill):
    """回答里必须复述查询条件，否则用户没法核对一个光秃秃的数字。"""
    filters = query(
        bill,
        date_from=date(2026, 1, 1),
        date_to=date(2026, 1, 31),
        categories=["咖啡"],
        agg="sum",
    ).filters

    text = filters.describe()

    assert "2026-01-01" in text and "2026-01-31" in text
    assert "咖啡" in text
    assert "合计" in text


def test_describe_says_all_time_when_unbounded():
    assert "全部时间" in Filters().describe()


def test_describe_mentions_grouping(bill):
    filters = query(bill, group_by="month").filters
    assert "月份" in filters.describe()
