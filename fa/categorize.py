"""三层分类：规则 → 缓存 → LLM。

全项目唯一**不可靠**的一层 —— 也是唯一需要被度量的那一层（第八天的
per-class precision/recall 测的就是它）。

## 为什么分三层：因为三种代价都不重叠

| 层 | 速度 | 准确度 | 花钱 | 覆盖率 |
|---|---|---|---|---|
| 规则 | 微秒 | 高（人写的） | 不花 | **低**，只敢覆盖常见且无歧义的 |
| 缓存 | 微秒 | **会把错误固化** | 不花 | 中，随用随长 |
| LLM | 秒级 | 一般，看模型 | 花 | 全覆盖 |

## 顺序是「规则 → 缓存 → LLM」，不是「缓存 → 规则」

差别的根源只有一句话：**改规则要能立刻生效**。

如果缓存先查，一条写错的规则被缓存固化之后，就算把规则改对了也再也不会被走到
—— 错误被永久掩盖，而且**没有任何症状**。所以：

- 规则层排在最前，每次重算
- **缓存只写 LLM 的结论，不写规则的结论**

## 缓存真正的危险不是慢，是它会把错误放大

「省 LLM 调用」这个好处一眼可见，但同一个机制也是台错误放大器：一次错分类被
写进缓存，这个商户的**每一笔未来交易**都跟着错，而且看起来完全正常 ——
缓存命中了嘛，谁也不会去查。

所以缓存必须**可读、可编辑、可删**（`data/category_cache.json`，就是个 JSON
文件）。黑盒缓存出的错没法修。

## 隐私边界

送进 LLM 的**只有归一化后的商户串**，不带金额、日期、账户。

这条边界还有个副作用是好的：模型拿不到金额，就没法用「金额小 = 订阅」这种
启发式 —— 而那正是 `AMZN Mktp` 那个坑想诱导它犯的错。**信息少反而更准**。
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path

from fa.config import CATEGORIES, CATEGORIZE_BATCH
from fa.models import Transaction, UNCATEGORIZED
from fa.normalize import normalize

# 规则层的匹配表：(关键词, 类目)。关键词是**归一化之后**的串的子串。
#
# 这一层刻意写得**小**。它只该覆盖「常见 + 无歧义」的商户：写了规则就不用问
# LLM，但也意味着这条商户再也走不到 LLM 了 —— 规则写错就是永久错误。
# 所以拿不准的一律不写，交给 LLM。
#
# 比如 AMZN / TARGET / BEST BUY 就故意不在这里：它们高频但金额跨度大，
# 让 LLM 从商户串本身判断，比在这里替它定死更诚实（也更能暴露问题）。
RULES: tuple[tuple[str, str], ...] = (
    ("starbucks", "咖啡"),
    ("blue bottle", "咖啡"),
    ("peets", "咖啡"),
    ("safeway", "超市"),
    ("trader joes", "超市"),
    ("whole foods", "超市"),
    ("costco", "超市"),
    ("pacific property", "房租"),
    ("city utilities", "水电燃气"),
    ("verizon", "通讯"),
    ("netflix", "订阅"),
    ("spotify", "订阅"),
    ("readly", "订阅"),
)

_FALLBACK = "其他"


@dataclass(frozen=True)
class CategorizeStats:
    """三层的账。验收要求「缓存命中率、LLM 调用次数可见」，说的就是这个。"""

    merchants: int
    by_rule: int
    by_cache: int
    by_llm: int
    fallback: int
    llm_calls: int

    @property
    def cache_hit_rate(self) -> float:
        """缓存命中率。

        分母是**规则没搞定、真需要查的那部分**（缓存 + LLM），不是全部商户。
        把规则命中的算进分母会把命中率稀释成一个偏低的数，然后你会为了
        「提高命中率」去干些没意义的事。
        """
        looked_up = self.by_cache + self.by_llm
        return self.by_cache / looked_up if looked_up else 0.0

    def describe(self) -> str:
        return (
            f"{self.merchants} 个商户：规则 {self.by_rule}、缓存 {self.by_cache}、"
            f"LLM {self.by_llm}"
            + (f"（其中 {self.fallback} 个降级到「{_FALLBACK}」）" if self.fallback else "")
            + f"\nLLM 调用 {self.llm_calls} 次，缓存命中率 {self.cache_hit_rate:.0%}"
        )


# --- 第一层：规则 -------------------------------------------------------


def match_rule(merchant_key: str) -> str | None:
    """在规则表里找。最长命中优先 —— 「uber eats」该赢过「uber」。"""
    hits = [cat for word, cat in RULES if word in merchant_key]
    if not hits:
        return None
    # 命中多个时取关键词最长的那个：更具体的那条更可能是对的。
    best = max((w for w, _ in RULES if w in merchant_key), key=len)
    return next(cat for word, cat in RULES if word == best)


# --- 第二层：缓存 -------------------------------------------------------


class CategoryCache:
    """归一化商户 → 类目。就是个 JSON 文件，可读可编辑可删。

    刻意不做成黑盒：缓存出的错**只能靠改它来修**，藏起来就没法修了。
    """

    def __init__(self, path: Path, entries: dict[str, str] | None = None):
        self.path = path
        self.entries: dict[str, str] = dict(entries or {})

    @classmethod
    def load(cls, path: Path) -> "CategoryCache":
        if not path.is_file():
            return cls(path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # 坏掉的缓存不该让整个流程挂掉 —— 它只是个加速器，丢了重新问一遍
            # 就是了。但**不能悄悄当它不存在**：那就成了「缓存坏了而你不知道」。
            return cls(path)
        if not isinstance(data, dict):
            return cls(path)
        return cls(path, {str(k): str(v) for k, v in data.items()})

    def get(self, key: str) -> str | None:
        return self.entries.get(key)

    def set(self, key: str, category: str) -> None:
        self.entries[key] = category

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.entries, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


# --- 第三层：LLM --------------------------------------------------------

_LLM_SYSTEM = """你在给银行流水里的商户归类。

