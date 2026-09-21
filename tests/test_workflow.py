"""子 agent —— 上下文隔离。

这个文件要钉住两件事：

1. **隔离是真的**：子 agent 的中间过程不进主上下文，主上下文只增长结论那一段。
2. **权限是收紧的**：子 agent 只能读，而且拿不到 `investigate` 自己。
   这不是「理论上应该」，而是安全边界 —— 它的中间过程用户看不到，
   未经确认的写操作不该从那种上下文里发生。
"""

from datetime import date
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, ToolMessage

import fa.workflow as workflow
from fa.agent import Session
from fa.models import Transaction, money
from fa.tools import build_tools
from fa.tools.delegate import build_delegate_tools
from fa.workflow import SUBAGENT_TOOLS, Delegation, readonly_tools, run_subagent


def txn(txn_id="T1", merchant="X", amount="10.00"):
    return Transaction(
        date=date(2026, 1, 5),
        merchant=merchant,
        amount=money(amount),
        account="credit",
        txn_id=txn_id,
    )


BILL = tuple(txn(f"T{i}", f"商户{i}", "10.00") for i in range(1, 6))


class _ScriptedModel:
    """按剧本回复的假模型，可以带 tool_calls。"""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.seen: list = []

    def invoke(self, messages):
        # **要存快照，不能存引用。** session 之后会继续往同一个列表里追加
        # assistant / tool 消息，存引用的话事后看到的就不是「调用当时的样子」了
        # —— 而是「跑完之后的样子」，断言会莫名其妙地差几条。
        self.seen.append(list(messages))
        reply = self.replies.pop(0) if self.replies else AIMessage(content="（没词了）")
        if isinstance(reply, BaseException):
            raise reply
        return reply


# --- 权限收紧 -----------------------------------------------------------


def test_subagent_tools_are_readonly():
    """子 agent 只能读。

    `correct_category` 会改分类缓存、`remember` 会写长期记忆 —— 那些改动需要
    用户确认，而子 agent 的中间过程用户看不到。
    """
    names = {t.name for t in readonly_tools(lambda: BILL)}

    assert "query_transactions" in names
    assert "find_anomalies" in names
    assert "correct_category" not in names
    assert "remember" not in names


def test_subagent_cannot_spawn_another_subagent():
    """递归的子 agent 是失控的乘数，而收益接近于零。"""
    names = {t.name for t in readonly_tools(lambda: BILL)}
    assert "investigate" not in names


def test_the_allowlist_is_fail_closed():
    """白名单而不是黑名单：新加的工具**默认不给子 agent**。

    黑名单的话，以后加一个 `delete_everything` 而忘了往名单里加，
    子 agent 就悄悄拿到它了 —— 而且没有任何测试会红。
    """
    assert "investigate" not in SUBAGENT_TOOLS
    assert "correct_category" not in SUBAGENT_TOOLS
    # 每个允许的名字都必须是真实存在的工具，否则白名单会静默地少给东西
    real = {t.name for t in build_tools(get_bill=lambda: BILL)}
    assert set(SUBAGENT_TOOLS) <= real


def test_readonly_tools_does_not_define_its_own_tools():
    """工具的定义只有一个地方（tools/__init__.py），这里只筛不造。

    两处定义就会漂移，然后有一处忘了改，子 agent 就拿到了不该拿的工具。
    """
    everything = {t.name for t in build_tools(get_bill=lambda: BILL)}
    assert set(SUBAGENT_TOOLS) < everything  # 真子集，不是全部


# --- 子 agent 跑得起来 ---------------------------------------------------


def test_subagent_runs_its_own_loop():
    model = _ScriptedModel(
        AIMessage(content="", tool_calls=[
            {"name": "query_transactions", "args": {"group_by": "merchant"}, "id": "c1"}
        ]),
        AIMessage(content="餐饮外卖三个月涨了 30%。"),
    )

    result = run_subagent("分析餐饮外卖的趋势", get_bill=lambda: BILL, model=model)

    assert "30%" in result.answer
    assert result.steps == 1  # 调了一轮工具
    assert result.question == "分析餐饮外卖的趋势"


def test_subagent_uses_the_fixed_prompt_without_skills_or_memory():
    """子 agent 的提示词只由任务决定，不带 skill 清单和记忆。

    带上那些的话，每个子 agent 都背着一份和它任务无关的上下文 ——
    而它存在的意义恰恰是省上下文。
    """
    model = _ScriptedModel(AIMessage(content="结论"))
    run_subagent("随便问点什么", get_bill=lambda: BILL, model=model)

    system = model.seen[0][0].content
    assert "子助手" in system
    assert "skill" not in system.lower()
    assert "关于这位用户" not in system


