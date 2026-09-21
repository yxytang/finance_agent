"""工具层：把查询/异常包成模型能用的东西。

重点测两件事：

1. **错误必须是字符串，不能抛异常。** 工具的返回值就是模型看到的观察结果，
   抛出去的话模型这一轮直接崩，而不是拿到一句「你这样调是错的」然后改。
2. **安静地返回错答案是这里最危险的失败。** 按类目查一笔都没分类的数据会返回
   0，而 0 看起来和「真的没花这个钱」一模一样。这类情况必须被拦下来。
"""

from datetime import date

import pytest

from fa.models import Transaction, money
from fa.tools.anomaly import build_anomaly_tools
from fa.tools.query import build_query_tools


def txn(day, merchant, amount, category=None, txn_id=None, account="credit"):
    return Transaction(
        date=day,
        merchant=merchant,
        amount=money(amount),
        account=account,
        txn_id=txn_id or f"{day}-{merchant}-{amount}",
        category=category,
    )


UNCATEGORIZED_BILL = (
    txn(date(2026, 1, 5), "STARBUCKS #1234", "5.00"),
    txn(date(2026, 1, 20), "SAFEWAY #9", "120.00"),
    txn(date(2026, 2, 3), "SAFEWAY #9", "100.00"),
    txn(date(2026, 2, 14), "STARBUCKS #1234", "6.00"),
)

CATEGORIZED_BILL = tuple(
    t.with_category("咖啡" if "STARBUCKS" in t.merchant else "超市")
    for t in UNCATEGORIZED_BILL
)


def tool(tools, name):
    return next(t for t in tools if t.name == name)


# --- describe_data ------------------------------------------------------


def test_describe_data_reports_the_facts():
    out = tool(build_query_tools(UNCATEGORIZED_BILL), "describe_data").invoke({})

    assert "2026-01-05" in out and "2026-02-14" in out
    assert "4" in out  # 笔数
    assert "credit" in out
    assert date.today().isoformat() in out


def test_describe_data_warns_when_nothing_is_categorized():
    """分类覆盖率要报出来。

    不报的话，模型按类目查会拿到 0，然后告诉用户「你这个月没喝咖啡」——
    而用户没有任何办法察觉这句话是错的。
    """
    out = tool(build_query_tools(UNCATEGORIZED_BILL), "describe_data").invoke({})
    assert "0 / 4" in out
    assert "还没分类" in out


def test_describe_data_is_quiet_once_categorized():
    """覆盖率是**算出来的**。写死成「还没分类」的话，第四天分类跑完它就变成
    一句谎话，而谎话比没话说更糟 —— 它看起来仍然可信。"""
    out = tool(build_query_tools(CATEGORIZED_BILL), "describe_data").invoke({})
    assert "⚠️" not in out


def test_describe_data_on_an_empty_bill():
    assert "空" in tool(build_query_tools(()), "describe_data").invoke({})


# --- query_transactions -------------------------------------------------


def test_query_renders_the_conditions_and_the_number():
    out = tool(build_query_tools(CATEGORIZED_BILL), "query_transactions").invoke(
        {"date_from": "2026-01-01", "date_to": "2026-01-31", "agg": "sum"}
    )

    assert "2026-01-01" in out and "2026-01-31" in out
    assert "125.00" in out
    assert "2 笔" in out


def test_query_groups_render_as_a_table():
    out = tool(build_query_tools(CATEGORIZED_BILL), "query_transactions").invoke(
        {"group_by": "category"}
    )

    assert "超市" in out and "220.00" in out
    assert "咖啡" in out and "11.00" in out
    # 类目是按金额降序排的，超市应该在咖啡前面
    assert out.index("超市") < out.index("咖啡")


def test_group_by_month_says_it_sorted_by_time():
    """排序规则不同，得说出来 —— 否则模型会以为商户榜也是按时间排的。"""
    out = tool(build_query_tools(CATEGORIZED_BILL), "query_transactions").invoke(
        {"group_by": "month"}
    )
    assert "时间" in out


def test_count_agg_does_not_repeat_the_number():
    """问「点了几次」时，命中笔数就是答案，不该再说一遍「结果：2 笔」。"""
    out = tool(build_query_tools(CATEGORIZED_BILL), "query_transactions").invoke(
        {"agg": "count"}
    )
    assert out.count("4") == 1


def test_count_is_rendered_without_decimals():
    """「4.00 笔」会让读到它的人怀疑整个结果的可靠程度。"""
    out = tool(build_query_tools(CATEGORIZED_BILL), "query_transactions").invoke(
        {"agg": "count", "group_by": "category"}
    )
    assert ".00 笔" not in out


