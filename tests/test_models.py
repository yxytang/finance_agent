"""金额与交易记录。

大部分测试看着琐碎，但它们钉的是同一个东西：**数字是精确的**。
这个项目对外承诺「答案可复现、可核对」，而承诺是靠这一层兑现的。
"""

from dataclasses import FrozenInstanceError
from datetime import date
from decimal import Decimal

import pytest

from fa.models import Transaction, format_money, money


def tx(amount="1.00", **kwargs) -> Transaction:
    defaults = dict(
        date=date(2026, 1, 5), merchant="STARBUCKS #1234", amount=money(amount),
        account="credit", txn_id="T000001",
    )
    return Transaction(**{**defaults, **kwargs})


# --- 金额 ---------------------------------------------------------------


def test_float_never_reaches_decimal_directly():
    """`Decimal(0.1)` 是**脏的**。

    不是 Decimal 坏了，是 `0.1` 这个字面量在 Python 里先变成了二进制浮点数，
    Decimal 接到的已经是个近似值。所以 `money()` 必须走字符串 ——
    这不是洁癖，是「金额从哪进来」这条路上的唯一一道闸。
    """
    assert Decimal(0.1) != Decimal("0.1")
    assert money(0.1) == Decimal("0.10")


def test_money_quantizes_to_cents():
    assert money("1.005") == Decimal("1.01")
    assert money("1.004") == Decimal("1.00")


def test_money_accepts_the_usual_inputs():
    assert money(5) == Decimal("5.00")
    assert money("5") == Decimal("5.00")
    assert money(-139.99) == Decimal("-139.99")


def test_decimal_sums_are_exact():
    """评测做的是**精确比对**，浮点误差会让本来对的答案时不时判错。"""
    assert sum([money("0.10")] * 3, Decimal("0")) == Decimal("0.30")
    assert 0.10 + 0.20 != 0.30
    assert sum([0.10] * 3) != 0.30


def test_format_money_is_readable():
    assert format_money(money("1234567.5")) == "1,234,567.50"
    assert format_money(money("-139.99")) == "-139.99"


# --- 交易 ---------------------------------------------------------------


def test_transaction_is_frozen():
    """分类是产出一条新记录，不是就地改字段 —— 「改之前是什么样」得留着。"""
    with pytest.raises(FrozenInstanceError):
        tx().merchant = "别的商户"


def test_with_category_leaves_the_original_alone():
    original = tx()
    assert original.category is None

    tagged = original.with_category("咖啡")

    assert tagged.category == "咖啡"
    assert original.category is None
    assert tagged.txn_id == original.txn_id


def test_year_month_is_zero_padded():
    assert tx(date=date(2026, 3, 5)).year_month == "2026-03"
    assert tx(date=date(2025, 12, 31)).year_month == "2025-12"


def test_str_shows_uncategorized_clearly():
    assert "未分类" in str(tx())
    assert "[咖啡]" in str(tx().with_category("咖啡"))
