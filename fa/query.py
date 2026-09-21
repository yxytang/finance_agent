"""确定性查询引擎 —— 整个项目的心脏。

两条不变量，破一条这个项目就没了：

1. **纯函数。** 不碰 LLM、不碰文件、不碰全局状态。所以它可单测、可以拿
   「0 笔交易」这种边界去钉，而且**能当第八天问答评测的 oracle** ——
   确定性的东西自己就是标准答案，不需要人工标注。
2. **返回结构化结果，不返回字符串。** 渲染成什么样是工具层的事。一个返回
   字符串的查询引擎没法当 oracle：你得反过来解析自己渲染的文本才能拿到数字，
   那是在测试渲染器，不是在测试查询。

`filters.describe()` 是个例外 —— 它渲染的**不是结果，是条件**。回答里必须
复述查询条件（prompt 里的第三条铁律），而这句话由谁来说都得一样，所以放在
这里而不是各工具里各写一遍。
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from fa.config import CATEGORIES
from fa.models import CENT, UNCATEGORIZED, Money, Transaction, ZERO

GROUP_BY_CHOICES = ("none", "category", "month", "merchant")
AGG_CHOICES = ("sum", "count", "avg", "max", "min")

AGG_LABELS = {
    "sum": "合计",
    "count": "笔数",
    "avg": "平均每笔",
    "max": "最大一笔",
    "min": "最小一笔",
}
GROUP_LABELS = {
    "none": "不分组",
    "category": "类目",
    "month": "月份",
    "merchant": "商户",
}


class QueryError(Exception):
    """查询参数有问题。

    消息里必须写清**合法值有哪些** —— 收到它的人（模型或用户）下一步的动作
    就是改参数，不告诉他改什么，他只会再猜一次。
    """


@dataclass(frozen=True)
class Filters:
    """这次查询到底筛了什么。

    原样带回来，是为了让回答能复述条件。用户看到「1,234.56」是没法核对的，
    得看到「2026-07 全月、类目=餐饮外卖，合计 1,234.56」才行。
    """

    date_from: date | None = None
    date_to: date | None = None
    categories: tuple[str, ...] = ()
    merchant_contains: str | None = None
    account: str | None = None
    agg: str = "sum"
    group_by: str = "none"

    def describe(self) -> str:
        parts: list[str] = []

        if self.date_from and self.date_to:
            parts.append(f"{self.date_from.isoformat()} ~ {self.date_to.isoformat()}")
        elif self.date_from:
            parts.append(f"{self.date_from.isoformat()} 起")
        elif self.date_to:
            parts.append(f"截至 {self.date_to.isoformat()}")
        else:
            parts.append("全部时间")

        parts.append("、".join(self.categories) if self.categories else "全部类目")

        if self.merchant_contains:
            parts.append(f"商户含「{self.merchant_contains}」")
        if self.account:
            parts.append(f"账户={self.account}")

        how = AGG_LABELS[self.agg]
        if self.group_by != "none":
            how += f"（按{GROUP_LABELS[self.group_by]}分组）"
        parts.append(how)

        return "，".join(parts)


@dataclass(frozen=True)
class Group:
    """一个分组：类目、月份或商户。"""

    key: str
    count: int  # 这一组里有几笔
    value: Decimal  # 按 agg 算出来的数（count 时就是 count）


@dataclass(frozen=True)
class QueryResult:
    """一次查询的结果。

    `matched` 和 `value` 是分开的两个数，别混：`matched` 永远是**命中笔数**，
    `value` 才是「用户问的那个数」。问「外卖花了多少」时 value 是金额、
    matched 是笔数；问「点了几次」时 value 就等于 matched。
    """

    matched: int
    value: Decimal
    groups: tuple[Group, ...]
    filters: Filters


# --- 内部：筛选与聚合 ----------------------------------------------------


def _matches(txn: Transaction, f: Filters) -> bool:
    if f.date_from and txn.date < f.date_from:
        return False
    if f.date_to and txn.date > f.date_to:
        return False
    if f.account and txn.account != f.account:
        return False
    if f.categories and (txn.category or UNCATEGORIZED) not in f.categories:
        return False
    if f.merchant_contains:
        # 不区分大小写：商户串都是大写的，而人（和模型）会写小写。
        if f.merchant_contains.lower() not in txn.merchant.lower():
            return False
    return True


def _aggregate(amounts: list[Money], agg: str) -> Decimal:
    if not amounts:
        # 空集合返回 0 而不是报错：**「你这个月没点外卖」是一个真实且常见的
        # 答案**，报错会让模型把它当成一次失败然后重试。
        return ZERO

    if agg == "sum":
        return sum(amounts, ZERO)
    if agg == "count":
        return Decimal(len(amounts))
    if agg == "avg":
        return (sum(amounts, ZERO) / len(amounts)).quantize(
            CENT, rounding=ROUND_HALF_UP
        )
    if agg == "max":
        return max(amounts)
    if agg == "min":
        return min(amounts)
    raise QueryError(f"不认识 agg={agg!r}。可选：{'、'.join(AGG_CHOICES)}")


def _group_key(txn: Transaction, group_by: str) -> str:
    if group_by == "category":
        return txn.category or UNCATEGORIZED
    if group_by == "month":
        return txn.year_month
    if group_by == "merchant":
        return txn.merchant
    return ""


# --- 参数校验 -----------------------------------------------------------


def _check_categories(categories) -> tuple[str, ...]:
    """类目必须是固定枚举里的一员。

    **这是最值得单独拦一遍的参数。** 传一个不存在的类目进去，查询不会报错，
    只会返回 0 笔 —— 然后模型会自信地告诉用户「你这个月没吃饭」。
    一个错的枚举值安静地返回空，是最难发现的一类错误。
    """
    if not categories:
        return ()

    allowed = set(CATEGORIES) | {UNCATEGORIZED}
    unknown = [c for c in categories if c not in allowed]
    if unknown:
        raise QueryError(
            f"没有这些类目：{'、'.join(unknown)}。"
            f"可选的只有：{'、'.join(CATEGORIES)}（外加「{UNCATEGORIZED}」）"
        )
    return tuple(categories)


def _check_choice(value: str, choices: tuple[str, ...], what: str) -> str:
    if value not in choices:
        raise QueryError(f"不认识 {what}={value!r}。可选：{'、'.join(choices)}")
    return value


# --- 入口 ---------------------------------------------------------------


def query(
    txns: list[Transaction],
    *,
    date_from: date | None = None,
    date_to: date | None = None,
    categories=None,
    merchant_contains: str | None = None,
    account: str | None = None,
    agg: str = "sum",
    group_by: str = "none",
) -> QueryResult:
    """筛出符合条件的交易，按 agg 算一个数，可选按 group_by 分组。

    `date_from` / `date_to` 都是**闭区间**（含当天）。用户说「7 月」时，
    7 月 31 号那天的消费当然要算进去。

    排序规则按分组方式定，不是随意的：
      · `month` 按时间升序 —— 时间序列要看出趋势，倒着排没法看
      · 其它按数值降序 —— 「哪家点得最多」要的是头部，不是尾部
    """
    agg = _check_choice(agg, AGG_CHOICES, "agg")
    group_by = _check_choice(group_by, GROUP_BY_CHOICES, "group_by")

    if date_from and date_to and date_from > date_to:
        raise QueryError(f"起始日期 {date_from} 比结束日期 {date_to} 还晚。")

    filters = Filters(
        date_from=date_from,
        date_to=date_to,
        categories=_check_categories(categories),
        merchant_contains=merchant_contains or None,
        account=account or None,
        agg=agg,
        group_by=group_by,
    )

    rows = [t for t in txns if _matches(t, filters)]

    if group_by == "none":
        return QueryResult(
            matched=len(rows),
            value=_aggregate([t.amount for t in rows], agg),
            groups=(),
            filters=filters,
        )

    buckets: dict[str, list[Money]] = defaultdict(list)
    for txn in rows:
        buckets[_group_key(txn, group_by)].append(txn.amount)

    groups = [
        Group(key=key, count=len(amounts), value=_aggregate(amounts, agg))
        for key, amounts in buckets.items()
    ]
    groups.sort(key=(lambda g: g.key) if group_by == "month" else (lambda g: -g.value))

    return QueryResult(
        matched=len(rows),
        value=_aggregate([t.amount for t in rows], agg),
        groups=tuple(groups),
        filters=filters,
    )
