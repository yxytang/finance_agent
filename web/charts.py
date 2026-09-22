"""从工具结果里抠出可以画成图的数据。

## 为什么这一层是解析文本

工具返回的是给**模型**看的字符串（那是它们该做的），而图表是给**人**看的。
两者中间需要一个转换。

理想情况下工具会直接给出结构化的东西。但工具层的约定是 `-> str`（Day 1 定的，
为了让模型读得顺），改掉它会让所有工具和它们的测试一起动。所以在这里加一层。

## 解析自己渲染的输出是危险的 —— 除非有测试钉住它

渲染格式一改，解析就**静默地返回空**：图表不出现，控制台干净，没有任何报错。
用户看到的是「这个回答没有图」，而不是「图表坏了」。

所以 `tests/test_web.py` 里有一条测试拿**真实的渲染器**跑一遍，再把输出解回来，
断言数字和顺序都对得上。格式动了它会红，而不是让图表悄悄消失。

这和 Day 5 那条教训是同一条：**解析自己的输出可以，但必须有一条闭环的测试。**
"""

import re

# 分组表头。`render_result` 里是：
#   按类目分组（按数值排序）：
#   按月份分组（按时间排序）：
_HEADER = re.compile(r"按(.+?)分组（按(时间|数值)排序）：")

# 一行分组。渲染格式是 `  {key:<width}  {count:>4} 笔  {value:>14}`。
#
# 用「两个以上空格」当分隔符：键（类目名、商户名、月份）里不会有连续两个空格，
# 而渲染用的填充恰好保证了对齐。取值那部分可能是金额也可能是「308 笔」
# （agg=count 时），所以宽松地捕获整段再单独解析。
_ROW = re.compile(r"^ {2}(.+?) {2,}(\d[\d,]*) 笔 {2,}(.+?) *$")

_MONEY = re.compile(r"^-?[\d,]+(\.\d+)?$")


def _number(text: str) -> float | None:
    """把「12,057.34」或「308 笔」变成数。认不出返回 None。"""
    cleaned = text.strip()
    if cleaned.endswith("笔"):
        cleaned = cleaned[:-1].strip()
    if not _MONEY.match(cleaned):
        return None
    try:
        return float(cleaned.replace(",", ""))
    except ValueError:
        return None


def extract_series(content: str) -> dict | None:
    """从一段工具结果里抠出分组序列。抠不出来返回 None（不是抛异常）。

    返回 `{"group": "类目", "order": "数值", "rows": [...]}`。

    **顺序照原样保留**，不重排 —— 渲染时已经按语义排好了（月份按时间升序、
    排行榜按数值降序），重排会把那个语义弄丢。而柱状图的横轴顺序是有意义的。
    """
    match = _HEADER.search(content)
    if not match:
        return None

    rows = []
    for line in content.splitlines():
        row = _ROW.match(line)
        if not row:
            continue
        label, count, raw_value = row.groups()

        value = _number(raw_value)
        if value is None:
            # 一整行解析不出数就整块放弃：半张图比没有图更糟，
            # 因为它看起来是对的。
            return None

        rows.append({"label": label.strip(), "count": int(count.replace(",", "")), "value": value})

    # 一个块都不成组的，不该出图。
    if len(rows) < 2:
        return None

    return {"group": match.group(1), "order": match.group(2), "rows": rows}
