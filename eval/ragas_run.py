"""跑 RAGAS，出三个指标，写 findings.md。

    python -m eval.collect && python -m eval.ragas_run

## 三个指标各测 RAG 的哪一端

| 指标 | 测什么 | 低了说明什么 |
|---|---|---|
| `faithfulness` | **生成端** —— 答案有没有超出检索到的内容 | 模型在编 |
| `context_precision` | **检索端** —— 排在前面的块有没有用 | 排了一堆没用的上来 |
| `context_recall` | **检索端** —— 该有的块有没有被检索到 | 漏了 |

这个分工是这一天最有用的地方：**分数低的时候，你能分清是检索的锅还是生成的锅。**
一个笼统的「RAG 效果不好」没法指导下一步；「recall 0.9 而 faithfulness 0.5」
立刻告诉你问题在提示词而不是检索器。

## LLM-as-judge 什么时候不可靠

这套指标的实现是「把答案和上下文交给另一个模型，让它打分」。所以：

- **judge 自己会错**，而且错法有系统性 —— 它倾向于给「读起来通顺」的答案高分，
  哪怕那个答案和上下文无关
- **成本高**：每题每指标要发好几次调用。21 题 × 3 指标 ≈ 上百次
- **方差大**：同一个样本重跑分数会动，所以**别在小数点后两位上做文章**

所以这些分数适合用来**定位问题在哪一端**，不适合拿来当精确的性能指标。
findings.md 里会按这个口径写，不会把它包装成「我的 RAG 得了 0.87 分」。

## 那个概率性的内容过滤

DeepSeek 的内容过滤是概率性的：同一句话有时候正常返回、有时候返回空。
judge 撞上它的时候，那个空回复会被**当成一次低分**记进指标里 ——
分数会莫名地飘，而且飘的方向固定向下。

所以 judge 用的模型带重试。重试次数和最终仍失败的样本数都会报出来 ——
**不报的话，你不知道手里的分数有多少是服务端拦截的产物。**
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import warnings
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

warnings.filterwarnings("ignore", category=DeprecationWarning)

from langchain_openai import ChatOpenAI  # noqa: E402

from eval.collect import OUT  # noqa: E402
from fa.config import build_model  # noqa: E402

if TYPE_CHECKING:  # pragma: no cover
    from ragas.dataset_schema import EvaluationDataset

# **ragas 的 import 是懒的**，不放在模块顶层。
#
# CI 装的是 `.[dev]`，不含 `eval` 那个 extra（ragas 会带进 datasets / pyarrow /
# scipy 两百多兆）。放在顶层的话，`tests/test_eval.py` 只要 import 这个模块就会
# 失败 —— 而那一天最重要的东西（`sanity_problem` 防呆、题库自检、采集器检查）
# **根本不需要 ragas**。
#
# 懒加载之后：CI 上照样能跑那些检查，只有真正用到 ragas 的两处会跳过。
# 依赖的重量不该拖累用不到它的测试。

FINDINGS = Path(__file__).resolve().parent.parent / "findings.md"
MAX_ATTEMPTS = 3

STATS = {"empty": 0, "retried": 0}


class RetryingChatModel(ChatOpenAI):
    """judge 用的模型：拿到空回复就重试。

    空回复是 DeepSeek 那个**概率性**内容过滤的产物，不是评分结果。不重试的话
    它会被当成一次低分，而低分的方向是固定的 —— 分数会往下飘，且飘多少取决于
    运气。
    """

    max_attempts: int = MAX_ATTEMPTS

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        result = None
        for _ in range(self.max_attempts):
            result = super()._generate(messages, stop, run_manager, **kwargs)
            text = ""
            if result.generations and result.generations[0]:
                text = getattr(result.generations[0][0], "text", "") or ""
            if text.strip():
                return result
            STATS["empty"] += 1
            STATS["retried"] += 1
            time.sleep(1.0)
        return result


MIN_CONTEXT_CHARS = 80


def sanity_problem(samples: list[dict]) -> str | None:
    """跑之前先看一眼：上下文里到底有没有真东西。

    **这条检查是被一个真 bug 逼出来的。** 第一版的采集只记了 `source` 和
    `breadcrumb`，没记块正文 —— 于是 `retrieved_contexts` 是一堆光秃秃的小节名，
    里面没有答案需要的任何事实，三个指标全部接近 0。

    危险的地方在于**那张表看起来像个真实（很差的）结果**：没有报错、没有异常、
    进度条正常走完、分数是个合理的 0.00 到 0.12 之间的数。没有人会怀疑它。

    发现它靠的是另一个**独立算出来**的数字（本地统计 agent 有没有检索到正确文档，
    结果是 17/17）—— 两个数对不上。所以这里把那个直觉固化成检查。
    """
    if not samples:
        return "一个样本都没有"

    # **分母是「单个块」不是「每题」。** 第一版算的是每题所有块的总长，
    # 于是 4 个 40 字的标题加起来 160 字，轻松越过阈值 —— 一个抓不住它要抓的
    # 东西的检查。分母错了，阈值就没意义。
    sizes = [len(c) for s in samples for c in s["retrieved_contexts"]]
    if not sizes:
        return "所有样本的 retrieved_contexts 都是空的"

    average = sum(sizes) / len(sizes)
    if average < MIN_CONTEXT_CHARS:
        return (
            f"检索到的块平均只有 {average:.0f} 字符，太短了 —— "
            "多半只记了标题没记正文。指标会全部接近 0，而那张表看起来很正常。"
        )
    return None


def load_samples() -> list[dict]:
    if not OUT.is_file():
        raise SystemExit(f"没有 {OUT}。先跑 `python -m eval.collect`。")
    return json.loads(OUT.read_text(encoding="utf-8"))


def to_dataset(samples: list[dict]) -> "EvaluationDataset":
    from ragas.dataset_schema import EvaluationDataset, SingleTurnSample

    return EvaluationDataset(
        samples=[
            SingleTurnSample(
                user_input=s["question"],
                retrieved_contexts=s["retrieved_contexts"] or ["（没有检索到任何内容）"],
                response=s["response"],
                reference=s["reference"],
            )
            for s in samples
        ]
    )


def score_table(rows: list[dict], metrics: list[str], label: str) -> str:
    if not rows:
        return ""
    lines = [f"### {label}（{len(rows)} 题）", ""]
    lines.append("| 指标 | 分数 |")
    lines.append("|---|---|")
    for name in metrics:
        values = [r[name] for r in rows if isinstance(r.get(name), (int, float))]
        if values:
            lines.append(f"| {name} | **{sum(values) / len(values):.2f}** |")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m eval.ragas_run")
    parser.add_argument("--dry", action="store_true", help="只组装数据集，不调 LLM")
    args = parser.parse_args(argv)

    samples = load_samples()
    print(f"载入 {len(samples)} 个样本")

    problem = sanity_problem(samples)
    if problem:
        print(f"⚠️ 数据集有问题，不跑：{problem}")
        return 1

    dataset = to_dataset(samples)
    if args.dry:
        print("数据集组装成功（--dry，没调 LLM）：")
        print(f"  字段：{list(dataset.samples[0].model_fields)}")
        print(f"  平均检索块数：{sum(len(s['retrieved_contexts']) for s in samples) / len(samples):.1f}")
        return 0

    from ragas import evaluate
    from ragas.llms import LangchainLLMWrapper
    from ragas.metrics import context_precision, context_recall, faithfulness

    judge = LangchainLLMWrapper(build_model(cls=RetryingChatModel))
    metric_objs = [faithfulness, context_precision, context_recall]
    names = [m.name for m in metric_objs]

    print(f"跑 {len(metric_objs)} 个指标（每题每指标要发好几次调用，慢）…\n")
    started = time.time()
    result = evaluate(
        dataset=dataset,
        metrics=metric_objs,
        llm=judge,
        raise_exceptions=False,
        show_progress=True,
    )
    elapsed = time.time() - started

    frame = result.to_pandas()
    rows = frame.to_dict("records")
    for row, sample in zip(rows, samples):
        row["group"] = sample["group"]

    print(f"\n用时 {elapsed:.0f} 秒")
    print(f"judge 遇到空回复 {STATS['empty']} 次，重试 {STATS['retried']} 次")

    print()
    print(score_table(rows, names, "全部"))
    print(score_table([r for r in rows if r["group"] == "literal"], names, "字面问法"))
    print(score_table([r for r in rows if r["group"] == "paraphrased"], names, "换种说法"))

    write_findings(rows, names, samples, elapsed)
    print(f"\n写到 {FINDINGS}")
    return 0


def write_findings(rows, names, samples, elapsed) -> None:
    by_source = defaultdict(lambda: {"n": 0, "recall": []})
    for row, sample in zip(rows, samples):
        entry = by_source[sample["source"]]
        entry["n"] += 1
        if isinstance(row.get("context_recall"), (int, float)):
            entry["recall"].append(row["context_recall"])

    empty = [s for s in samples if not s["retrieved_contexts"]]

    body = [
        "# RAGAS 评测结果",
        "",
        f"生成时间：本文件由 `python -m eval.ragas_run` 写出，用时 {elapsed:.0f} 秒。",
        f"样本：{len(samples)} 题（17 题字面问法 + 4 题换种说法）。",
        f"judge：DeepSeek，遇到 {STATS['empty']} 次空回复（概率性内容过滤），重试 {STATS['retried']} 次。",
        "",
        "## 怎么读这三个数",
        "",
        "- `faithfulness` 测**生成端**：答案有没有超出检索到的内容。低了说明模型在编。",
        "- `context_precision` 测**检索端**：排在前面的块有没有用。低了说明排序不好。",
        "- `context_recall` 测**检索端**：该有的块有没有被检索到。低了说明漏了。",
        "",
        "**它们能用来看问题出在哪一端，不适合当成精确的性能指标** —— "
        "judge 自己会错、成本高、重跑一次分数会动。所以下面的数字都保留两位，"
        "不要在小数点后三位上做文章。",
        "",
        score_table(rows, names, "全部"),
        score_table([r for r in rows if r["group"] == "literal"], names, "字面问法"),
        score_table([r for r in rows if r["group"] == "paraphrased"], names, "换种说法"),
    ]

    if empty:
        body += [
            "## 一块都没检索到的题",
            "",
            "这几题的 `context_recall` 必然是 0，而且原因**不在检索器** —— "
            "是 agent 根本没去检索。",
            "",
        ]
        body += [f"- {s['question']}" for s in empty]
        body.append("")

    body += routing_section(samples, rows)

    body += [
        "## 按文档看 recall",
        "",
        "这一栏用来回答「是检索没找对地方，还是找对了但答歪了」。",
        "",
        "| 文档 | 题数 | 平均 context_recall |",
        "|---|---|---|",
    ]
    for source, entry in sorted(by_source.items()):
        if entry["recall"]:
            avg = sum(entry["recall"]) / len(entry["recall"])
            body.append(f"| {source} | {entry['n']} | {avg:.2f} |")
        else:
            body.append(f"| {source} | {entry['n']} | — |")

    body += [
        "",
        "## 已知的偏差（不许为了好看改掉）",
        "",
        "1. **知识库是我自己写的。** 语料和参考答案出自同一个人、同一套口径，"
        "所以 `context_recall` 天然偏乐观 —— 真实语料上一定会降。",
        "2. **检索是纯词法的**（DeepSeek 没有 embedding 接口，所以没有向量召回）。"
        "这个限制是真的，但**本轮数据没有显示它是失分原因** —— 见上面那节。"
        "它在 Day 5 用原始问题直接查检索器时量到过（转述组 1/5）；"
        "agent 会把问题改写成关键词，改写恰好补上了这个缺口。",
        "3. **judge 用的是同一个模型。** 被评的答案和打分的是同一个 DeepSeek，"
        "可能共享同一种偏好。换成不同厂商的 judge 分数会变。",
        "4. **数字会动。** judge 自己会错、路由也不是确定性的 ——"
        "重跑一次分数会变几个百分点。所以看趋势，别看小数位。",
        "",
    ]
    FINDINGS.write_text("\n".join(body), encoding="utf-8")


def routing_section(samples: list[dict], rows: list[dict]) -> list[str]:
    """把「失分是路由问题还是检索问题」算出来，而不是写死在文案里。

    第一版的结论写的是「转述组答不好是纯词法检索的结构性短板」，但那句话
    **和数据不符**：本地统计显示 agent 去检索的时候命中率是 100%，转述组真正
    的问题是它**不去检索**。而且那是生成出来的文件 —— 手改会被下次覆盖。

    所以结论必须由代码从数据里算出来。写死的结论会随着数据变化变成谎言，
    而它看起来仍然像一份分析。
    """
    searched = [s for s in samples if s["search_queries"]]
    missed = [s for s in samples if not s["search_queries"]]

    lines = [
        "## 根因：失分是路由问题还是检索问题",
        "",
        f"本轮 {len(samples)} 题里，**{len(searched)} 题真的调了 `search_knowledge`**，"
        f"另外 {len(missed)} 题一次都没调。",
        "",
    ]

    if not searched:
        lines += ["一道题都没检索，这份分数反映的是路由，不是检索。", ""]
        return lines

    by_group: dict[str, list[dict]] = defaultdict(list)
    for sample in samples:
        by_group[sample["group"]].append(sample)

    lines += [
        "| 组 | 题数 | 真的去检索 | 检索到期望的文档 |",
        "|---|---|---|---|",
    ]
    for group, items in by_group.items():
        did = [s for s in items if s["search_queries"]]
        hit = [s for s in did if any(s["source"] in c for c in s["retrieved_contexts"])]
        lines.append(f"| {group} | {len(items)} | {len(did)}/{len(items)} | {len(hit)}/{len(items)} |")

    lines += [""]

    # 只在有对比价值时下结论：两组的「检索率」差得多，说明问题在路由
    rates = {
        group: len([s for s in items if s["search_queries"]]) / len(items)
        for group, items in by_group.items()
    }
    if len(rates) >= 2:
        low, high = min(rates.values()), max(rates.values())
        if high - low >= 0.3:
            lines += [
                f"两组的**检索率**差了 {high - low:.0%} —— 一边几乎每题都查，另一边大半不查。"
                "这说明组间的分差主要来自 **agent 决定查不查**，而不是检索器查得好不好。"
                "**所以该动的是路由（prompt / 触发条件），不是 BM25 的排序。**",
                "",
            ]

    lines += [
        "> 这个诊断很重要：三个指标一起塌的时候，最自然的反应是「检索器不行」，",
        "> 然后去调排序、换分词、上向量。但如果 agent 压根没调用检索，",
        "> 上面这些改动一个都不会让分数上升 —— 而你会以为是自己没调对。",
        "",
    ]
    return lines


if __name__ == "__main__":
    raise SystemExit(main())
