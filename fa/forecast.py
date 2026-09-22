"""预测 —— 从「已经发生」推到「大概会怎样」。

三个函数，都是**纯函数、返回结构化数据**（和 `fa/query.py` 同一条规矩：返回
字符串的东西没法当 oracle —— 你得反过来解析自己渲染的文本才能拿到数字，那是
在测试渲染器，不是在测试计算）。

## 为什么单独一个模块

`fa/anomalies.py` 回答的是「过去哪里不对劲」，这里回答的是「接下来大概怎样」。
两边都要读交易、都要认订阅，但问题方向相反，混在一起两边都会变糊。

## 「今天是哪天」为什么是参数

不在函数里读 `date.today()`，由调用方传进来。理由和 `build_system_prompt`
一样：纯函数才能拿固定输入复现，测试也才能把日期钉住。CLI 层负责传真实的今天。

## 借订阅那套口径，不重写

「什么算周期扣款」直接复用 `anomalies._recurring_charges` —— 那个定义只能有
一份，两份就会漂移，然后有一份忘了改，订阅列表和预测就对不上了。

「往后推一个周期」也沿用 `_periods_per_month` 里那条判断：**按日历月，不按天数
平均**。那儿有一条注释解释了按天平均为什么在短窗口上有系统性偏差；推到未来这
边更明显 —— 拿 31 天去推月付订阅，一个月漂一天，漂到年底差半个月。

## 预测值必须能指回它的基准

prompt 铁律 3 要求报差值时把两个原始数字一起写出来。预测更严格：用户看到的
每一个推断值，都得能顺着字段找到「已发生多少 / 过了几天 / 共几天 / 什么假设」。
所以 `Projection` 把输入原样全留着，不做成一句话。

**别从 `PriceIncrease.yearly_extra` 抄 ×12。** 那儿硬编码了 12、隐含「月付」，
季付的订阅会被算错。这里每个周期单独算，不做「一年」这种折算。
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from statistics import median

from fa.anomalies import _recurring_charges
from fa.models import CENT, Money, Transaction, ZERO

# 前瞻的默认窗口。一个月是个好默认：够覆盖所有月付订阅一次。
DEFAULT_HORIZON_DAYS = 30


@dataclass(frozen=True)
class MonthCoverage:
    """某个月在数据里长什么样。

    `first` / `last` 是**数据里**该月最早和最晚的一天，不是日历月的首尾 ——
    两者不一样才是重点（例如 8 月只有到 28 号的数据）。
    """

    year_month: str
    first: date
    last: date
    count: int


@dataclass(frozen=True)
class Coverage:
    """账单实际覆盖到哪、落后今天多少天。

    `describe_data` 已经在给人看的文字里提醒过「数据通常不是实时的」；这里是
    给算的那一份 —— 预测必须知道自己脚下有没有实地。
    """

    first: date
    last: date
    today: date
    months: tuple[MonthCoverage, ...]

    @property
    def lag_days(self) -> int:
        """数据落后今天多少天。0 或负数表示覆盖到今天。"""
        return (self.today - self.last).days

    def month(self, year_month: str) -> MonthCoverage | None:
        for item in self.months:
            if item.year_month == year_month:
                return item
        return None


@dataclass(frozen=True)
class Projection:
    """把一个还没走完的区间按当前速率推到完整。

    **输入原样留在字段里**，因为用户看到「预计 9,458」是没法核对的，得看到
    「9/1–9/22 花了 6,935.60（22 天），全月 30 天」。少写一个，这个数就
    成了不可核对的那种答案。
    """

    period_start: date
    period_end: date
    observed_through: date
    observed: Money
    count: int
    observed_days: int
    total_days: int
    projected: Money

    @property
    def daily_rate(self) -> Money:
        """已发生区间的日均。这是整条推算里唯一的假设，单独给出来。"""
        if self.observed_days <= 0:
            return ZERO
        return (self.observed / Decimal(self.observed_days)).quantize(
            CENT, rounding=ROUND_HALF_UP
        )

    @property
    def is_partial(self) -> bool:
        """这个区间还没走完 —— 也就是「预测」这件事成立的前提。"""
        return self.observed_through < self.period_end


@dataclass(frozen=True)
class Upcoming:
    """一笔即将发生的固定扣款。

    `next_date` 是**推断的**，不是账单里记着的 —— 账单里没有「下次扣款日」这个
    字段，它是从 `last_seen` 加中位间隔外推出来的。取消、改价都会让它落空。
    """

    merchant: str
    amount: Money
    next_date: date
    interval_days: int
    occurrences: int


# --- 区间换算的小工具 ----------------------------------------------------


def month_bounds(year_month: str) -> tuple[date, date]:
    """`2026-09` → (2026-09-01, 2026-09-30)。

    给调用方把「9 月」这种说法变成一个闭区间 —— 查询层是闭区间口径，别在这
    重算一遍月末（那正是容易写错的地方）。
    """
    year, month = (int(part) for part in year_month.split("-"))
    first = date(year, month, 1)
    next_month = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return first, next_month - timedelta(days=1)


# --- 覆盖范围 -----------------------------------------------------------


def coverage(txns: list[Transaction], *, today: date) -> Coverage:
    """数据覆盖到哪，逐月。

    一笔都没有时抛 —— 空账单上谈预测没有意义，而且那种情况下所有下游数字都会
    是 0，看起来像「你这个月没花钱」。
    """
    if not txns:
        raise ValueError(
            "账单是一笔都没有，没法谈覆盖范围。先确认数据生成了：`python -m data.generate`。"
        )

    by_month: dict[str, list[Transaction]] = defaultdict(list)
    for txn in sorted(txns, key=lambda t: t.date):
        by_month[txn.year_month].append(txn)

    months = tuple(
        MonthCoverage(
            year_month=name,
            first=rows[0].date,
            last=rows[-1].date,
            count=len(rows),
        )
        for name, rows in sorted(by_month.items())
    )

    return Coverage(
        first=months[0].first,
        last=months[-1].last,
        today=today,
        months=months,
    )


# --- 速率推算 -----------------------------------------------------------


def project_period(
    txns: list[Transaction],
    *,
    period_start: date,
    period_end: date,
    today: date,
) -> Projection:
    """把 [period_start, period_end] 按当前速率推到完整。

    典型用法是「这个月还没过完，全月大概多少」—— `period_end` 是月末，
    而数据只到某一天。

    三条守卫，每一条都防着「看起来合理但错了」：

      1. **区间必须已经开始**（`period_start <= today`）。问未来的区间没有
         「当前速率」可言，那是纯外推，不该混进这个函数。
      2. **数据必须覆盖到区间内的实际观测点**。数据只到 8-28 而去推 9 月，
         观测值会是 0，推出来也是 0 —— 一个看着像「9 月没花钱」的答案。
      3. **观测点不能等于区间末**。那说明区间已经走完了，没有可推的东西；
         这时候该直接报实际值，标成「预计」反而是画蛇添足。
    """
    if period_end < period_start:
        raise ValueError(f"区间是反的：{period_start} ~ {period_end}")
    if period_start > today:
        raise ValueError(
            f"{period_start} 还没到（今天是 {today}），没有「当前速率」可以推。"
            "要算未来的固定扣款用 upcoming_charges。"
        )

    cov = coverage(txns, today=today)

    if period_start < cov.first:
        raise ValueError(
            f"账单从 {cov.first} 才开始，覆盖不到 {period_start}。"
            "算这个区间会漏掉前面的日子，数字会偏小。"
        )

    # 观测点取「今天」「数据覆盖到的最后一天」「区间末」里最早的那个。
    observed_through = min(today, cov.last, period_end)

    if observed_through < period_start:
        # 区间里一天数据都没有。这时候观测值是 0，推出来也是 0 ——
        # 而那个 0 看起来就像「这段时间没花钱」，是这里最危险的一种输出。
        raise ValueError(
            f"账单只覆盖 {cov.first} ~ {cov.last}，"
            f"{period_start} ~ {period_end} 这个区间里一天数据都没有。"
            "硬推会得出一个看起来像「这段时间没花钱」的 0。"
        )

    window = [t for t in txns if period_start <= t.date <= observed_through]
    observed = sum((t.amount for t in window), ZERO)

    observed_days = (observed_through - period_start).days + 1
    total_days = (period_end - period_start).days + 1

    projected = (
        observed / Decimal(observed_days) * Decimal(total_days)
    ).quantize(CENT, rounding=ROUND_HALF_UP)

    return Projection(
        period_start=period_start,
        period_end=period_end,
        observed_through=observed_through,
        observed=observed,
        count=len(window),
        observed_days=observed_days,
        total_days=total_days,
        projected=projected,
    )


# --- 固定扣款前瞻 -------------------------------------------------------

# 往后推的上限。周期被算成 0、或者锚点落在很远的未来时会死循环，这里兜住。
_MAX_CYCLES = 240


def _days_in_month(year: int, month: int) -> int:
    first_of_next = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return (first_of_next - timedelta(days=1)).day


def _cycle_months(rows: list[Transaction]) -> int:
    """这笔扣费每期跨几个**日历月**。0 表示不到一个月，得按天数推。

    直接从「首尾跨了几个月 ÷ 间隔数」得出，而不是拿中位间隔去除以 30 ——
    后者对月付会得到 1.03 这种数，round 到 1 只是碰巧对，遇到季付就散了。

    命中不了的情形：**每 4 周扣一次**（固定 28 天）算出来也是 1 个月，长得和月付
    一样。真要区分得看「是不是每月同一个几号」，留到有真实需要时再说。
    """
    if len(rows) < 2:
        return 0

    span = (rows[-1].date.year - rows[0].date.year) * 12 + (
        rows[-1].date.month - rows[0].date.month
    )
    if span <= 0:
        return 0
    return round(span / (len(rows) - 1))


def _nth_charge(anchor: date, *, months: int, interval_days: int, n: int) -> date:
    """从 `anchor` 起第 n 期的扣款日。

    **日锚点取自 anchor，不取上一期。** 31 号扣的订阅遇到 2 月要退到 28 号；
    如果拿退过的那天当日锚点，往后就永远是 28 号了 —— 一个月丢三天。
    """
    if months <= 0:
        return anchor + timedelta(days=interval_days * n)

    year = anchor.year + (anchor.month - 1 + months * n) // 12
    month = (anchor.month - 1 + months * n) % 12 + 1
    return date(year, month, min(anchor.day, _days_in_month(year, month)))


def upcoming_charges(
    txns: list[Transaction],
    *,
    today: date,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
) -> list[Upcoming]:
    """已知固定扣款在未来 `horizon_days` 天内的到期日，按日期升序。

    到期日 = `last_seen` 往后推整数个周期，推到不早于今天。

    **周期按日历月算，不按天数**（理由和 `_periods_per_month` 那条一样）：月付
    订阅是「每月 5 号扣」，不是「每 31 天扣一次」。拿 31 天推，9/5 会变成 10/6 ——
    一个月漂一天，漂到年底差半个月。季付同理。

    金额用**当前单价**，和 `Subscription.monthly` 同一口径：是「按现在这个价
    接下来会扣多少」，不是「过去扣了多少」（NETFLIX 涨过价，两者不一样）。
    """
    if horizon_days <= 0:
        raise ValueError(f"horizon_days 要是正数，收到 {horizon_days}")

    end = today + timedelta(days=horizon_days)
    found: list[Upcoming] = []

    for merchant, rows in _recurring_charges(txns).items():
        gaps = [(b.date - a.date).days for a, b in zip(rows, rows[1:])]
        if not gaps:
            continue

        interval = int(round(median(gaps)))
        if interval <= 0:
            continue
        months = _cycle_months(rows)

        anchor = rows[-1].date
        n = 1
        while n <= _MAX_CYCLES and _nth_charge(
            anchor, months=months, interval_days=interval, n=n
        ) < today:
            n += 1
        if n > _MAX_CYCLES:
            continue

        when = _nth_charge(anchor, months=months, interval_days=interval, n=n)
        if when > end:
            continue

        found.append(
            Upcoming(
                merchant=merchant,
                amount=rows[-1].amount,
                next_date=when,
                interval_days=interval,
                occurrences=len(rows),
            )
        )

    found.sort(key=lambda item: (item.next_date, item.merchant))
    return found
