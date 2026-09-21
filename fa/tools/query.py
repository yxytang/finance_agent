"""把查询引擎包成工具。

## 这层唯一真正重要的东西是 docstring

模型选不选一个工具，几乎完全由它的描述决定。这里要传达的是一条**铁律**：
任何时候需要数字都调它，不要自己算。所以 docstring 不是文档，是提示词 ——
它和 `prompt.py` 里那三条规则说的是同一件事，但**只有写在这里的那份才会被
每个决策点读到**（system prompt 是背景，工具描述是模型挑工具时正在看的东西）。

在 forge 上实测过这个差别：把「先扫一遍 skill 清单」写在 system prompt 里，
模型连着几个任务一次都没照做；挪进工具描述立刻生效。

## 渲染放在这层，不放 query.py

`query()` 返回结构化结果，渲染成文本是这里的事。分开的好处是第八天的评测能
直接拿 `query()` 的数字当标准答案，不必去解析自己渲染出来的文本。
"""

from datetime import date

from langchain_core.tools import BaseTool, tool

from fa.config import CATEGORIES, MAX_QUERY_GROUPS
from fa.ingest import IngestError, parse_date
from fa.models import Transaction, UNCATEGORIZED
from fa.query import GROUP_LABELS, QueryError, QueryResult, query
from fa.tools._util import format_value, truncate


def render_result(result: QueryResult) -> str:
    """把结构化结果摊成模型能读的文本。"""
    f = result.filters
    lines = [f"条件：{f.describe()}", f"命中 {result.matched} 笔"]

    if not result.groups:
        # 问「点了几次」时，命中笔数就是答案本身，不必再说一遍。
        if f.agg != "count":
            lines.append(f"结果：{format_value(result.value, f.agg)}")
        return "\n".join(lines)

    lines.append(f"总计：{format_value(result.value, f.agg)}")
    lines.append("")
    lines.append(f"按{GROUP_LABELS[f.group_by]}分组（按"
                 f"{'时间' if f.group_by == 'month' else '数值'}排序）：")

    shown = result.groups[:MAX_QUERY_GROUPS]
    width = max((len(g.key) for g in shown), default=4)
    for group in shown:
        lines.append(
            f"  {group.key:<{width}}  {group.count:>4} 笔  "
            f"{format_value(group.value, f.agg):>14}"
        )
    if len(result.groups) > len(shown):
        lines.append(f"  …另有 {len(result.groups) - len(shown)} 组未列出")

    return "\n".join(lines)


def _parse_optional_date(raw: str, what: str) -> date | None:
    if not raw or not raw.strip():
        return None
    try:
        return parse_date(raw)
    except IngestError as exc:
        raise QueryError(f"{what}读不出来：{exc}") from exc


def build_query_tools(txns: tuple[Transaction, ...]) -> list[BaseTool]:
    @tool
    def describe_data() -> str:
        """看看这份账单覆盖了哪些数据。**拿不准时间范围或类目名时先调它。**

        会告诉你：账单覆盖的起止日期、有多少笔、涉及哪些账户、今天几号。
        回答「上个月」「今年」这类相对时间之前**必须**先看一眼 —— 账单的
        截止日期和今天不是一回事，数据通常落后一两个月。
        """
        if not txns:
            return "账单是空的。"

        dates = [t.date for t in txns]
        accounts = sorted({t.account for t in txns})
        categorized = sum(1 for t in txns if t.category)

        lines = [
            "账单概况",
            f"  笔数：{len(txns)}",
            f"  覆盖：{min(dates).isoformat()} ~ {max(dates).isoformat()}",
            f"  账户：{'、'.join(accounts)}",
            f"  今天是：{date.today().isoformat()}",
            "",
            f"类目就这 {len(CATEGORIES)} 个，别自己发明："
            f"{'、'.join(CATEGORIES)}",
            "（还没分类的交易查询时用「{}」）".format(UNCATEGORIZED),
        ]

        # 分类覆盖率**算出来**，不写死：它会变（第四天分类跑完就变了）。
        # 写死的话，提示词会在某一天开始撒谎 —— 而撒谎的提示词比没有提示词更糟，
        # 因为它看上去仍然可信。
        if categorized < len(txns):
            lines += [
                "",
                f"⚠️ 分类情况：{categorized} / {len(txns)} 笔已分类。"
                f"**按类目查询现在基本只会返回 0 笔** —— 那不是「这个月没花钱」，"
                f"是数据还没分类。遇到这种情况直接告诉用户，不要报 0。",
            ]

        return truncate("\n".join(lines))

    @tool
    def query_transactions(
        date_from: str = "",
        date_to: str = "",
        categories: list[str] | None = None,
        merchant_contains: str = "",
        account: str = "",
        agg: str = "sum",
        group_by: str = "none",
    ) -> str:
        """查账单。**任何时候需要数字都必须调它，绝不自己心算。**

        用户问的金额、笔数、排名、趋势，答案全部来自这里。你自己算出来的数字
        哪怕碰巧对了，用户也没法核对 —— 而这个项目的全部价值就在于可核对。

        几个参数：
          agg       sum 合计 / count 笔数 / avg 每笔平均 / max 单笔最大 / min 单笔最小
          group_by  none 只要总数 / month 按月 / category 按类目 / merchant 按商户
          date_from, date_to   YYYY-MM-DD，**两端都包含**（问「7 月」要写
                   2026-07-01 到 2026-07-31，写 08-01 会把 7 月 31 号漏掉）

        范围或类目不明确时先问用户，别猜：猜错的时间范围会给出一个数字上说得通、
        但答非所问的答案，而用户很难发现。不确定账单覆盖到哪天，先调 describe_data。

        金额约定：**支出为正，收入和退款为负**。所以「合计」是净支出。
        """
        # 一笔都没分类时按类目查，会安静地返回 0 —— 然后模型会自信地告诉用户
        # 「你这个月没吃饭」。**这和「传了一个不存在的类目名」是同一类错误**：
        # 输出看起来完全正常，没有任何东西可以察觉。所以在这里拦下来。
        #
        # 注意判据是「一笔都没分类」而不是「有部分没分类」：部分分类是正常状态，
        # 那种情况下按类目查出来的数字是真实的下界，可以照常报。
        if categories and not any(t.category for t in txns):
            return (
                "查不了：这笔数据**一笔都还没分类**，按类目查询只会返回 0 笔，"
                "而那不等于「没花这个钱」。\n"
                "可以改用 merchant_contains 按商户名查（比如「STARBUCKS」），"
                "或者 group_by='merchant' 先看看钱都花在哪几家了。"
            )

        try:
            result = query(
                list(txns),
                date_from=_parse_optional_date(date_from, "起始日期"),
                date_to=_parse_optional_date(date_to, "结束日期"),
                categories=categories,
                merchant_contains=merchant_contains or None,
                account=account or None,
                agg=agg,
                group_by=group_by,
            )
        except QueryError as exc:
            return str(exc)

        return truncate(render_result(result))

    return [describe_data, query_transactions]
