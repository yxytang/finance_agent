"""异常与订阅检测 —— 全确定性，不碰 LLM。

四个检测器：

  · `find_duplicates`      同一商户同一天被扣了两次
  · `find_price_increases` 固定价格的周期性扣款涨价了
  · `find_subscriptions`   列出所有周期性扣款（幽灵订阅就藏在这个列表里）
  · `find_outliers`        某笔金额远超同组的其他笔

全都不返回字符串，返回结构化 finding —— 第八天的评测要拿它们和埋好的 6 个坑
对数，字符串没法对数。

## 两处和原方案不一样的地方

**1. 异常大额不用「超 p95」，用 Tukey 围栏（Q3 + k×IQR）。**

原方案写的是「类目内金额超 p95」。但 p95 按定义就会标出 5% 的交易 ——
这份账单上千笔，那就是几十笔。**一个标出几十笔的异常检测等于没有检测**：
用户看两次就会学会无视它，而那之后真正的那一笔也一起被无视了。

（这里刻意不写「1061 笔」这种精确数：账单的结束日跟今天走，那个数会过时。）

围栏法问的是「离主体有多远」，不是「排在前面几名」。同一个阈值下，
它只揪出真正离群的，被改坏的阈值也不会一崩就是几十笔。

**2. 多加了一个 `find_subscriptions`。**

原方案说「三个检测器」，但 6 个坑里的「幽灵订阅」不属于其中任何一个 ——
幽灵订阅的定义是「你在付、但你忘了」。没有使用数据就没法判断「忘了」，唯一
诚实的做法是**把周期性扣款全列出来，并按年化金额排序**：9.99 一个月感觉不到，
一年 119.88 就有感觉了。这个数字本身就是洞察。
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from statistics import mean, median, quantiles, stdev

from fa.models import CENT, Money, Transaction, UNCATEGORIZED, ZERO

# --- 阈值（都做成参数，但默认值放在这里，好统一调）----------------------

# 多少笔以上才谈得上「周期性」。四次是一个订阅至少该出现的次数。
MIN_OCCURRENCES = 4

# 金额最多只能有几种取值。这是订阅最可靠的指纹：**它反复扣同一个数**。
#
# 不卡这条会怎样：一家你去了 5 次的餐厅会在「前半年均价 vs 后半年均价」上
# 触发涨价告警 —— 那不是涨价，那是你最近吃得贵。
MAX_DISTINCT_AMOUNTS = 3

# 间隔的规律程度（变异系数上限）。
#
# 只卡「金额固定」还不够：一杯 5.00 的咖啡一年里恰好买到 4 次同样金额，
# 长得和订阅一模一样。加上「间隔规律」才把两者分开 —— 订阅是按月扣的，
# 随机消费不是。0.25 能容下月末天数差（28~31 天）和 2 月截断。
MAX_GAP_CV = Decimal("0.25")

# 间隔至少这么久才算「周期性扣款」。挡的是每天/每周都去的店。
MIN_GAP_DAYS = 20

# 重复扣款的时间窗：同商户 + 同金额，隔这么近才算「重复」。
#
# 3 天足够短，短到月度订阅（间隔约 30 天）不会误报；也足够长，长到跨周末的
# 双重扣款能被抓到。
DEFAULT_WINDOW_DAYS = 3

# 围栏倍数。Tukey 的「远外侧」用 3 —— 1.5 是「疑似」，3 是「离群」。
DEFAULT_OUTLIER_K = Decimal("3")

# 少于这么多笔的分组不判异常：四个点算出来的四分位数没什么意义。
MIN_GROUP_FOR_OUTLIER = 4


@dataclass(frozen=True)
class DuplicateCharge:
    merchant: str
    amount: Money
    txn_ids: tuple[str, ...]
    dates: tuple[date, ...]

    @property
    def times(self) -> int:
        return len(self.txn_ids)


@dataclass(frozen=True)
class PriceIncrease:
    merchant: str
    was: Money
    now: Money
    since: date  # 第一次按新价扣费的那天
    occurrences: int

    @property
    def ratio(self) -> Decimal:
        return (self.now / self.was).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    @property
    def yearly_extra(self) -> Money:
        """涨价之后一年多花多少。

        比「涨了 25%」有用得多 —— 25% 要用户自己在脑子里乘一遍才知道疼不疼，
        「一年多花 46.44」不用。和 Subscription.yearly 一样是**按当前差价折算
        未来一年**，不是过去一年实际多花的。
        """
        return ((self.now - self.was) * 12).quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class Subscription:
    merchant: str
    amount: Money  # 当前单价
    occurrences: int
    monthly: Money  # 折算到每月
    yearly: Money  # 折算到每年 —— 这个数才是重点
    first_seen: date
    last_seen: date


@dataclass(frozen=True)
class Outlier:
    txn_id: str
    merchant: str
    amount: Money
    group: str
    threshold: Money  # 围栏值，超过它才被判为异常
    ratio: Decimal  # amount / threshold


# --- 共用：什么叫「周期性扣款」------------------------------------------


def _is_periodic(rows: list[Transaction]) -> bool:
    """间隔是否规律。rows 必须已按日期排好。"""
    gaps = [(b.date - a.date).days for a, b in zip(rows, rows[1:])]
    if not gaps:
        return False

    middle = median(gaps)
    if middle < MIN_GAP_DAYS:
        return False

    average = mean(gaps)
    if average <= 0:
        return False

    # 只有一个间隔时 stdev 无意义（默认会抛），但 CV 显然算作 0。
    spread = stdev(gaps) if len(gaps) > 1 else 0.0
    return Decimal(str(spread / average)) <= MAX_GAP_CV


def _recurring_charges(
    txns: list[Transaction],
    *,
    min_occurrences: int = MIN_OCCURRENCES,
    max_distinct: int = MAX_DISTINCT_AMOUNTS,
) -> dict[str, list[Transaction]]:
    """找出固定价格的周期性扣款，按商户分组。返回的列表已按日期排好。

    `find_subscriptions` 和 `find_price_increases` 共用这一层 —— 「什么算
    订阅」只能有一个定义。两份就会漂移，然后有一份忘了改，订阅列表和涨价
    告警就对不上了。
    """
    buckets: dict[str, list[Transaction]] = defaultdict(list)
    for txn in txns:
        buckets[txn.merchant].append(txn)

    found: dict[str, list[Transaction]] = {}
    for merchant, rows in buckets.items():
        if len(rows) < min_occurrences:
            continue
        if len({r.amount for r in rows}) > max_distinct:
            continue

        ordered = sorted(rows, key=lambda r: r.date)
        if not _is_periodic(ordered):
            continue
        found[merchant] = ordered

    return found


def _periods_per_month(rows: list[Transaction]) -> Decimal:
    """这笔周期性扣费每月扣几次。

    **按日历月计数，不是按天数的平均值。** 后者在短窗口上有系统性偏差：
    1~6 月的月均长度是 30.2 天（冬季月份长），外推出来会把 10.00 的月订阅
    算成 10.08 —— 每一笔都差 0.8%，而且方向固定，不会互相抵消。

    日历月计数对「每月 5 号扣一次」这种最常见的情形给出精确的 1，
    对「一月扣两次」给出 2，都没有外推。

    结果是**按当前单价折算的未来一年**，不是「过去一年实际花了多少」。
    订阅涨过价时两者不一样（NETFLIX 涨过），要看实际发生额就用 query 查。
    """
    if len(rows) < 2:
        return Decimal(1)

    months = (
        (rows[-1].date.year - rows[0].date.year) * 12
        + (rows[-1].date.month - rows[0].date.month)
        + 1
    )
    return Decimal(len(rows)) / Decimal(max(months, 1))


# --- 四个检测器 ---------------------------------------------------------


def find_duplicates(
    txns: list[Transaction], *, window_days: int = DEFAULT_WINDOW_DAYS
) -> list[DuplicateCharge]:
    """找出「同商户 + 同金额，且在 window_days 天内」被扣了多次的。

    为什么按「同商户同金额」而不是 txn_id：**真实的双重扣款是两笔不同 id 的
    同额交易**。同一 id 出现两次是数据重复导入，那是另一回事（ingest 层的问题）。

    为什么要有时间窗：月度订阅每个月扣同一个数，天经地义。窗口够短，
    它就不会被误报；窗口够长，跨周末的双重扣款也能抓到。
    """
    buckets: dict[tuple[str, Money], list[Transaction]] = defaultdict(list)
    for txn in txns:
        buckets[(txn.merchant, txn.amount)].append(txn)

    found: list[DuplicateCharge] = []
    for (merchant, amount), rows in buckets.items():
        ordered = sorted(rows, key=lambda r: r.date)

        # 按相邻间隔聚类：间隔 <= 窗口的算同一簇。
        # 不做「任意两笔在窗口内」的两两比较 —— 月度订阅攒了 12 笔，
        # 两两比较是 66 次无谓的配对。
        cluster: list[Transaction] = [ordered[0]]
        for previous, current in zip(ordered, ordered[1:]):
            if (current.date - previous.date).days <= window_days:
                cluster.append(current)
            else:
                if len(cluster) >= 2:
                    found.append(_duplicate_of(merchant, amount, cluster))
                cluster = [current]
        if len(cluster) >= 2:
            found.append(_duplicate_of(merchant, amount, cluster))

    found.sort(key=lambda d: (d.dates[0], d.merchant))
    return found


def _duplicate_of(merchant: str, amount: Money, rows: list[Transaction]) -> DuplicateCharge:
    return DuplicateCharge(
        merchant=merchant,
        amount=amount,
        txn_ids=tuple(r.txn_id for r in rows),
        dates=tuple(r.date for r in rows),
    )


def find_price_increases(
    txns: list[Transaction], *, min_rise: Decimal = Decimal("0.05")
) -> list[PriceIncrease]:
    """在周期性扣款里找涨价。

    `min_rise=0.05` 是 5% —— 低于这个幅度的变动多半是税费或汇率波动，
    报出来只是噪音。

    局限：只看**首笔**和**末笔**的单价。先涨后降的订阅会互相抵消、报不出来。
    换成「逐段检测」能覆盖，但代价是参数和误报都变多，现在不值得。
    """
    found: list[PriceIncrease] = []

    for merchant, rows in _recurring_charges(txns).items():
        was, now = rows[0].amount, rows[-1].amount
        if was <= ZERO or now <= was * (1 + min_rise):
            continue

        since = next(r.date for r in rows if r.amount == now)
        found.append(
            PriceIncrease(
                merchant=merchant, was=was, now=now, since=since, occurrences=len(rows)
            )
        )

    found.sort(key=lambda p: p.ratio, reverse=True)
    return found


def find_subscriptions(txns: list[Transaction]) -> list[Subscription]:
    """列出所有周期性扣款，按**年化金额**降序。

    按年化排而不是按月，是因为这个项目要解决的就是「感觉不到」：9.99 一个月
    没有痛感，119.88 一年就有。幽灵订阅之所以是幽灵，正是因为按月看它太小。
    """
    found: list[Subscription] = []

    for merchant, rows in _recurring_charges(txns).items():
        current = rows[-1].amount
        monthly = (current * _periods_per_month(rows)).quantize(
            CENT, rounding=ROUND_HALF_UP
        )
        found.append(
            Subscription(
                merchant=merchant,
                amount=current,
                occurrences=len(rows),
                monthly=monthly,
                yearly=(monthly * 12).quantize(CENT, rounding=ROUND_HALF_UP),
                first_seen=rows[0].date,
                last_seen=rows[-1].date,
            )
        )

    found.sort(key=lambda s: s.yearly, reverse=True)
    return found


def _quantile(values: list[Money], fraction: float) -> Money:
    """线性插值的分位数。`statistics.quantiles` 对 <2 个样本会抛，所以调用方
    得先保证样本够（见 MIN_GROUP_FOR_OUTLIER）。"""
    if len(values) == 1:
        return values[0]
    return quantiles(sorted(values), n=100, method="inclusive")[int(fraction * 100) - 1]


def find_outliers(
    txns: list[Transaction],
    *,
    group_by: str = "category",
    k: Decimal = DEFAULT_OUTLIER_K,
) -> list[Outlier]:
    """Tukey 围栏：金额超过 `Q3 + k×IQR` 的算异常。

    `group_by` 取 `category` / `merchant` / `none`。分组的理由很实际：
    「3800 元的笔记本」在购物类里正常，在咖啡类里就是数据错误。**异常是相对的，
    没有组就没有基准。**

    取样 < 4 的分组直接跳过 —— 四个点算出来的四分位数没有意义，硬算会得出
    「这一组最大的那笔就是异常」这种废话。
    """
    if group_by not in ("category", "merchant", "none"):
        raise ValueError(f"group_by 只能是 category / merchant / none，收到 {group_by!r}")

    if group_by == "category" and not any(t.category for t in txns):
        # 一笔都没分类的时候，所有交易会挤进同一个「未分类」组，围栏退化成
        # 全局围栏 —— 实测在这份数据上会标出 31 笔，其中房租被标了 12 次。
        # 这正是「一个标出 50 笔的检测等于没有检测」那个坑，从另一扇门进来。
        # 与其安静地返回一堆噪音，不如直接说清楚。
        raise ValueError(
            "一笔交易都还没分类，按类目分组没有意义 —— 所有交易会挤进同一个"
            "「未分类」组，异常判定退化成对全体金额判定。"
            "用 group_by='merchant'，或者先跑分类。"
        )

    buckets: dict[str, list[Transaction]] = defaultdict(list)
    for txn in txns:
        if group_by == "category":
            buckets[txn.category or UNCATEGORIZED].append(txn)
        elif group_by == "merchant":
            buckets[txn.merchant].append(txn)
        else:
            buckets["全部"].append(txn)

    found: list[Outlier] = []
    for name, rows in buckets.items():
        if len(rows) < MIN_GROUP_FOR_OUTLIER:
            continue

        q1 = _quantile([r.amount for r in rows], 0.25)
        q3 = _quantile([r.amount for r in rows], 0.75)
        fence = q3 + k * (q3 - q1)

        for txn in rows:
            if txn.amount > fence:
                found.append(
                    Outlier(
                        txn_id=txn.txn_id,
                        merchant=txn.merchant,
                        amount=txn.amount,
                        group=name,
                        threshold=fence.quantize(CENT, rounding=ROUND_HALF_UP),
                        ratio=(txn.amount / fence).quantize(
                            Decimal("0.01"), rounding=ROUND_HALF_UP
                        )
                        if fence > ZERO
                        else ZERO,
                    )
                )

    found.sort(key=lambda o: o.ratio, reverse=True)
    return found
