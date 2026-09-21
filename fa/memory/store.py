"""跨会话记忆的存取。

**它不是黑盒，就是一个 markdown 文件**（`memory/facts.md`）：

    # 记忆
    - 2026-09-21 [偏好] 我的房租归「住房」类
    - 2026-09-21 [纠正] target 应该归「购物」不是「超市」

直接编辑就能增删，删一行就是删一条。做成可读文件不是因为偷懒：**记忆出错的
时候用户得能自己修**。存在数据库里、要写脚本才能改的话，一条记错的偏好会一直
跟着用户，而他毫无办法。

为什么是「一行一条」而不是 markdown 分节：分节好看但难解析，而这里真正需要的是
**机器和人都能可靠地增删单条**，不是好看的排版。
"""

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

KINDS = ("偏好", "事实", "纠正")

HEADER = """# 记忆

这个文件是 agent 的跨会话记忆。**直接编辑它就可以增删** —— 删掉一行就是删掉
一条记忆，改一行就是改一条。每行的格式：

    - 日期 [类型] 内容

类型只有三种：偏好、事实、纠正。
"""

_LINE = re.compile(r"^-\s*(?:(\d{4}-\d{2}-\d{2})\s*)?\[([^\]]+)\]\s*(.+?)\s*$")


@dataclass(frozen=True)
class Memory:
    text: str
    kind: str = "事实"
    created: date = date(1970, 1, 1)

    def line(self) -> str:
        return f"- {self.created.isoformat()} [{self.kind}] {self.text}"


def parse(content: str) -> list[Memory]:
    """解析整个文件。认不出的行**跳过，不报错** —— 用户手工编辑时多写一行
    注释、或者写错个括号，不该让整个记忆功能瘫痪。"""
    found: list[Memory] = []
    for raw in content.splitlines():
        match = _LINE.match(raw.strip())
        if not match:
            continue
        stamp, kind, text = match.groups()
        found.append(
            Memory(
                text=text.strip(),
                kind=kind.strip(),
                created=date.fromisoformat(stamp) if stamp else date(1970, 1, 1),
            )
        )
    return found


def render(memories: list[Memory]) -> str:
    body = "\n".join(m.line() for m in memories)
    return f"{HEADER}\n{body}\n" if body else f"{HEADER}\n"


def load(path: Path) -> list[Memory]:
    if not path.is_file():
        return []
    try:
        return parse(path.read_text(encoding="utf-8"))
    except OSError:
        return []


def save(path: Path, memories: list[Memory]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(memories), encoding="utf-8")


def add(path: Path, memory: Memory) -> list[Memory]:
    """加一条。**内容完全相同的就不重复加** —— 用户连着说两遍「记住我住北京」
    不该变成两条记忆，那会让注入的 prompt 里出现重复段落。"""
    memories = load(path)
    if any(m.text == memory.text for m in memories):
        return memories
    memories.append(memory)
    save(path, memories)
    return memories


def forget(path: Path, index: int) -> Memory | None:
    """按序号删一条（序号是 `/memory` 列出来的那个，从 1 数）。

    越界返回 None 而不是抛异常 —— 这个操作是给人用的，输错一个数字不该炸。
    """
    memories = load(path)
    if not 1 <= index <= len(memories):
        return None
    removed = memories.pop(index - 1)
    save(path, memories)
    return removed
