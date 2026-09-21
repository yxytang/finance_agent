"""跨会话记忆。

分成两个模块，因为它们是两个完全不同的问题：

- `store` —— 怎么存。简单，一个可读可编辑的 markdown 文件。
- `extract` —— **什么时候该记**。难得多，也是真正决定这个功能好不好用的地方。

把这两件事分开，是为了让「写入策略」能被单独讨论和单独测试 ——
它才是这里唯一有设计含量的部分。
"""

from fa.memory.store import Memory, add, forget, load, parse, render, save

__all__ = ["Memory", "add", "forget", "load", "parse", "render", "save"]