# --- 参数出错时返回字符串，不抛异常 -------------------------------------


def test_unknown_category_returns_a_string_not_an_exception():
    out = tool(build_query_tools(CATEGORIZED_BILL), "query_transactions").invoke(
        {"categories": ["伙食费"]}
    )

    assert isinstance(out, str)
    assert "伙食费" in out
    assert "餐饮外卖" in out  # 得告诉他合法值


def test_unparseable_date_returns_a_string():
    out = tool(build_query_tools(CATEGORIZED_BILL), "query_transactions").invoke(
        {"date_from": "去年三月"}
    )

    assert isinstance(out, str)
    assert "去年三月" in out


def test_reversed_range_returns_a_string():
    out = tool(build_query_tools(CATEGORIZED_BILL), "query_transactions").invoke(
        {"date_from": "2026-03-01", "date_to": "2026-01-01"}
    )
    assert isinstance(out, str)
    assert "还晚" in out


def test_category_filter_on_uncategorized_data_is_refused():
    """**这个文件里最重要的一条。**

    一笔都没分类时按类目查会返回 0 笔，而 0 和「真的没花这个钱」长得一模一样。
    模型会自信地报出来，用户没有任何办法察觉。所以宁可拒绝，也不给一个看起来
    正常的错答案。
    """
    out = tool(build_query_tools(UNCATEGORIZED_BILL), "query_transactions").invoke(
        {"categories": ["咖啡"]}
    )

    assert "还没分类" in out
    assert "merchant_contains" in out  # 得告诉他换条路走


def test_partially_categorized_data_is_queryable():
    """部分分类是正常状态 —— 查出来的数字是真实的下界，可以照常报。
    判据是「一笔都没分类」，不是「有部分没分类」。"""
    partly = (
        txn(date(2026, 1, 5), "X", "5.00", "咖啡"),
        txn(date(2026, 1, 6), "Y", "9.00"),
    )
    out = tool(build_query_tools(partly), "query_transactions").invoke(
        {"categories": ["咖啡"]}
    )
    assert "5.00" in out


# --- find_anomalies -----------------------------------------------------


@pytest.fixture
def anomaly_tools():
    bill = (
        txn(date(2026, 3, 14), "TARGET T-1234", "188.40", txn_id="A"),
        txn(date(2026, 3, 14), "TARGET T-1234", "188.40", txn_id="B"),
    )
    return build_anomaly_tools(bill)


def test_all_runs_every_kind(anomaly_tools):
    out = tool(anomaly_tools, "find_anomalies").invoke({})

    assert "重复扣款" in out
    assert "订阅涨价" in out
    assert "异常大额" in out


def test_single_kind_only_runs_that_one(anomaly_tools):
    out = tool(anomaly_tools, "find_anomalies").invoke({"kind": "duplicates"})

    assert "重复扣款" in out
    assert "订阅涨价" not in out


def test_duplicates_come_with_transaction_ids(anomaly_tools):
    """交易号是用户拿去和银行对质的凭据 —— 没有它这条发现没法行动。"""
    out = tool(anomaly_tools, "find_anomalies").invoke({"kind": "duplicates"})
    assert "A" in out and "B" in out


def test_bad_kind_returns_the_choices(anomaly_tools):
    out = tool(anomaly_tools, "find_anomalies").invoke({"kind": "随便"})
    assert "duplicates" in out


def test_outlier_group_by_category_on_uncategorized_data_explains_itself(anomaly_tools):
    """底层会拒绝，但工具不能把这个拒绝变成异常 —— 要转成一句人话。"""
    out = tool(anomaly_tools, "find_anomalies").invoke(
        {"kind": "outlier", "group_by": "category"}
    )
    assert isinstance(out, str)
    assert "未分类" in out


# --- list_subscriptions -------------------------------------------------


def test_list_subscriptions_totals_the_year():
    bill = tuple(
        txn(date(2026, m, 5), "NETFLIX.COM", "15.49") for m in range(1, 13)
    )
    out = tool(build_anomaly_tools(bill), "list_subscriptions").invoke({})

    assert "NETFLIX.COM" in out
    assert "185.88" in out  # 15.49 × 12
    assert "全年" in out or "每年" in out


def test_list_subscriptions_when_there_are_none():
    bill = (txn(date(2026, 1, 5), "ONE OFF", "9.99"),)
    out = tool(build_anomaly_tools(bill), "list_subscriptions").invoke({})
    assert "没有" in out
