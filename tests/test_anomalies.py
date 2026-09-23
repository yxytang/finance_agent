"""异常与订阅检测。

这个文件的重点是**误报**，不是漏报。一个检测器漏掉一笔，用户损失一次提醒；
误报十笔，用户会学会无视整个功能 —— 而那之后真正的那一笔也一起被无视了。
所以每个检测器都配了「不该报的别报」的用例。
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from data.generate import build
from fa.anomalies import (
    find_duplicates,
    find_outliers,
    find_price_increases,
    find_subscriptions,
)
from fa.models import Transaction, money


def txn(day, merchant, amount, category=None, txn_id=None):
    return Transaction(
        date=day,
        merchant=merchant,
        amount=money(amount),
        account="credit",
        txn_id=txn_id or f"{day}-{merchant}-{amount}",
        category=category,
    )


def monthly(merchant, day, amount, months=6, start=(2026, 1), **kwargs):
    """从 start 那个月起，每月 day 号扣一笔。"""
    year, month = start
    rows = []
    for i in range(months):
        shifted = month - 1 + i
        rows.append(
            txn(
                date(year + shifted // 12, shifted % 12 + 1, day),
                merchant,
                amount,
                **kwargs,
            )
        )
    return rows


@pytest.fixture(scope="module")
def categorized():
    """真实账单 + 真值类目。

    异常检测的按类目分组需要类目，而 categories 本来要第四天才有 ——
    这里直接拿 reference 当答案贴上，测的是检测器不是分类器。

    **结束日钉死在 2026-08-31。** 账单默认跟着今天走（见 data/generate.py），
    而下面几条断言写的是**具体数字**：READLY 扣了 12 次、年化 119.88。不钉的话
    这份 fixture 会随运行日期漂移 —— READLY 锚在每月 23 号，今天一过 23 号就多
    一次扣款，`occurrences == 12` 当场变 13，而 23 号之前它又是绿的。

    **一个只在每月后半月才红的测试，比一个一直红的更糟**：它会在某天毫无征兆地
    出现、改完又「自己好了」。它 2026-09-23 就是这么红的（前一天还是绿的）。

    钉住的是**输入数据**，不是被测的行为；要测「账单不完整时检测器怎么办」，
    用 `end=` 单造一份，别动这份。
    """
    transactions, reference = build(end=date(2026, 8, 31))
    return [t.with_category(reference[t.txn_id]) for t in transactions]


# --- 重复扣款 -----------------------------------------------------------


def test_detects_a_double_charge():
    rows = [
        txn(date(2026, 3, 14), "TARGET T-1234", "188.40", txn_id="A"),
        txn(date(2026, 3, 14), "TARGET T-1234", "188.40", txn_id="B"),
    ]

    (dup,) = find_duplicates(rows)

    assert dup.merchant == "TARGET T-1234"
    assert dup.amount == money("188.40")
    assert dup.times == 2
    assert set(dup.txn_ids) == {"A", "B"}


def test_monthly_subscription_is_not_a_duplicate():
    """每月扣同一个数天经地义 —— 时间窗够短就不会误报。"""
    assert find_duplicates(monthly("NETFLIX.COM", 5, "15.49", months=12)) == []


def test_same_merchant_same_day_but_different_amount_is_not_a_duplicate():
    """同一天在同一家买两次不同金额，是正常消费。"""
    rows = [
        txn(date(2026, 3, 14), "SAFEWAY", "42.10", txn_id="A"),
        txn(date(2026, 3, 14), "SAFEWAY", "12.80", txn_id="B"),
    ]
    assert find_duplicates(rows) == []


def test_duplicate_outside_the_window_is_ignored():
    rows = [
        txn(date(2026, 3, 1), "X", "10.00", txn_id="A"),
        txn(date(2026, 3, 20), "X", "10.00", txn_id="B"),
    ]

    assert find_duplicates(rows) == []
    assert len(find_duplicates(rows, window_days=30)) == 1


# --- 涨价 ---------------------------------------------------------------


def test_detects_a_price_increase():
    rows = monthly("NETFLIX.COM", 5, "15.49", months=6) + monthly(
        "NETFLIX.COM", 5, "19.36", months=6, start=(2026, 7)
    )

    (increase,) = find_price_increases(rows)

    assert increase.was == money("15.49")
    assert increase.now == money("19.36")
    assert increase.since == date(2026, 7, 5)  # 第一次按新价扣费
    assert increase.occurrences == 12


def test_a_price_drop_is_not_an_increase():
    rows = monthly("SPOTIFY", 5, "11.99", months=6) + monthly(
        "SPOTIFY", 5, "9.99", months=6, start=(2026, 7)
    )
    assert find_price_increases(rows) == []


def test_varying_amounts_do_not_trigger_a_price_increase():
    """**你最近吃得贵了，不是你订的东西涨价了。**

    没有「金额固定」这道闸，一家去了 6 次的餐厅会在「前三次均价 vs 后三次
    均价」上触发涨价告警。那是消费行为变化，不是订阅涨价 —— 报出来就是噪音。
    """
    rows = [
        txn(date(2026, 1, 5), "SUSHI PALACE", "40.00"),
        txn(date(2026, 2, 5), "SUSHI PALACE", "55.00"),
        txn(date(2026, 3, 5), "SUSHI PALACE", "38.00"),
        txn(date(2026, 4, 5), "SUSHI PALACE", "90.00"),
        txn(date(2026, 5, 5), "SUSHI PALACE", "88.00"),
        txn(date(2026, 6, 5), "SUSHI PALACE", "95.00"),
    ]
    assert find_price_increases(rows) == []


def test_a_small_rise_is_ignored():
    """5% 以下多半是税费或汇率波动，报了只是噪音。"""
    rows = monthly("X", 5, "10.00", months=6) + monthly(
        "X", 5, "10.30", months=6, start=(2026, 7)
    )
    assert find_price_increases(rows) == []
    assert len(find_price_increases(rows, min_rise=Decimal("0.01"))) == 1


# --- 订阅清单 -----------------------------------------------------------


def test_monthly_fixed_price_is_a_subscription():
    (sub,) = find_subscriptions(monthly("NETFLIX.COM", 5, "15.49", months=6))

    assert sub.amount == money("15.49")
    assert sub.occurrences == 6
    assert sub.first_seen == date(2026, 1, 5)
    assert sub.last_seen == date(2026, 6, 5)


def test_same_amount_at_random_intervals_is_not_a_subscription():
    """**一条 5 块钱的咖啡，一年里恰好买到 4 次同样金额，长得和订阅一模一样。**

    只卡「金额固定」会把它误报成订阅。加上「间隔规律」才分得开 —— 订阅是按月
    扣的，随机消费不是。这条用例就是那个间隔检查存在的全部理由。
    """
    rows = [
        txn(date(2026, 1, 3), "PEETS #88", "5.00"),
        txn(date(2026, 1, 9), "PEETS #88", "5.00"),
        txn(date(2026, 5, 2), "PEETS #88", "5.00"),
        txn(date(2026, 11, 20), "PEETS #88", "5.00"),
    ]
    assert find_subscriptions(rows) == []


def test_a_daily_shop_is_not_a_subscription():
    """每天都去、每次都是 3 块 —— 金额和间隔都「固定」，但不是订阅。"""
    rows = [
        txn(date(2026, 1, 1) + timedelta(days=i), "KIOSK", "3.00") for i in range(10)
    ]
    assert find_subscriptions(rows) == []


def test_three_occurrences_is_too_few():
    """三次说明不了规律，可能只是巧合。"""
    assert find_subscriptions(monthly("X", 5, "10.00", months=3)) == []


def test_yearly_is_an_annualization_not_a_sum():
    """月 10 块的订阅年化该是**精确的** 120，而不是「实际发生的 60」。

    半年数据也要折算成整年，但折算方式得对。第一版按「跨度 / 间隔数」算平均
    天数，1~6 月这个窗口的平均月长是 30.2 天（冬季月份长），于是 10.00 被算成
    10.08 —— 每笔都差 0.8%，方向固定，不会互相抵消。按日历月计数才对。
    """
    (sub,) = find_subscriptions(monthly("X", 5, "10.00", months=6))

    assert sub.monthly == money("10.00")
    assert sub.yearly == money("120.00")


# --- 异常大额 -----------------------------------------------------------


def test_outliers_are_relative_to_their_group():
    """「3800 的笔记本」在购物类里正常，在咖啡类里就是数据错误。

    异常是相对的 —— 没有组就没有基准。
    """
    rows = (
        [txn(date(2026, 1, i + 1), "COFFEE", "5.00", "咖啡") for i in range(9)]
        + [txn(date(2026, 2, 1), "COFFEE", "3800.00", "咖啡")]
        + [txn(date(2026, 3, i + 1), "BEST BUY", "200.00", "购物") for i in range(9)]
    )

    found = find_outliers(rows, group_by="category")

    assert [o.amount for o in found] == [money("3800.00")]
    assert found[0].group == "咖啡"


def test_small_groups_are_skipped():
    """三个点算出来的四分位数没有意义，硬算会得出「最大的那笔就是异常」。"""
    rows = [txn(date(2026, 1, i + 1), "X", "5.00", "咖啡") for i in range(3)]
    assert find_outliers(rows, group_by="category") == []


def test_uniform_amounts_produce_no_outliers():
    rows = [txn(date(2026, 1, i + 1), "X", "5.00", "咖啡") for i in range(20)]
    assert find_outliers(rows, group_by="category") == []


def test_category_grouping_refuses_when_nothing_is_categorized():
    """没分类时所有交易挤进同一个「未分类」组，围栏退化成全局围栏。

    实测在这份数据上会标出 31 笔、房租被标 12 次 —— 与其安静地返回一堆噪音，
    不如直接说清楚。
    """
    rows = [txn(date(2026, 1, i + 1), "X", "5.00") for i in range(10)]

    with pytest.raises(ValueError) as exc:
        find_outliers(rows, group_by="category")

    assert "未分类" in str(exc.value)


def test_unknown_group_by_is_rejected():
    with pytest.raises(ValueError):
        find_outliers([], group_by="week")


# --- 在真实账单上：6 个坑里的 4 个 ---------------------------------------


def test_finds_the_double_charge_trap(categorized):
    """坑 1。**正好 1 组** —— 多出来的每一组都是误报。"""
    found = find_duplicates(categorized)

    assert len(found) == 1
    assert found[0].merchant == "TARGET T-1234"
    assert found[0].dates == (date(2026, 3, 14), date(2026, 3, 14))


def test_finds_the_price_increase_trap(categorized):
    """坑 2。也正好 1 个 —— 别把消费变多误报成涨价。"""
    found = find_price_increases(categorized)

    assert [p.merchant for p in found] == ["NETFLIX.COM"]
    assert found[0].was == money("15.49")
    assert found[0].now == money("19.36")


def test_finds_the_outlier_trap(categorized):
    """坑 3。按类目分组时，购物类里那笔 3899 该被揪出来。"""
    found = find_outliers(categorized, group_by="category")

    assert any(o.merchant == "BEST BUY #245" for o in found)
    assert any(o.amount == money("3899.00") for o in found)


def test_ghost_subscription_shows_up_with_its_annual_cost(categorized):
    """坑 4。幽灵订阅的定义是「你在付、但你忘了」。

    没有使用数据就没法判断「忘了」，唯一诚实的做法是把周期性扣款全列出来、
    按年化金额排序 —— 9.99 一个月没有痛感，120 一年就有。
    """
    subs = {s.merchant: s for s in find_subscriptions(categorized)}

    ghost = subs["READLY DIGITAL MAGAZINES"]
    assert ghost.occurrences == 12
    assert ghost.amount == money("9.99")
    # 9.99 一个月没有痛感，119.88 一年就有 —— 这个数字本身就是洞察
    assert ghost.yearly == money("119.88")


def test_variable_utilities_are_not_listed_as_a_subscription(categorized):
    """水电燃气每月都扣、间隔规律，但金额浮动 —— 不是固定价格的扣款。"""
    merchants = {s.merchant for s in find_subscriptions(categorized)}
    assert "CITY UTILITIES ELECTRIC" not in merchants


def test_subscriptions_are_ranked_by_annual_cost(categorized):
    """按年化排 —— 按月的顺序会让 9.99 排在有痛感的位置前面。"""
    yearly = [s.yearly for s in find_subscriptions(categorized)]
    assert yearly == sorted(yearly, reverse=True)
