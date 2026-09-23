"""把父块再切成子块 —— **只用于向量检索**。

## 为什么单独一个模块

`chunk.py` 是**冻结**的。那边的分块方式决定了 BM25 的每一路排名，动它一下，
`corpus_fingerprint` 变、整个索引失效、**基线数字整体位移** —— 而这个实验的
全部意义就是「加了向量之后数字怎么变」，基线一动就没法归因了。

所以子块单独一条路：`chunk.py` 一行不改，父块照旧；这里的东西只喂给向量。

## 为什么要父子块

讲的是「embedding 的单位」和「喂给模型的单位」可以不一样：

    · 子块小 → 向量更准（一句话的向量比一段话的向量更能表达它在讲什么）
    · 父块大 → 模型看到的上下文更全（光给一句话，模型判断不了它在讲什么）

所以检索在子块上做，返回的时候回到父块。

## 但在这个语料上它近乎无操作 —— 别粉饰

60 个父块，均值 174 字符、最长 398，而 `MAX_CHUNK_CHARS` 是 900（从来没生效过）。
所以大部分父块只会切出**一个等于自己的子块**。实测（Stage 0 探针）也印证了：
**嵌父块就能拿到 4/5 的换说法命中率，父子块没有额外贡献。**

它是**面向未来的结构** —— 语料长到几千字一节的时候它才有用。现在建它是因为
用户明确要求，而不是因为它在这个尺度上提高了数字。报告里要如实写。

## 递归的意思

分隔符从粗到细：空行 → 换行 → 句号 → 分号 → 逗号 → 硬切。每一步只在「上一级
切完还是超长」时才降级。这样尽量落在语义边界上，而不是切在词中间。

**分隔符留在前一段的末尾**，所以拼回去就是原文（除了被丢掉的纯空白碎片）。
这条性质有测试钉着 —— 它是「切分没有丢内容」的唯一保证。
"""

CHILD_MAX_CHARS = 200

# 从粗到细。最后的硬切不在这个表里，它是兜底。
SEPARATORS = ("\n\n", "\n", "。", "；", "，")


def _split_after(text: str, sep: str) -> list[str]:
    """按 `sep` 切，**把 sep 留在前一段末尾**。

    留着是为了能拼回去：`"".join(_split_after(t, s)) == t`。切成「不含分隔符」
    的片段看着更干净，但那样就再也证明不了切分没丢东西了。
    """
    if sep not in text:
        return [text]

    parts: list[str] = []
    start = 0
    while True:
        index = text.find(sep, start)
        if index == -1:
            break
        parts.append(text[start : index + len(sep)])
        start = index + len(sep)
    if start < len(text):
        parts.append(text[start:])
    return parts


def _recursive(text: str, max_chars: int, separators: tuple[str, ...]) -> list[str]:
    """递归切分。返回的片段拼起来 == 原文。"""
    if len(text) <= max_chars:
        return [text]

    if not separators:
        # 分隔符用光了还是超长 —— 硬切。切在词中间不好，但比留一个超长片段好。
        return [text[i : i + max_chars] for i in range(0, len(text), max_chars)]

    parts = _split_after(text, separators[0])
    if len(parts) == 1:
        # 这个分隔符在文本里不存在，降级到下一个
        return _recursive(text, max_chars, separators[1:])

    # 贪心合并相邻片段到上限以内 —— 不是为了「正好塞满」，而是别切得太碎：
    # 两个 80 字的半句合成 160 字，比两个各自独立的片段更能表达完整意思。
    merged: list[str] = []
    current = ""
    for part in parts:
        if current and len(current) + len(part) > max_chars:
            merged.append(current)
            current = part
        else:
            current += part
    if current:
        merged.append(current)

    # 合并后仍然超长的（单个片段本身就比上限长），用更细的分隔符再切
    out: list[str] = []
    for piece in merged:
        if len(piece) <= max_chars:
            out.append(piece)
        else:
            out.extend(_recursive(piece, max_chars, separators[1:]))
    return out


def children_of(text: str, *, max_chars: int = CHILD_MAX_CHARS) -> list[str]:
    """把一个父块的正文切成子块。

    纯空白碎片会被丢掉 —— 它们嵌成向量只是噪音，而检索的时候没有人会想
    命中一段空行。**代价是拼不回原文**，所以丢掉的那部分一定得是纯空白。
    """
    if not text.strip():
        return []
    return [piece for piece in _recursive(text, max_chars, SEPARATORS) if piece.strip()]
