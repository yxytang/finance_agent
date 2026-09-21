"""交易记录与金额。

## 金额为什么是 Decimal 而不是 float

这个项目的卖点之一是「数字可复现、可核对」。用 float 会把这条卖点自己拆掉：
1100 笔求和下来误差落在 1e-10 量级，肉眼看不出来，但第八天的问答评测做的是
**精确比对**（agent 通过工具链拿到的数字 vs query.py 直算），浮点误差会让本来
正确的答案被判成错的 —— 而且时有时无，最难查的那种。

所以金额一律 `Decimal`，只在展示时量化到两位。

⚠️ 注意 `money()` 为什么先转字符串：
`Decimal(0.1)` 得到的是 0.1000000000000000055511151231257827021181583404541015625
—— 因为 `0.1` 这个字面量在 Python 里**先变成了一个二进制浮点数**，Decimal
接到的已经是个脏值了。这不是 Decimal 的 bug，是它诚实地把你给的数原样收下。
外部输入（CSV、模型返回、用户输入）必须走字符串。

## 金额符号约定

**支出为正，收入与退款为负。** 银行流水一般反过来。

选「支出为正」是因为「上个月花了多少」直接 `sum()` 才是直觉的；要是反过来，
每个查询都得先取负号，迟早有一处漏了 —— 而且漏了之后数字看着还挺合理。

这个约定在 README 里必须写明，因为它**反直觉**，人不看文档一定会猜错。

## 为什么 frozen

分类结果是一次性写进去的，不是就地改字段。要改类目就用 `with_category()`
产出一条新的。这样「改之前是什么样」永远还在，第八天的分类评测要的正是这个 ——
拿模型输出和真值比，而不是拿一个被就地改过的对象。
"""

from dataclasses import dataclass, replace
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

Money = Decimal
CENT = Decimal("0.01")
ZERO = Decimal("0")


def money(value) -> Money:
    """把外部输入变成金额。**先转字符串**，理由见模块 docstring。"""
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def format_money(amount: Money) -> str:
    """给模型看的金额。带千分位，负数写清楚 —— 退款不该长得像支出。"""
    return f"{amount:,.2f}"


@dataclass(frozen=True)
class Transaction:
    date: date
    merchant: str  # 银行给的原始描述串，如 "STARBUCKS #1234 SEATTLE WA"
    amount: Money  # 支出为正，收入/退款为负
    account: str  # 来自哪个账户
    txn_id: str  # 稳定 id：去重和评测配对都靠它
    category: str | None = None  # None = 还没分类

    @property
    def year_month(self) -> str:
        """`2025-07`。group_by=month 用它，别在查询里现拆日期。"""
        return f"{self.date.year:04d}-{self.date.month:02d}"

    def with_category(self, category: str) -> "Transaction":
        """产出一条只改了类目的新记录，原对象不动。"""
        return replace(self, category=category)

    def __str__(self) -> str:
        category = self.category or "未分类"
        return (
            f"{self.txn_id}  {self.date.isoformat()}  "
            f"{format_money(self.amount):>12}  [{category}]  {self.merchant}"
        )
