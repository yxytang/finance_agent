"""跑 agent，把每道题「检索到了什么」记下来。

    python -m eval.collect            # 跑全部题目
    python -m eval.collect --limit 3  # 先跑几道看看

## retrieved_contexts 取什么 —— 这个选择决定一半的分数

**取所有 `search_knowledge` 返回的块之和，不含 `query_transactions` 的结果。**

RAGAS 量的是**检索器**。账单查询的结果不是检索器的产出 —— 那是另一个工具、
另一条路径。把它算进去的话，`context_precision` 会被大量和问题无关的账单数据
拉低，而那个低分反映的不是「检索差」，是「我们在拿两个东西一起量」。

反过来说，如果一个没被问到账单的问题，agent 却去查了账单，那**确实**该扣分 ——
但那是 agent 的路由问题，不是检索质量问题，该由另一套指标（或者人工看）来发现。
把它混进 RAGAS 只会让两边都读不出结论。

## 怎么拿到「检索到了什么」

`search_knowledge` 返回的是**渲染好的文本**，从它反解出块等于在测渲染器。

所以在 eval 脚本里**劫持检索函数**，直接记录它返回了什么对象。这是测量代码，
生产路径一行没改 —— 而且这样记到的就是真东西，不是渲染结果的某种近似。
"""

import argparse
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fa.tools.knowledge as knowledge_module  # noqa: E402

from eval.qa_cases import ALL, LITERAL, PARAPHRASED  # noqa: E402
from fa.agent import Session  # noqa: E402

OUT = Path(__file__).resolve().parent / "last_run.json"

# 被内容过滤拦掉时 send() 会返回这句话。它是**概率性**的，重问一次往往就好了。
FILTER_MARK = "内容过滤"


class Recorder:
    """劫持检索函数，把每次返回的块记下来。"""

    def __init__(self):
        self.original = knowledge_module.search
        self.calls: list[dict] = []

    def install(self) -> None:
        def recording(index, query, **kwargs):
            hits = self.original(index, query, **kwargs)
            self.calls.append(
                {
                    "query": query,
                    "chunks": [
                        {
                            "source": h.chunk.source,
                            "breadcrumb": h.chunk.breadcrumb,
                            # **正文必须记。** 只记标题的话，喂给 RAGAS 的
                            # `retrieved_contexts` 是一堆光秃秃的小节名，
                            # 里面没有答案需要的任何事实 —— 于是三个指标全部
                            # 接近 0，而那张表**看起来像个真实的结果**。
                            # 第一版就是这么写的，靠另一个独立算出来的命中率
                            # （17/17）才发现不对。
                            "text": h.chunk.text,
                        }
                        for h in hits
                    ],
                }
            )
            return hits

        knowledge_module.search = recording

    def restore(self) -> None:
        knowledge_module.search = self.original

    def reset(self) -> None:
        self.calls = []


def ask(question: str, *, attempts: int = 3) -> tuple[str, int]:
    """问一题，返回 (答案, 重试了几次)。

    重试是因为 DeepSeek 那个**概率性**的内容过滤：同一句话有时候正常返回、
    有时候被拦成空回复。不重试的话，这些空答案会以「模型答得不好」的形式
    混进分数里 —— 而它根本不是答案质量问题。
    """
    for attempt in range(attempts):
        session = Session(on_event=None)
        answer = session.send(question)
        if FILTER_MARK not in answer:
            return answer, attempt
        time.sleep(1.5)
    return answer, attempts - 1


def collect(limit: int | None = None) -> tuple[list[dict], int]:
    """跑题、采集。返回 (样本列表, 重试次数)。

    分组直接从**来源列表**带出来，不靠事后判断 —— 「这条属于哪一组」是数据的
    属性，用 `case in some_list` 去猜会在两组有重复题时静默出错。
    """
    recorder = Recorder()
    recorder.install()
    samples: list[dict] = []
    retries = 0

    todo: list[tuple[str, object]] = [("literal", c) for c in LITERAL]
    todo += [("paraphrased", c) for c in PARAPHRASED]
    if limit:
        todo = todo[:limit]

    try:
        for i, (group, case) in enumerate(todo, start=1):
            recorder.reset()
            answer, attempts = ask(case.question)
            retries += attempts

            contexts = [
                f"{c['breadcrumb']}\n{c['text']}"
                for call in recorder.calls
                for c in call["chunks"]
            ]

            status = f"{len(contexts)} 块" if contexts else "⚠️ 一块都没检索"
            print(f"  [{i}/{len(todo)}] {case.question}  →  {status}")

            samples.append(
                {
                    **asdict(case),
                    "group": group,
                    "response": answer,
                    "retrieved_contexts": contexts,
                    "search_queries": [call["query"] for call in recorder.calls],
                    "retries": attempts,
                }
            )
    finally:
        recorder.restore()

    return samples, retries


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m eval.collect")
    parser.add_argument("--limit", type=int, default=None, help="只跑前几题")
    args = parser.parse_args(argv)

    print(f"跑 {args.limit or len(ALL)} 道题，每题一个新 Session\n")
    samples, retries = collect(args.limit)

    OUT.write_text(
        json.dumps(samples, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    empty = sum(1 for s in samples if not s["retrieved_contexts"])
    print(f"\n写到 {OUT}")
    print(f"重试 {retries} 次（内容过滤是概率性的）")
    if empty:
        print(f"⚠️ {empty} 道题一块都没检索到 —— 那几题的 context_recall 必然是 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
