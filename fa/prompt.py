"""system prompt 组装。

刻意做成**纯函数**：所有会变的东西都是显式参数，不在这里扫盘、不碰文件。
好处是能做指纹比对 —— 只有渲染结果真的变了才替换 `messages[0]`，否则每轮都改
会让 DeepSeek 的前缀缓存整段失效（前缀命中大约便宜 10 倍）。
"""

from fa.config import CATEGORIES, SKILL_LIST_BUDGET

PERSONA = """你是一个个人财务分析助手。

用户会拿自然语言问他的账单 ——「上个月外卖花了多少」「有没有重复扣款」。
你的回答必须**准确、可核对**，因为用户是拿它当真的。

## 三条铁律

1. **数字只能来自工具。** 任何时候要算钱、数笔数、按月分组，都调
   `query_transactions`。**绝不凭记忆或印象报数字。** 报「比上月多多少」这类
   差值时，必须把**两个原始数字一起写出来**（「6 月 7,034.22 → 7 月 8,234.56」），
   让用户能自己验算 —— 只给一个「+17.1%」，用户没法核对，而这个项目的全部
   价值就在于可核对。
2. **说不清就先问。** 时间范围或类目不明确时（「最近」是多久？「吃饭」算不算
   咖啡？），先问一句，不要猜。猜错的时间范围会给出一个数字上说得通、但
   答非所问的答案 —— 那种错误用户很难发现。
3. **复述你用的查询条件。** 回答时带上「2026-07 全月、类目=餐饮外卖」这样的
   限定条件，用户才能核对。一个光的数字没法验证。
"""

STYLE = """## 风格

- 中文回答，简洁直接，不要客套。
- 金额写成 `1,234.56`，支出为正、收入与退款为负。
- 涉及多个月或多个类目时，用表格比用句子清楚。
"""


def render_data_section() -> str:
    """数据概况 + 类目表。

    类目表**必须列出来**，因为它是固定枚举，而模型不知道这件事。不列的话它
    会自己编一个「伙食费」传进查询，拿到 0 笔，然后自信地告诉用户「你这个月
    没吃饭」。**一个错的枚举值不会报错，只会安静地返回空** —— 这是最难发现的
    一类错误，所以得从提示词这一层就堵住。
    """
    listed = "、".join(CATEGORIES)
    return (
        "## 数据\n\n"
        "用户的账单在 `data/transactions.csv`，由账单服务提供（用 MCP 的"
        "工具取，不要自己读文件）。\n\n"
        f"**类目是固定这 {len(CATEGORIES)} 个，不要自己发明新的**：\n\n"
        f"{listed}\n\n"
        "不确定数据的时间跨度、账户有哪些时，先查一次再回答。"
    )


def render_skills_section(skills) -> str:
    """skill 清单 —— 渐进披露的**第一级**：只有名字和描述，正文一个字不进。

    正文不进不是审美问题，是上下文预算问题。这份清单在 `messages[0]` 里，
    也就是**每一轮都在**。把 skill 正文全塞进来，几十个 skill 就能把窗口吃掉
    一大半，而其中绝大多数和当前问题毫无关系。

    超预算时**整条不列**，而不是把描述截半句 —— 半句话比没有更糟，模型会拿
    残缺信息去判断该不该用这个 skill。

    但省略是有代价的：没列出来的 skill，模型不知道它存在，也就永远不会用。
    这是一条真取舍，不是免费的优化。
    """
    if not skills:
        # 这里**一个字都不能提 use_skill**。在 forge 上踩过这个坑：提示词里
        # 写着「用某个工具去查」，而那个工具根本不在工具列表里，模型收到的
        # 是一条执行不了的命令，只好回一句「我这边没有」把问题挡回去。
        # 规则必须和事实同源 —— 没有 skill 就直说没有。
        return "## 可用的 skill\n\n（当前没有 skill。）"

    lines: list[str] = []
    used = 0
    for index, skill in enumerate(skills):
        line = f"- {skill.name}: {skill.description}"
        if used + len(line) + 1 > SKILL_LIST_BUDGET:
            lines.append(f"…另有 {len(skills) - index} 个 skill 因篇幅未列出。")
            break
        lines.append(line)
        used += len(line) + 1

    return (
        "## 可用的 skill\n\n"
        "这个项目预置了一些分析流程。开始回答之前先扫一遍：只要有一条和当前问题\n"
        "沾边，就先调 `use_skill` 把正文读进来再动手 —— 正文里通常是本项目对\n"
        "口径和格式的具体约定，跳过它容易按通用习惯来，然后和约定对不上。\n\n"
        "清单里只有名字和一句话描述，正文必须调 `use_skill` 才拿得到。\n\n"
        + "\n".join(lines)
    )


def _budget() -> int:
    """从 config **在调用时**读，不 import 快照 —— 测试要能 monkeypatch 它。"""
    from fa import config

    return config.SKILL_LIST_BUDGET


def build_system_prompt(skills) -> str:
    """拼出这一轮的 system prompt。

    `skills` 是**显式参数**而不是在这里调 `discover()`：一是不扫盘才叫纯函数，
    二是指纹门控需要「同样输入必得同样输出」才成立 —— 如果它自己去扫盘，
    门控依赖的就是一个函数体里看不见的副作用了。

    顺序固定，而且**易变的放最后**：DeepSeek 按精确 token 前缀命中缓存，
    skill 清单每轮都可能变，放最后就只让它自己那一段失效，前面的人设和数据
    说明照样命中。
    """
    return "\n\n".join(
        [
            PERSONA,
            render_data_section(),
            STYLE,
            render_skills_section(skills),
        ]
    )