规则：
- 只输出一个 JSON 对象，键是商户名，值是类目。不要解释、不要加代码块标记。
- 每个商户必须出现在结果里，一个都不能漏。
- 拿不准就归「其他」，**不要猜**。猜错比归到「其他」更糟。
"""

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


def _ask_llm(model, merchants: list[str]) -> tuple[dict[str, str], int]:
    """问一批。返回 (商户 → 类目, 实际调用次数)。

    **只发商户名。** 不带金额、日期、账户 —— 这是这个项目的隐私边界，
    它落在这一层的入参上，所以这里是最该看清的地方。
    """
    calls = 0
    answer: dict[str, str] = {}

    for start in range(0, len(merchants), CATEGORIZE_BATCH):
        chunk = merchants[start : start + CATEGORIZE_BATCH]
        calls += 1
        response = model.invoke(
            [
                {"role": "system", "content": _LLM_SYSTEM},
                {
                    "role": "user",
                    "content": (
                        f"可选类目（只能从这里面选）：{'、'.join(CATEGORIES)}\n\n"
                        "要归类的商户：\n" + "\n".join(f"- {m}" for m in chunk)
                    ),
                },
            ]
        )
        answer.update(_parse(response.content or ""))

    return answer, calls


def _parse(raw: str) -> dict[str, str]:
    """从模型回复里抠出 JSON。

    模型很爱在外面包一层 ```json，所以不能直接 json.loads。
    """
    match = _JSON_BLOCK.search(raw)
    if not match:
        return {}
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items()}


# --- 组装 ---------------------------------------------------------------


def categorize(
    txns: list[Transaction],
    *,
    model=None,
    cache: CategoryCache | None = None,
    use_llm: bool = True,
) -> tuple[list[Transaction], CategorizeStats]:
    """给每笔交易贴上类目。返回 (新交易列表, 三层的账)。

    按**商户**归类，不是按交易 —— 同一个商户的几百笔交易只需要判一次。
    这也是归一化存在的意义：不归一化的话，「一个商户」这个概念根本不成立。

    返回的 Transaction 是**新对象**（`with_category` 产出的），原列表不动。
    """
    cache = cache if cache is not None else _default_cache()

    keys: dict[str, list[Transaction]] = {}
    for txn in txns:
        keys.setdefault(normalize(txn.merchant), []).append(txn)

    decided: dict[str, str] = {}
    by_rule = by_cache = 0
    pending: list[str] = []

    for key in keys:
        rule_hit = match_rule(key)
        if rule_hit:
            decided[key] = rule_hit
            by_rule += 1
            continue

        cached = cache.get(key)
        if cached in CATEGORIES:
            decided[key] = cached
            by_cache += 1
            continue

        pending.append(key)

    by_llm = 0
    fallback = 0
    llm_calls = 0

    if pending:
        if use_llm and model is not None:
            answer, llm_calls = _ask_llm(model, pending)
        else:
            answer = {}

        for key in pending:
            category = answer.get(key)
            if category not in CATEGORIES:
                # 模型返回了不存在的类目、漏了某个商户、或者压根没跑 ——
                # 一律降级到「其他」。**降级要计数**：不计数的话，
                # 「一半的商户其实是降级的」这件事永远浮不出来。
                category = _FALLBACK
                fallback += 1
            decided[key] = category
            cache.set(key, category)
            by_llm += 1

    if by_llm:
        cache.save()

    tagged = [
        txn.with_category(decided[normalize(txn.merchant)]) for txn in txns
    ]

    stats = CategorizeStats(
        merchants=len(keys),
        by_rule=by_rule,
        by_cache=by_cache,
        by_llm=by_llm,
        fallback=fallback,
        llm_calls=llm_calls,
    )
    return tagged, stats


def uncategorized(txns: list[Transaction]) -> list[Transaction]:
    """还没分类的。结果里 `UNCATEGORIZED` 是一个真实且常见的情况，
    得能单独数出来 —— 否则「还有多少没分」永远是个未知数。"""
    return [t for t in txns if not t.category]


def _default_cache() -> CategoryCache:
    """默认缓存位置。

    从 config **在调用时**读，不 import 快照 —— 测试要能把缓存换到临时目录，
    否则跑一次测试就会污染（甚至删改）真实的 `data/category_cache.json`。
    """
    from fa import config

    return CategoryCache.load(config.CATEGORY_CACHE)
