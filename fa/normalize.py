"""商户串归一化 —— 三层分类的地基。

银行流水里的商户串是给人看的，不是给机器比的：

    STARBUCKS #1234 SEATTLE WA
    SAFEWAY #1234 SEATTLE WA
    AMZN Mktp US*2H4KJ
    AMZN Mktp US*9X2LP       ← 同一个商户，随机码不同

不归一化的话每个门店、每笔交易都是不同的字符串：规则匹配不上、缓存命中不了、
LLM 每次都要重新判 —— **三层同时失效**。

## 归一化到什么程度，是这里唯一的难点

两个方向都会出事：

**太松**：`UBER *EATS` 和 `UBER *TRIP` 都变成 `uber`，缓存会把「外卖」和
「打车」混成一个类目 —— 而且**从此永远错下去**，因为缓存命中了。

**太紧**：`SPOTIFY P0A1B2C3` 和假设的 `SPOTIFY P0A1B2C4` 是两个 key，
缓存命中不了，每次都去问 LLM，钱照花。

规则因此定成：**只删能确定是「编号」的东西**，不确定的一律留着。

    漏删的代价 = 多花一次 LLM 调用（可恢复）
    错删的代价 = 永久性的分类错误（不可恢复）

两种代价不对称，所以一律往「宁可漏删」那边倒。
"""

import re

# 美国的州 + 哥伦比亚特区，小写。银行格式里常见「商户名 城市 州」的结尾。
_STATE_CODES = frozenset(
    """al ak az ar ca co ct de dc fl ga hi id il in ia ks ky la me md ma mi mn ms
    mo mt ne nv nh nj nm ny nc nd oh ok or pa ri sc sd tn tx ut vt va wa wv wi
    wy""".split()
)

_STATE_SUFFIX = re.compile(r"^[A-Z]{2}$")


def _is_code(token: str) -> bool:
    """这个 token 看起来是编号吗？

    判据就一条：**含数字**。这条规则把所有真实出现过的形式都覆盖了 ——
    `#1234`、`1234`、`T-1234`、`2H4KJ`、`P0A1B2C3`、`#10263` —— 而且不会
    误删任何品牌名（它们都是纯字母的）。
    """
    return any(ch.isdigit() for ch in token)


def _strip_city_and_state(tokens: list[str]) -> list[str]:
    """去掉结尾的「城市 + 州」。

    只认**大写**的两字母州码，因为银行格式里州名就是大写写的，而小写的 `in`
    / `or` / `me` / `ok` 都是普通英文词 —— 不加这个限制的话
    「FEDEX OFFICE IN ...」会被莫名其妙地砍掉尾巴。

    砍完必须还剩东西。全砍光说明这不是「城市+州」，是个恰好以州码结尾的商户名。
    """
    if len(tokens) >= 2 and _STATE_SUFFIX.match(tokens[-1]):
        if tokens[-1].lower() in _STATE_CODES and len(tokens) - 2 >= 1:
            return tokens[:-2]
    return tokens


def normalize(merchant: str) -> str:
    """把商户串归一成缓存 key。

    步骤：转小写 → 把 `*` 和 `/` 变成空格（`UBER *EATS` 要保住 `eats`）
    → 删掉含数字的 token → 去掉结尾的城市和州 → 压缩空白。

    归一化结果为空时回退到「小写去空白」的原串 —— 宁可留一个丑 key，
    也不能让一堆不同的商户共用空字符串这个 key。
    """
    original = merchant.strip()

    tokens = original.replace("*", " ").replace("/", " ").split()
    tokens = _strip_city_and_state(tokens)
    kept = [t for t in tokens if not _is_code(t)]

    result = " ".join(kept).lower().strip()
    result = re.sub(r"\s+", " ", result)
    return result or original.lower()
