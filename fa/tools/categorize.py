"""纠正分类 —— 唯一一个「用户教的东西」会改变系统行为的入口。

它**同时写两个地方**，这不是重复：

- **缓存**（`data/category_cache.json`）—— 让这个商户从现在起归对类
- **记忆**（`memory/facts.md`）—— 让 agent 下次答得出「为什么这么归」

只写缓存的话，用户下次问「你为什么把 target 算成购物」agent 答不上来，
只能现编一个理由；只写记忆的话，得等下一次分类批处理跑完才生效。

这是第一天留的那个 `confirm` 回调第一次派上用场。它会改变**以后所有查询的
结果**，所以必须问一句。读操作不问（无脑按 y 的确认等于没有确认），
但「以后都按这个来」这种改变行为的事，问一句是应该的。
"""

from datetime import date

from langchain_core.tools import BaseTool, tool

from fa.categorize import CategoryCache
from fa.config import CATEGORIES
from fa.memory import Memory, add
from fa.normalize import normalize
from fa.permissions import Confirm
from fa.tools._util import reload_bill


def _paths():
    """从 config **在调用时**读。测试要能把缓存和记忆换到临时目录。"""
    from fa import config

    return config.CATEGORY_CACHE, config.MEMORY_FILE


def build_categorize_tools(confirm: Confirm | None = None) -> list[BaseTool]:
    @tool
    def correct_category(merchant: str, category: str) -> str:
        """用户说某个商户的分类归错了时用它纠正。**纠正之后一直生效。**

        merchant 传**缓存里的那个名字**（归一化之后的商户名，形如 `target`、
        `uber trip`）。不确定它叫什么不要紧：报错信息里会列出所有可纠正的商户。

        什么时候用：用户说「X 应该算 Y」「X 归错了」「以后 X 都算 Y」。

        这个操作会改变以后所有查询的结果，所以会先问用户一句。
        """
        if category not in CATEGORIES:
            return (
                f"没有「{category}」这个类目。可选的只有：{'、'.join(CATEGORIES)}"
            )

        cache_path, memory_path = _paths()
        cache = CategoryCache.load(cache_path)
        key = normalize(merchant)

        if key not in cache.entries:
            # 纠正的前提是这个商户已经被判过类。没判过说明它还没进过 LLM 那层，
            # 先跑一次 `python -m data.categorize` 才有得纠正。
            known = "、".join(sorted(cache.entries)) or "（缓存是空的）"
            return (
                f"缓存里没有「{key}」这个商户，没法纠正。\n"
                f"可纠正的有：{known}\n"
                f"（如果你说的是别的名字，从上面挑一个原样传进来。）"
            )

        before = cache.entries[key]
        if before == category:
            return f"「{key}」本来就是「{category}」，不用改。"

        if confirm is not None and not confirm(
            f"把「{key}」的类目从「{before}」改成「{category}」（以后一直生效）"
        ):
            return "用户拒绝了这次修改，没有改动。"

        cache.set(key, category)
        cache.save()

        add(
            memory_path,
            Memory(
                text=f"{key} 归「{category}」而不是「{before}」",
                kind="纠正",
                created=date.today(),
            ),
        )

        # 纠正完必须重读账单 —— 不清缓存的话，agent 手里的账还是旧的，
        # 「纠正了却没变化」比根本不能纠正更让人困惑。
        reload_bill()

        return (
            f"已改：「{key}」的类目从「{before}」改成「{category}」。\n"
            f"写进了分类缓存（立刻生效）和长期记忆（以后答得出为什么）。"
        )

    return [correct_category]
