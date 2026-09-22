"""预测工具 —— 把 fa/forecast.py 算出来的东西讲给模型听。

这层只做两件事：把参数转成区间、把结构化结果渲染成文字。所有数字都来自
`fa/forecast.py`（那里是纯函数、可单独测），**这一层不做任何算术** ——
「LLM 绝不做算术」这条铁律对工具层同样成立。

## 输出为什么这么啰嗦

prompt 铁律 3 要求报差值时把两个原始数字一起写出来。预测更严格：**每一个推断
值都必须挨着它的依据**。用户看到「预计 9,457」没法核对，看到「9/1–9/22 花了
6,935.60，22 天，全月 30 天」才能自己验算。

这也是 skill `forecast` 正文里那条规矩的落点：观测和推断必须分开写，不能让一个
推测看起来像账单里的事实。
"""

from datetime import date, timedelta
from typing import Callable

from langchain_core.tools import BaseTool, tool

from fa.forecast import (
    DEFAULT_HORIZON_DAYS,
    month_bounds,
    project_period,
)
from fa.forecast import upcoming_charges as _upcoming_charges
from fa.models import ZERO
from fa.tools._util import Bill, truncate


def _render_projection(p) -> str:
    """把一个 Projection 渲染出来。**依据和推断值必须挨着。**"""
    if not p.is_partial:
        return (
            f"{p.period_start.isoformat()} ~ {p.period_end.isoformat()} 已经过完了，"
            f"不用推算：\n\n"
            f"  实际合计  {p.observed:,.2f}   （{p.count} 笔，{p.total_days} 天）\n\n"
            "这段时间的数据是完整的，报这个数就行。"
        )

    return (
        f"{p.period_start.isoformat()} ~ {p.period_end.isoformat()}"
        f"（全月 {p.total_days} 天）**还没过完**，账单只到 {p.observed_through.isoformat()}。\n"
        "\n"
        f"  已发生    {p.observed:>12,.2f}   "
        f"（{p.period_start.isoformat()} ~ {p.observed_through.isoformat()}，"
        f"{p.observed_days} 天，{p.count} 笔）\n"
        f"  日均      {p.daily_rate:>12,.2f}\n"
        f"  全月推算  {p.projected:>12,.2f}\n"
        "\n"
        f"**{p.projected:,.2f} 是推算值，不是账单里的数。** 依据只有一条：已发生的 "
        f"{p.observed:,.2f} ÷ {p.observed_days} 天 = {p.daily_rate:,.2f}/天，再乘 "
        f"{p.total_days} 天。月末如果集中出现大额支出（或者没有），实际会和它不一样。\n"
        "\n"
        "回答时请把**已发生的数和推算的数分开说**，别让推算看起来像实际发生额。"
    )


def _render_upcoming(found, *, today: date, horizon_days: int) -> str:
    end = today + timedelta(days=horizon_days)
    if not found:
        return (
            f"未来 {horizon_days} 天（{today.isoformat()} ~ {end.isoformat()}）"
            "没有已知的固定扣款。"
        )

    total = sum((item.amount for item in found), ZERO)
    width = max(len(item.merchant) for item in found)
    lines = [
        f"未来 {horizon_days} 天（{today.isoformat()} ~ {end.isoformat()}）"
        f"已知的固定扣款 {len(found)} 笔，合计 {total:,.2f}：",
        "",
    ]
    for item in found:
        lines.append(
            f"  {item.next_date.isoformat()}  {item.merchant:<{width}}  "
            f"{item.amount:>10,.2f}"
        )
    lines += [
        "",
        "**每个日期都是推算的，不是账单里记着的。** 账单里没有「下次扣款日」这个"
        "字段，它是拿「上次扣款日 + 固定周期」外推出来的。订阅可能被取消或改价，"
        "以实际账单为准。",
        "",
        "金额用的是**当前单价**（涨过价的订阅推的是新价），意思和 list_subscriptions "
        "的年化口径一致。",
    ]
    return "\n".join(lines)


def build_forecast_tools(
    get_bill: Bill,
    today: Callable[[], date] = date.today,
) -> list[BaseTool]:
    """构造预测工具。

    `today` 做成可注入的，和账单一样：这个工具的输出**强烈依赖今天是哪天**
    （推算的分母就是它），测试不给固定日期就钉不住任何东西。
    """

    @tool
    def project_spending(month: str = "") -> str:
        """推算一个**还没过完**的月份大概会花多少。用户问「这个月会花多少」
        「月底大概超多少」「照这样下去一个月要多少」时用它。

        month 写成 "2026-09" 这种形式。**留空就是当前月，这也是最常见的用法** ——
        不确定用户指哪个月时先留空，或者直接问清楚。

        三个不要用的场合：

          · 月份已经过完 → 不需要推算，直接调 query_transactions 拿实际值。
          · 用户问的是**已有数据**（「8 月花了多少」）→ 用 query_transactions。
          · 数据还没覆盖到那个月 → 会报错，不是猜一个数给你。

        返回里同时有**已发生额和推算额**。回答时必须分开说：推算值要带上它的
        依据（已发生多少、过了几天、全月几天），否则用户没法核对，而这个项目的
        全部价值就在于可核对。
        """
        try:
            start, end = month_bounds(month.strip() or today().strftime("%Y-%m"))
        except ValueError:
            return f"month 要写成 2026-09 这种形式，收到了 {month!r}。"

        try:
            projection = project_period(
                list(get_bill()),
                period_start=start,
                period_end=end,
                today=today(),
            )
        except ValueError as exc:
            return f"算不了：{exc}"

        return truncate(_render_projection(projection))

    @tool
    def upcoming_charges(days: int = DEFAULT_HORIZON_DAYS) -> str:
        """列出未来一段时间内**已知会扣的固定支出**（房租、话费、各种订阅）。
        用户问「接下来有什么要扣的」「下个月固定支出多少」「最近会扣哪些」时用它。

        days 是往前看多少天，默认 30（够覆盖所有月付订阅一次）。想看到季度付的
        订阅就传 90 以上。

        用来看**已知的**固定支出。用户问「下个月一共会花多少」（包含吃饭购物这类
        浮动消费）时，这个工具只答得了固定部分，浮动部分得用 project_spending 推 ——
        两者加起来才是全貌，但要说清楚哪部分是已知的、哪部分是推的。

        返回的日期是**外推出来的**，不是账单里记着的。回答时必须标明这一点。
        """
        if days <= 0:
            return f"days 要是正数，收到 {days}。"

        try:
            found = _upcoming_charges(list(get_bill()), today=today(), horizon_days=days)
        except ValueError as exc:
            return f"算不了：{exc}"

        return truncate(_render_upcoming(found, today=today(), horizon_days=days))

    return [project_spending, upcoming_charges]
