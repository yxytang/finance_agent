"""检索评测 —— 命中率，以及和「朴素 grep」的对比。

    python -m eval.retrieval

## 为什么要跟 grep 比

因为「上了 RAG」这件事本身不说明任何问题。一个检索系统如果打不过
`grep`，那它就是在给项目增加复杂度而没有增加能力。

这个对比还有一个更实际的作用：**它是第八天 RAGAS 的前哨**。RAGAS 测的是
「检索出来的东西对不对」，而这里是「有没有检索到」。先知道后者，
第八天的 `context_recall` 分数才有上下文 —— 分数低的时候，你能分清是
「根本没召回」还是「召回了但排得靠后」。

## 基线怎么定的

朴素 grep 的做法：把查询切成 token，在每个文件里做**子串**匹配，
按命中的 token 数给文件排名。

这是个公平的基线不是稻草人 —— 它就是一个人在编辑器里按 Ctrl+F 能干的事，
而且它**没有 chunk 的概念**，只在文件粒度上排名。所以本系统在
「命中哪个小节」上有天然优势，而在「命中哪个文件」上两者可比。
"""

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fa.config import CHROMA_DIR, KNOWLEDGE_DIR, KNOWLEDGE_INDEX, rag_settings  # noqa: E402
from fa.retrieval import load_or_build, search  # noqa: E402
from fa.retrieval.index import tokenize  # noqa: E402

# 向量和重排那两件东西**在函数里 import**：`--mode bm25` 是 CI 走的路径，
# 它不该顺带把 openai 客户端那套拉起来。chromadb 本来就在 dense.py 里是懒的，
# 但让这个模块也只按需取，边界更清楚。


@dataclass(frozen=True)
class Case:
    question: str
    source: str  # 应该命中哪个文件
    heading: str  # 应该命中哪个小节（grep 基线用不到，只比到文件）


# 问题按难度分两组，因为它们的失败原因完全不同：
#
#   · 字面组：查询里出现的词，文档里也有。词法检索理应做对。
#   · 转述组：**换个说法**。文档里一个原词都没有。这一组是纯词法检索的
#     结构性短板，不是 bug —— 第八天的分数主要就是被它拉下来的。
LITERAL = [
    Case("LPR 是什么", "房贷.md", "利率是怎么定的"),
    Case("房贷利息能抵扣多少个税", "专项附加扣除.md", "住房贷款利息"),
    Case("信用卡最低还款的利息怎么算", "信用卡.md", "最低还款是个陷阱"),
    Case("等额本息和等额本金哪个划算", "房贷.md", "等额本息 vs 等额本金"),
    Case("五险一金个人要交多少", "五险一金.md", "谁交多少"),
    Case("医保断缴有什么后果", "五险一金.md", "断缴的后果"),
    Case("专项附加扣除里赡养老人能扣多少", "专项附加扣除.md", "赡养老人"),
    Case("年终奖的临界点是怎么回事", "年终奖计税.md", "那个著名的「临界点」"),
    Case("提前还房贷划算吗", "房贷.md", "提前还款划算吗"),
    Case("分期的真实利率是多少", "信用卡.md", "分期的真实利率"),
    Case("公积金有什么用", "五险一金.md", "住房公积金值得单独说"),
    Case("应急资金应该放哪里", "应急资金.md", "放在哪里"),
    Case("征信上的查询记录保留多久", "征信.md", "报告里有什么"),
    Case("重疾险保额该买多少", "保险.md", "顺序"),
    Case("货币基金能随时取吗", "存款与理财.md", "各类产品的特点"),
]

PARAPHRASED = [
    Case("怎么少交点税", "专项附加扣除.md", ""),
    Case("每个月到手工资为什么不一样", "个税基础.md", ""),
    Case("报销医药费超过多少能抵税", "专项附加扣除.md", ""),
    Case("借钱消费划算吗", "消费贷与真实利率.md", ""),
    Case("失业了没收入怎么办", "应急资金.md", ""),
]


def grep_baseline(question: str) -> list[str]:
    """朴素基线：按「命中了几个 token」给文件排名。

    只做子串匹配，没有词频、没有文档长度归一化、没有 IDF —— 也就是一个人
    在编辑器里逐个 Ctrl+F 能干的事。
    """
    terms = [t for t in set(tokenize(question)) if len(t) >= 2]
    if not terms:
        return []

    scored: list[tuple[int, str]] = []
    for path in sorted(KNOWLEDGE_DIR.rglob("*.md")):
        text = path.read_text(encoding="utf-8")
        hits = sum(1 for term in terms if term in text)
        if hits:
            scored.append((hits, path.relative_to(KNOWLEDGE_DIR).as_posix()))
    scored.sort(key=lambda kv: (-kv[0], kv[1]))
    return [name for _, name in scored]


def rank_of(items: list[str], wanted: str) -> int | None:
    """wanted 排在第几（从 1 数）。不在里面返回 None。"""
    for index, item in enumerate(items, start=1):
        if item == wanted:
            return index
    return None


