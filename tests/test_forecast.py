"""预测层。

这里钉的大多是**守卫**而不是算法 —— 速率推算本身就是一行乘除法。真正会出错的
是「什么情况下不该推」：那类错误的形状都一样，**没有报错、数字看着合理、但它
是错的**（例如拿一段根本没有数据的区间去推，得到一个 0，而 0 看起来就像「这
段时间没花钱」）。

日期一律当参数传进去，模块里不读 `date.today()`；测试因此能把今天钉死。
"""

from datetime import date

import pytest

from fa.forecast import (
    coverage,
    month_bounds,
    project_period,
    upcoming_charges,
)
from fa.models import Transaction, money

TODAY = date(2026, 9, 22)


def bill(*rows: tuple[str, str, str], category: str = "购物") -> list[Transaction]:
    """(日期, 商户, 金额) → Transaction 列表。"""
    return [
        Transaction(
            date=date.fromisoformat(day),
            merchant=merchant,
            amount=money(amount),
            account="credit",
            txn_id=f"T{index:04d}",
            category=category,
        )
        for index, (day, merchant, amount) in enumerate(rows, start=1)
    ]


def monthly(merchant: str, day: int, months: list[int], amount: str) -> list[Transaction]:
    """某商户在 2026 年的这几个月的 day 号各扣一次。"""
    return bill(*[(f"2026-{month:02d}-{day:02d}", merchant, amount) for month in months])


# --- 区间换算 -----------------------------------------------------------


def test_month_bounds_are_closed_intervals():
    """查询层是闭区间口径，月末别在这儿算错 —— 这是最容易差一天的地方。"""
    assert month_bounds("2026-09") == (date(2026, 9, 1), date(2026, 9, 30))
    assert month_bounds("2026-02") == (date(2026, 2, 1), date(2026, 2, 28))
    assert month_bounds("2024-02") == (date(2024, 2, 1), date(2024, 2, 29))  # 闰年
    assert month_bounds("2026-12") == (date(2026, 12, 1), date(2026, 12, 31))  # 跨年


# --- 覆盖范围 -----------------------------------------------------------


def test_coverage_reports_range_and_lag():
    txns = bill(("2026-08-01", "A", "10.00"), ("2026-09-22", "B", "20.00"))

    cov = coverage(txns, today=TODAY)

    assert cov.first == date(2026, 8, 1)
    assert cov.last == date(2026, 9, 22)
    assert cov.lag_days == 0
    assert [m.year_month for m in cov.months] == ["2026-08", "2026-09"]


def test_coverage_measures_the_lag_when_the_bill_is_behind():
    """数据落后是常态，得能算出来 —— 预测必须知道自己脚下有没有实地。"""
    cov = coverage(bill(("2026-08-28", "A", "10.00")), today=date(2026, 9, 22))

    assert cov.lag_days == 25


def test_coverage_records_where_the_month_actually_ends():
    """末月是残的：记的是**数据里**的最后一天，不是日历月末。"""
    txns = bill(("2026-08-01", "A", "10.00"), ("2026-08-28", "B", "20.00"))

    month = coverage(txns, today=TODAY).month("2026-08")

    assert month.last == date(2026, 8, 28)
    assert month.count == 2
    assert coverage(txns, today=TODAY).month("2026-07") is None


def test_coverage_rejects_an_empty_bill():
    """空账单上所有下游数字都会是 0，而那看起来像「你这个月没花钱」。"""
    with pytest.raises(ValueError, match="一笔都没有"):
        coverage([], today=TODAY)


# --- 速率推算 -----------------------------------------------------------


def test_projection_keeps_every_input_it_used():
    """报差值必须把原始数字一起写出来，所以这些字段一个都不能少 ——
    用户看到「预计 545.45」是没法核对的。"""
    txns = bill(("2026-09-01", "A", "100.00"), ("2026-09-11", "B", "100.00"))

    p = project_period(
        txns,
        period_start=date(2026, 9, 1),
        period_end=date(2026, 9, 30),
        today=date(2026, 9, 11),
    )

    assert p.observed == money("200.00")
    assert p.count == 2
    assert p.observed_days == 11
    assert p.total_days == 30
    assert p.projected == money("545.45")  # 200 / 11 × 30
    assert p.daily_rate == money("18.18")
    assert p.is_partial


def test_projection_refuses_a_period_with_no_data():
    """最危险的一种输出：推出来是 0，而 0 看起来像「这段时间没花钱」。"""
    txns = bill(("2026-08-01", "A", "10.00"))

    with pytest.raises(ValueError, match="一天数据都没有"):
        project_period(
            txns,
            period_start=date(2026, 9, 1),
            period_end=date(2026, 9, 30),
            today=TODAY,
        )


def test_projection_refuses_a_period_starting_before_the_bill():
    """区间比账单还早，漏掉的日子会被当成「没花钱」，数字偏小而不报错。"""
    txns = bill(("2026-09-05", "A", "10.00"))

    with pytest.raises(ValueError, match="才开始"):
        project_period(
            txns,
            period_start=date(2026, 9, 1),
            period_end=date(2026, 9, 30),
            today=TODAY,
        )


