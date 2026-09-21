"""写入门槛 —— 记忆这个功能，难点在「什么时候记」，不在「记在哪」。

**记太多**：一次性细节堆成流水账，注入 prompt 既占地方又稀释真正重要的那几条，
最后模型学会无视整块记忆。

**记太少**：用户教过的事下次还得再教一遍，那这个功能等于没做。

所以门槛按**信号可靠性**排：

1. 用户明确要求（「记住…」）—— 最可靠，用户自己说要记
2. 用户纠正了 agent 的判断 —— 纠正意味着 agent 的判断和用户意图不一致，
   而这个不一致**还会再发生**（同一个商户下次还会被问到）
3. 用户陈述了自己的长期属性（「我住…」「我不用…」）

**最不该记的是一次性的任务细节。** 「这次帮我看看 3 月」记下来，下次用户问
4 月的时候这条记忆就是错的 —— 而且它看起来还很合理，所以特别难发现。
"""

ONE_OFF_MARKERS = (
    "这次",
    "本次",
    "今天",
    "刚刚",
    "刚才",
    "眼下",
    "这一次",
    "帮我看看",
    "帮我查",
)

CORRECTION_MARKERS = ("应该", "不是", "改成", "纠正", "归到", "算作", "属于")
PREFERENCE_MARKERS = ("我喜欢", "我习惯", "我住", "我不用", "我不要", "别把", "不要")


def judge(text: str) -> tuple[bool, str]:
    """返回 (要不要记, 原因)。

    原因在**拒绝**时才要紧：模型得知道为什么被拒，否则它会换个说法再试一次，
    而那一次可能就绕过了门槛 —— 那比一开始就放行还糟。
    """
    clean = text.strip()

    if len(clean) < 4:
        return False, "太短了，看不出是一条能复用的信息。"

    for marker in ONE_OFF_MARKERS:
        if marker in clean:
            return False, (
                f"里面有「{marker}」，听起来像一次性的事，不该记。"
                "一次性的细节记下来，下次情况变了它就是误导 —— 而且看起来还挺合理。"
            )

    return True, ""


def guess_kind(text: str) -> str:
    """猜这条属于哪一类。

    猜错不要紧 —— 类型只影响 `/memory` 列表里的展示，不影响记忆本身怎么用。
    所以规则从简，也不值得再花一次 LLM 调用。
    """
    if any(word in text for word in CORRECTION_MARKERS):
        return "纠正"
    if any(word in text for word in PREFERENCE_MARKERS):
        return "偏好"
    return "事实"