def _build_index(mode: str):
    """按模式建索引。**full 模式拒绝降级。** 返回 `(索引, 缓存)`。

    `bm25` 模式是 CI 和快速迭代用的：确定、免费、秒级。它不需要缓存，
    所以缓存那一路返回 None。

    `full` 模式会调真的 embedding（要 key、要网络），并且**缓存开一次共用**——
    以前这里和重排那处各开了一次，于是两个独立的缓存对象、两条重复的
    「连不上」警告。

    ## 为什么 full 要当场验一次 embedding

    因为**降级是静默的**。向量路挂了（账户欠费、key 失效、库坏了）时，
    `hybrid._semantic_ranking` 会接住异常、退回两路 BM25，然后照常返回结果 ——
    这是对的（不该让一次检索整个失败），但它意味着**一次失效的测量看起来和
    正常测量一模一样**。

    第十一天就踩到了：百炼账户欠费，embedding 和 rerank 全 400，脚本照样
    打出一张表，而那张表全是 BM25 的数。差一点就当成「重排没用」报出去了。

    所以这里直接打一发 embedding，让它在**测量开始之前**就抛出来。
    """
    if mode == "bm25":
        return load_or_build(KNOWLEDGE_DIR, KNOWLEDGE_INDEX), None

    from fa.retrieval.cache import CachedEmbedder, open_cache
    from fa.retrieval.dense import HttpEmbedder, open_store

    settings = rag_settings()
    if settings is None:
        raise SystemExit(
            "--mode full 要 RAG_API_KEY，但现在没配。\n"
            "  要么填 .env，要么用 --mode bm25 —— 别在没开的时候声称是全开。"
        )

    cache = open_cache()
    print(f"检索缓存：{cache.describe()}")
    embedder = CachedEmbedder(HttpEmbedder(settings), cache)
    index = load_or_build(
        KNOWLEDGE_DIR,
        KNOWLEDGE_INDEX,
        embedder=embedder,
        store=open_store(CHROMA_DIR),
    )
    if index.dense is None or not index.dense.parents:
        raise SystemExit("full 模式下向量路没挂上（库是空的？），不测了。")

    embedder.embed(["健康检查"])  # 会抛就在这儿抛，别让它退化成 BM25
    return index, cache


def _rank_of(hits, wanted: str) -> int | None:
    for position, hit in enumerate(hits, start=1):
        if hit.chunk.source == wanted:
            return position
    return None


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m eval.retrieval")
    parser.add_argument(
        "--mode",
        choices=("bm25", "full"),
        default="bm25",
        help="bm25 = 只用词法两路（CI 用这个）；full = 加上向量",
    )
    parser.add_argument(
        "--rerank",
        action="store_true",
        help="再叠一层 cross-encoder 重排（只在 full 下有意义）",
    )
    args = parser.parse_args(argv)

    if args.rerank and args.mode != "full":
        raise SystemExit("--rerank 只在 --mode full 下有意义（它排的是融合后的候选）")

    index, cache = _build_index(args.mode)
    if not index.chunks:
        print(f"{KNOWLEDGE_DIR} 里没有语料。")
        return 1

    reranker = None
    candidates = 3
    if args.rerank:
        from fa.retrieval.cache import CachedReranker
        from fa.retrieval.rerank import RERANK_CANDIDATES, HttpReranker, rerank

        # 和 embedding 用**同一个**缓存对象，别再开一个。
        reranker = CachedReranker(HttpReranker(rag_settings()), cache)
        candidates = RERANK_CANDIDATES

    label = args.mode + ("+重排" if args.rerank else "")
    print(f"语料：{len({c.source for c in index.chunks})} 篇，{len(index.chunks)} 个块")
    print(f"模式：{label}")
    if index.dense is not None:
        print(f"      向量路子块 {len(index.dense.parents)} 个")
    print()

    for name, cases in (("字面问法", LITERAL), ("换种说法", PARAPHRASED)):
        with_heading = [c for c in cases if c.heading]
        total = len(cases)

        file_at_1 = 0
        head_at_1 = head_at_3 = 0
        base_at_1 = base_at_3 = 0
        ranks: list[int] = []
        misses: list[str] = []

        for case in cases:
            # 重排要的是**候选集**，不是最终的 3 条 —— 先截再排的话，被截掉的
            # 那个正好常常是重排该救回来的。
            hits = search(index, case.question, top_k=candidates)
            if reranker is not None:
                hits = rerank(case.question, hits, reranker=reranker)
            hits = hits[:3]

            files = [h.chunk.source for h in hits]
            crumbs = [h.chunk.breadcrumb for h in hits]

            if files and files[0] == case.source:
                file_at_1 += 1
            else:
                misses.append(f"{case.question}  →  {files[0] if files else '（空）'}")

            position = _rank_of(hits, case.source)
            if position is not None:
                ranks.append(position)

            if case.heading:
                # breadcrumb 是「文件 > 一级标题 > 二级标题 …」，所以判「命中哪个
                # 小节」要看**结尾**，不能拼成 "文件 > 小节" 去比 —— 那样永远比不上。
                suffix = f"> {case.heading}"
                if crumbs and crumbs[0].endswith(suffix):
                    head_at_1 += 1
                if any(c.endswith(suffix) for c in crumbs):
                    head_at_3 += 1

            base = grep_baseline(case.question)
            at = rank_of(base, case.source)
            if at == 1:
                base_at_1 += 1
            if at is not None and at <= 3:
                base_at_3 += 1

        print(f"=== {name}（{total} 题）===")
        line = f"  本系统   文件@1 {file_at_1}/{total}"
        if with_heading:
            line += f"   小节@1 {head_at_1}/{len(with_heading)}   小节@3 {head_at_3}/{len(with_heading)}"
        print(line)
        # **平均排名和 @1 一样要报。** 第十一天重排把换说法的平均排名从 4.20
        # 压到 2.20、字面的 @3/@5 提到 15/15，而 @1 一个都没动 —— 光看 @1
        # 会把一个有效的改动读成无效的。
        if ranks:
            print(f"  期望文档的排名：命中 {len(ranks)}/{total}   平均 {sum(ranks)/len(ranks):.2f}")
        print(f"  朴素grep  文件@1 {base_at_1}/{total}   文件@3 {base_at_3}/{total}")
        if misses:
            print("  文件级没命中的：")
            for miss in misses:
                print(f"    {miss}")
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(run())