def test_subagent_gets_no_session_memory():
    """子 agent 的 messages 从零开始，只装它自己的任务。"""
    model = _ScriptedModel(AIMessage(content="结论"))
    run_subagent("看看商户", get_bill=lambda: BILL, model=model)

    first_call_messages = model.seen[0]
    # system + 任务，就这两条
    assert len(first_call_messages) == 2


def test_subagent_respects_a_lower_step_limit():
    """步数上限低，是为了让「任务没切好」尽早暴露，而不是拖着烧钱。"""
    model = _ScriptedModel(
        *[
            AIMessage(content="", tool_calls=[
                {"name": "query_transactions", "args": {"agg": "count"}, "id": f"c{i}"}
            ])
            for i in range(20)
        ]
    )

    result = run_subagent("一个永远不肯收敛的任务", get_bill=lambda: BILL,
                          model=model, max_steps=3)

    assert "3 步" in result.answer  # 撞上限之后如实报告
    assert result.steps == 3


def test_subagent_error_is_reported_not_raised():
    """子任务失败不该把主 agent 那一轮炸掉。"""
    model = _ScriptedModel(RuntimeError("This model's maximum context length is 65536 tokens"))

    result = run_subagent("问点什么", get_bill=lambda: BILL, model=model)

    assert "上下文超限" in result.answer  # 转成结论带回来


# --- 上下文记账 ---------------------------------------------------------


def test_delegation_accounting():
    """把账算清楚，才谈得上「值不值」。"""
    model = _ScriptedModel(
        AIMessage(content="", tool_calls=[
            {"name": "describe_data", "args": {}, "id": "c1"}
        ]),
        AIMessage(content="短结论。"),
    )

    result = run_subagent("摸清这笔账单", get_bill=lambda: BILL, model=model)

    assert result.context_chars > result.returned_chars
    assert result.saved_chars > 0


def test_report_says_so_when_delegation_lost_money():
    """中间过程少而结论长的时候，委派是**亏**的。

    报告必须如实说亏 —— 一个永远只报好话的记账等于没记账。
    """
    lost = Delegation(
        question="q", answer="很长的结论" * 100, steps=0,
        context_chars=50, returned_chars=500,
    )

    assert lost.saved_chars < 0
    assert "反而多了" in lost.report()
    assert "不该开子 agent" in lost.report()


def test_report_shows_the_saving_when_it_helped():
    won = Delegation(
        question="q", answer="短结论", steps=4,
        context_chars=10_000, returned_chars=100,
    )

    assert "省下" in won.report()
    assert "10,000" in won.report() or "9,900" in won.report()


# --- 隔离：主上下文只长结论那一段 ---------------------------------------


def test_main_context_only_grows_by_the_answer(monkeypatch):
    """**这是这一天要证明的东西。**

    主 agent 调一次 `investigate`，它的上下文只该增长「结论 + 那行账」，
    而不是子 agent 中间查过的所有东西。
    """
    sub_model = _ScriptedModel(
        AIMessage(content="", tool_calls=[
            {"name": "query_transactions", "args": {"group_by": "category"}, "id": "c1"},
            {"name": "find_anomalies", "args": {}, "id": "c2"},
        ]),
        AIMessage(content="结论：餐饮占大头。"),
    )
    main_model = _ScriptedModel(
        AIMessage(content="", tool_calls=[
            {"name": "investigate", "args": {"question": "分析整体结构"}, "id": "m1"}
        ]),
        AIMessage(content="综合起来是这样。"),
    )

    # 让子 agent 用假模型（真跑会去调 API）。
    #
    # 打在 `fa.workflow` 上而不是 `fa.tools.delegate` 上：后者是**在
    # investigate 函数体里**才 import 的（为了避开循环 import），
    # 所以它每次调用都去读 `fa.workflow` 的当前属性 —— 打那里才拦得住。
    original = workflow.run_subagent
    monkeypatch.setattr(
        "fa.workflow.run_subagent",
        lambda question, **kwargs: original(question, model=sub_model, **kwargs),
    )

    session = Session(tools=build_delegate_tools(lambda: BILL), model=main_model)
    session.send("帮我分析一下消费")

    delivered = [m for m in session.messages if isinstance(m, ToolMessage)]
    assert len(delivered) == 1
    content = delivered[0].content

    # 子 agent 的**结论**进来了
    assert "结论：餐饮占大头。" in content
    # 那行账也进来了 —— 主 agent 能看到这次委派值不值
    assert "省下" in content or "反而多了" in content
    # 而子 agent 中间那些原始数据**没有**进来
    assert "商户" not in content
    assert "按类目分组" not in content
