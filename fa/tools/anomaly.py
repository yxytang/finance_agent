"""把异常检测包成工具。

`find_anomalies` 和 `list_subscriptions` 分开，不只是数量问题：

「有没有乱扣钱」是一个**怀疑**，用户要的是「帮我查查」。而「我订阅了哪些
东西」是一个**清点**，用户要的是完整清单。把清点塞进「异常」里，模型就会
在用户只是想知道订阅列表时，只报那些「看起来不对劲」的 —— 而幽灵订阅的
定义恰恰是「看起来很正常，但你已经忘了它」。

按年化金额排序也是为这个：9.99 一个月没有痛感，119.88 一年就有。
"""

from langchain_core.tools import BaseTool, tool

from fa.anomalies import (
    find_duplicates,
    find_outliers,
    find_price_increases,
    find_subscriptions,
)
from fa.config import MAX_ANOMALY_ITEMS
from fa.models import ZERO, Transaction
from fa.tools._util import Bill, truncate

KINDS = ("all", "duplicates", "price_increase", "outlier")


def _render_duplicates(txns) -> list[str]:
    found = find_duplicates(txns)
    if not found:
        return ["重复扣款：没有。"]

    lines = [f"重复扣款：{len(found)} 组"]
    for dup in found[:MAX_ANOMALY_ITEMS]:
        when = "、".join(d.isoformat() for d in dup.dates)
        lines.append(
            f"  {dup.merchant}  {dup.amount:,.2f} × {dup.times} 次  {when}"
        )
        lines.append(f"      交易号：{'、'.join(dup.txn_ids)}")
    if len(found) > MAX_ANOMALY_ITEMS:
        lines.append(f"  …另有 {len(found) - MAX_ANOMALY_ITEMS} 组未列出")
    return lines


def _render_price_increases(txns) -> list[str]:
    found = find_price_increases(txns)
    if not found:
        return ["订阅涨价：没有。"]

    lines = [f"订阅涨价：{len(found)} 个"]
    for item in found[:MAX_ANOMALY_ITEMS]:
        lines.append(
            f"  {item.merchant}  {item.was:,.2f} → {item.now:,.2f}"
            f"（{item.ratio} 倍），{item.since.isoformat()} 起，"
            f"一年多花 {item.yearly_extra:,.2f}"
        )
    return lines


def _render_outliers(txns, group_by: str) -> list[str]:
    try:
        found = find_outliers(txns, group_by=group_by)
    except ValueError as exc:
        return [f"异常大额：查不了 —— {exc}"]

    if not found:
        return [f"异常大额：没有（按{'类目' if group_by == 'category' else '商户'}分组）。"]

    lines = [f"异常大额：{len(found)} 笔"]
    for item in found[:MAX_ANOMALY_ITEMS]:
        lines.append(
            f"  {item.txn_id}  {item.merchant}  {item.amount:,.2f}"
            f"（{item.group} 的基准是 {item.threshold:,.2f}，这是它的 {item.ratio} 倍）"
        )
    if len(found) > MAX_ANOMALY_ITEMS:
        lines.append(f"  …另有 {len(found) - MAX_ANOMALY_ITEMS} 笔未列出")
    return lines


def build_anomaly_tools(get_bill: Bill) -> list[BaseTool]:
    @tool
    def find_anomalies(kind: str = "all", group_by: str = "merchant") -> str:
        """查账单里可能有问题的地方。用户问「有没有乱扣钱」「账单正常吗」时用它。

        kind 选查哪一类，默认 all 一次全查（**不确定用户想问什么就用默认值**）：
          duplicates      同一商户同一天被扣了两次（真实的双重扣款是两笔不同
                          交易号的同额交易）
          price_increase  固定价格的周期性扣款涨价了 —— 会算出一年多花多少
          outlier         某笔金额远超同一组的其他笔
          all             以上三项

        group_by 只在 kind=outlier 时有用，决定「和谁比」：
          merchant（默认）比同商户的其他笔。**任何数据上都能用。**
          category 比同类目的其他笔。判断更准（3800 的笔记本在购物类正常、
                   在咖啡类就是数据错误），但**必须先把交易分类过**，
                   否则所有交易挤在一组，会标出一堆噪音。

        注意 kind=outlier 报的是「离群」不是「错误」：一笔年度车险在满是打车费
        的类目里本来就会离群，那是正常的。报出来是让用户自己判断，不是替他下结论。
        """
        if kind not in KINDS:
            return f"没有 {kind!r} 这种 kind。可选：{'、'.join(KINDS)}"

        txns = get_bill()
        blocks: list[list[str]] = []
        if kind in ("all", "duplicates"):
            blocks.append(_render_duplicates(txns))
        if kind in ("all", "price_increase"):
            blocks.append(_render_price_increases(txns))
        if kind in ("all", "outlier"):
            blocks.append(_render_outliers(txns, group_by))

        return truncate("\n\n".join("\n".join(b) for b in blocks))

    @tool
    def list_subscriptions() -> str:
        """列出所有固定价格的周期性扣款（房租、话费、各种订阅），按**年化金额**排序。

        用户问「我订阅了哪些」「订阅一共花多少钱」「有没有忘了取消的」时用它。

        为什么按年化排而不是按月：这个清单的用处就在于戳破「感觉不到」——
        9.99 一个月没痛感，119.88 一年就有。**幽灵订阅之所以是幽灵，
        正是因为按月看它太小。**

        金额是**按当前单价折算的未来一年**，不是过去一年实际花了多少（涨过价的
        订阅两者不一样）。要看实际发生额就调 query_transactions 查过去 12 个月。
        """
        found = find_subscriptions(list(get_bill()))
        if not found:
            return "没有找到固定价格的周期性扣款。"

        total = sum((s.yearly for s in found), ZERO)
        lines = [f"周期性扣款 {len(found)} 个，合计每年 {total:,.2f}", ""]

        width = max(len(s.merchant) for s in found)
        for sub in found:
            lines.append(
                f"  {sub.merchant:<{width}}  {sub.amount:>10,.2f}/月  "
                f"年 {sub.yearly:>11,.2f}  （{sub.occurrences} 次，"
                f"{sub.first_seen.isoformat()} 起）"
            )
        return truncate("\n".join(lines))

    return [find_anomalies, list_subscriptions]
