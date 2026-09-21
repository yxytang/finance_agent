"""把 markdown 切成检索用的块。

## 按标题层级切，不按字数切

按字数硬切是最省事的做法，也是最糟的：一刀下去正好落在「专项附加扣除」这个
小标题和它的正文之间，那个块就同时丢了标题和上下文 —— 而标题恰恰是这段话在讲
什么的最强信号。

所以按**标题**切：每个标题开启一个新块，块的边界就是语义的边界。

## 每块必须带上 breadcrumb

    "个税基础 > 专项附加扣除 > 住房贷款利息"

为什么这条最重要：用户问「房贷利息能扣多少」，正文里可能写着「每月 1000 元标准
定额扣除」—— 一个字都没提「房贷」，也没提「个税」。**光靠正文，这个问题永远
检索不到。**

而 breadcrumb 里那三级标题把「这段在讲房贷利息能抵多少个税」完整地说清楚了。
把它拼进被检索的文本里，命中率是另一个量级。

同一个道理在代码检索里也成立（切块要带上文件路径 + 类名 + 函数签名），
只是这里换成了标题层级。
"""

import re
from dataclasses import dataclass
from pathlib import Path

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")

# 太短的块多半是只有标题没有内容的占位，进去只会给检索添噪音。
MIN_CHUNK_CHARS = 30

# 单个块的长度上限。超了就按空行（自然段）再切一刀，
# 免得一个超长小节挤成一个巨大的块、把检索精度拉垮。
MAX_CHUNK_CHARS = 900

CONTINUED = "（续）"


@dataclass(frozen=True)
class Chunk:
    """一个可检索的块。

    `breadcrumb` 和 `text` 分开存，理由是要**分别检索**：标题命中和正文命中
    是两种不同的证据强度（标题命中几乎一定相关），所以它们是两路检索通道，
    而不是拼成一坨。见 `hybrid.py`。
    """

    source: str  # 相对 knowledge/ 的路径
    breadcrumb: str  # "个税基础 > 专项附加扣除 > 住房贷款利息"
    text: str  # 正文（不含标题行）

    @property
    def title(self) -> str:
        """最内层的那级标题。"""
        return self.breadcrumb.split(" > ")[-1]

    @property
    def display(self) -> str:
        return f"[{self.source}] {self.breadcrumb}"

    def __str__(self) -> str:
        return f"{self.display}\n{self.text}"


def _hard_split(paragraph: str) -> list[str]:
    """单个自然段本身就超长时，硬切。

    不硬切的话这个段会以超限的状态留在索引里 —— 而分块上限存在的**全部意义**
    就是不出现这种块。硬切会切断句子，但一个被切断的块仍然能被检索到，
    一个超大的块则会把检索精度整体拉垮。
    """
    if len(paragraph) <= MAX_CHUNK_CHARS:
        return [paragraph]
    return [
        paragraph[i : i + MAX_CHUNK_CHARS]
        for i in range(0, len(paragraph), MAX_CHUNK_CHARS)
    ]


def _split_long(text: str) -> list[str]:
    """超长的正文按自然段再切几刀，段落本身超长就硬切。"""
    if len(text) <= MAX_CHUNK_CHARS:
        return [text]

    parts: list[str] = []
    current = ""
    for paragraph in text.split("\n\n"):
        for piece in _hard_split(paragraph):
            candidate = f"{current}\n\n{piece}".strip() if current else piece
            if len(candidate) > MAX_CHUNK_CHARS and current:
                parts.append(current)
                current = piece
            else:
                current = candidate
    if current:
        parts.append(current)
    return parts


def chunk_markdown(text: str, source: str) -> list[Chunk]:
    """把一份 markdown 切成块。

    没有标题的文档整份作为一个块 —— 总比什么都检索不到强，而且它会拿到文件名
    当 breadcrumb。
    """
    lines = text.splitlines()
    # 标题栈：[(级别, 标题文本)]
    stack: list[tuple[int, str]] = []
    # 当前块收集到的正文行
    body: list[str] = []
    chunks: list[Chunk] = []

    def flush() -> None:
        content = "\n".join(body).strip()
        body.clear()
        if len(content) < MIN_CHUNK_CHARS:
            return
        breadcrumb = " > ".join([source, *(title for _, title in stack)])
        pieces = _split_long(content)
        for index, piece in enumerate(pieces):
            chunks.append(
                Chunk(
                    source=source,
                    breadcrumb=breadcrumb if index == 0 else f"{breadcrumb} {CONTINUED}",
                    text=piece,
                )
            )

    for line in lines:
        match = _HEADING.match(line)
        if not match:
            body.append(line)
            continue

        # 遇到新标题：先把上一块收掉，再把栈弹到它上面
        flush()
        level = len(match.group(1))
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, match.group(2).strip()))

    flush()
    return chunks


def load_corpus(root: Path) -> list[Chunk]:
    """读整个语料目录。

    路径在工作区之外也照读不误 —— 这是内部管线，不是模型能传路径进来的入口。
    """
    if not root.is_dir():
        return []

    chunks: list[Chunk] = []
    for path in sorted(root.rglob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        chunks.extend(chunk_markdown(text, path.relative_to(root).as_posix()))
    return chunks