def test_projection_refuses_a_future_period():
    """未来没有「当前速率」可言 —— 那是纯外推，不该从这个函数出来。"""
    txns = bill(("2026-09-05", "A", "10.00"))

    with pytest.raises(ValueError, match="还没到"):
        project_period(
            txns,
            period_start=date(2026, 10, 1),
            period_end=date(2026, 10, 31),
            today=TODAY,
        )


def test_a_finished_period_is_not_marked_partial():
    """区间已经走完时不该标「预计」—— 那会让一个实际值看起来是猜的。"""
    txns = bill(("2026-08-01", "A", "100.00"), ("2026-08-31", "B", "200.00"))

    p = project_period(
        txns,
        period_start=date(2026, 8, 1),
        period_end=date(2026, 8, 31),
        today=TODAY,
    )

    assert not p.is_partial
    assert p.observed_days == p.total_days == 31
    assert p.projected == p.observed == money("300.00")


# --- 固定扣款前瞻 -------------------------------------------------------


def test_monthly_charge_advances_by_calendar_month_not_by_31_days():
    """月付是「每月 5 号扣」，不是「每 31 天扣一次」。

    拿 31 天去推，9/5 会变成 10/6 —— 一个月漂一天，漂到年底差半个月。
    这个 bug 真的出现过。
    """
    txns = monthly("NETFLIX.COM", 5, [5, 6, 7, 8, 9], "15.49")

    found = upcoming_charges(txns, today=date(2026, 9, 10), horizon_days=40)

    assert [u.next_date for u in found] == [date(2026, 10, 5)]


def test_quarterly_charge_advances_by_three_months():
    """季付走的是同一条路：跨 3 个日历月，不是 +91 天。"""
    txns = bill(
        ("2025-01-15", "INSURANCE", "300.00"),
        ("2025-04-15", "INSURANCE", "300.00"),
        ("2025-07-15", "INSURANCE", "300.00"),
        ("2025-10-15", "INSURANCE", "300.00"),
    )

    found = upcoming_charges(txns, today=date(2025, 11, 1), horizon_days=120)

    assert [u.next_date for u in found] == [date(2026, 1, 15)]


def test_a_month_end_charge_clamps_and_keeps_its_anchor():
    """31 号扣的订阅遇到 2 月退到 28 号，**但 3 月要回到 31 号**。

    日锚点如果取自上一期（退过的 28 号），往后就永远是 28 号，一个月丢三天。
    """
    txns = bill(
        ("2025-10-31", "RENT", "100.00"),
        ("2025-11-30", "RENT", "100.00"),
        ("2025-12-31", "RENT", "100.00"),
        ("2026-01-31", "RENT", "100.00"),
    )

    february = upcoming_charges(txns, today=date(2026, 2, 1), horizon_days=30)
    march = upcoming_charges(txns, today=date(2026, 3, 1), horizon_days=30)

    assert [u.next_date for u in february] == [date(2026, 2, 28)]  # 截到月末
    assert [u.next_date for u in march] == [date(2026, 3, 31)]  # 锚点还在 31


def test_upcoming_uses_the_current_price_not_the_original_one():
    """涨过价的订阅推的是新价 —— 「按现在这个价接下来会扣多少」。"""
    txns = bill(
        ("2026-05-05", "NETFLIX.COM", "15.49"),
        ("2026-06-05", "NETFLIX.COM", "15.49"),
        ("2026-07-05", "NETFLIX.COM", "19.36"),
        ("2026-08-05", "NETFLIX.COM", "19.36"),
        ("2026-09-05", "NETFLIX.COM", "19.36"),
    )

    found = upcoming_charges(txns, today=date(2026, 9, 10), horizon_days=40)

    assert [u.amount for u in found] == [money("19.36")]


def test_charges_beyond_the_horizon_are_left_out():
    txns = monthly("NETFLIX.COM", 5, [5, 6, 7, 8, 9], "15.49")

    assert upcoming_charges(txns, today=date(2026, 9, 10), horizon_days=5) == []


def test_upcoming_is_sorted_by_date():
    txns = monthly("NETFLIX.COM", 20, [5, 6, 7, 8, 9], "15.49") + monthly(
        "SPOTIFY", 5, [5, 6, 7, 8, 9], "11.99"
    )

    found = upcoming_charges(txns, today=date(2026, 9, 10), horizon_days=40)

    assert [u.next_date for u in found] == [date(2026, 10, 5), date(2026, 10, 20)]


def test_upcoming_rejects_a_nonpositive_horizon():
    with pytest.raises(ValueError, match="正数"):
        upcoming_charges([], today=TODAY, horizon_days=0)


def test_a_one_off_merchant_is_not_a_charge():
    """只出现两三次的商户不是订阅 —— 那是消费，不是固定扣款。"""
    txns = bill(
        ("2026-08-03", "TARGET T-1234", "50.00"),
        ("2026-09-03", "TARGET T-1234", "50.00"),
    )

    assert upcoming_charges(txns, today=TODAY, horizon_days=60) == []
