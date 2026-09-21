"""use_skill —— 渐进披露第二、三级的入口。

为什么二级和三级挤在**同一个**工具里：**skill 属于 agent，不属于它操作的
数据**。skill 目录挂在项目根下，agent 分析的是 `data/` 里的账单，两者没有
从属关系。把第三级的入口收在同一个工具里，就不需要让模型去猜一个和它自己
无关的路径。

代价是资源读取得由 skill 自己实现、自己守边界（见 `Skill.read_resource`）。
"""

from langchain_core.tools import BaseTool, tool

from fa import skills as skills_mod


def build_skill_tools() -> list[BaseTool]:
    @tool
    def use_skill(name: str, path: str = "") -> str:
        """**动手做任务之前，先扫一遍 system prompt 里那份 skill 清单。**

        只要有一条和当前任务沾边，就先调这个工具把它的正文读进来再开始 ——
        正文里写的是这个项目的具体做法，跳过它你会按通用习惯来，然后和项目
        约定对不上。这条比「先看看代码」还靠前：流程错了，看得再仔细也白搭。

        用法两档，不要一次读到底：

        - 读正文：use_skill(name="清单里的名字")
        - 读附带文件：use_skill(name="同一个名字", path="references/xxx.md")
          path 相对该 skill 的目录。正文末尾会列出有哪些附带文件。

        清单里只有名字和一句话描述，**不要凭名字猜正文写了什么**。
        """
        current = skills_mod.discover()
        target = skills_mod.find(current, name)
        if target is None:
            available = "、".join(s.name for s in current) or "（当前没有 skill）"
            return f"没有名为 {name!r} 的 skill。可用的是：{available}"

        if path.strip():
            return target.read_resource(path)

        body = target.body()
        listed = target.resources()
        if not listed:
            return f"# skill: {target.name}\n\n{body}"

        files = "\n".join(f"- {item}" for item in listed)
        return (
            f"# skill: {target.name}\n\n{body}\n\n"
            f"## 附带文件\n\n"
            f"这份 skill 还带了下面这些文件，需要哪个再单独读，别一次全读：\n\n"
            f"{files}\n\n"
            f'读法：use_skill(name="{target.name}", path="上面某一条")'
        )

    return [use_skill]
