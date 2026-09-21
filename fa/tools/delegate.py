"""把子 agent 包成工具。

这个工具是整个项目里**唯一一个「开不开」由模型自己判断**的能力。别的地方
我们都把决策写死在代码里（什么时候分类、什么时候检索），这里不行 ——
因为「这个子任务的中间过程有没有价值」取决于主上下文里已经有什么，
而那是模型才知道的事。

所以 docstring 要写清楚**两个方向**：什么时候该用，和什么时候别用。
只写「什么时候该用」，模型会把它当成万能钥匙，每个问题都先派一个子助手 ——
而那样比直接查更慢、还多一次调用。
"""

from langchain_core.tools import BaseTool, tool

from fa.tools._util import Bill, truncate


def build_delegate_tools(get_bill: Bill) -> list[BaseTool]:
    @tool
    def investigate(question: str) -> str:
        """派一个子助手去查一件**具体的、孤立的事**，只把结论拿回来。

        **什么时候用它**：一件事要查很多次才有结论，而你只要结论、不要过程。
        比如「把餐饮这一类过去 12 个月逐月拉出来看趋势」「把所有固定扣款挨个
        核对一遍」「每个类目各自的异常情况分别是什么」。
        这类任务中间会攒出十几条查询结果，而那些结果对最终答案没有贡献，
        留在对话里只会一直占地方。

        **什么时候别用**：
        - 结果**本身就是**答案的（「把这个月超过 500 的几笔列出来」）——
          隔离之后信息反而丢了，你要的就是那些明细
        - 一两步就能查完的 —— 起一个子助手比直接查更慢
        - 需要改东西的（纠正分类、记东西）—— 子助手**只能读**，做不到

        `question` 必须**自足**：子助手看不到你和用户的对话，也看不到你之前
        查过什么。时间范围、类目、口径、要什么格式，全都要写进去。
        写得含糊它就只能猜，而猜出来的结论拿回来你也没法判断对不对。
        """
        if not question.strip():
            return "要说清楚让子助手查什么。"

        # 延迟导入：workflow 要 import tools（拿工具白名单），tools 要 import
        # 这个模块（挂 investigate），这个模块又要用 workflow 的 run_subagent。
        # 顶层互相 import 会转圈，把最里面这个环放在调用时解开。
        from fa.workflow import run_subagent

        result = run_subagent(question, get_bill=get_bill)
        return truncate(f"{result.answer}\n\n{result.report()}")

    return [investigate]
