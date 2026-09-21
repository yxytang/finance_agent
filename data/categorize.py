"""批量分类 —— 把三层跑一遍，结果写进缓存。

**为什么做成批处理而不是查询时现算。** 查询是交互式的，用户不该为了问一句
「上个月花了多少」等一次 LLM 调用。所以分类单独跑：跑完写进
`data/category_cache.json`，之后所有查询都白嫖缓存。

    python -m data.categorize            # 分类，并把缓存写回去
    python -m data.categorize --report   # 顺带出一份 per-class 报告

`--report` 要对着 `data/reference.csv`（真值）算，那是**评测**，不是运行时。
所以两者分开：默认只分类，报告得显式要。
"""

import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fa.categorize import CategoryCache, categorize  # noqa: E402
from fa.config import CATEGORIES, CATEGORY_CACHE, REFERENCE_CSV, TRANSACTIONS_CSV, build_model  # noqa: E402
from fa.ingest import IngestError, load_transactions  # noqa: E402


def load_truth(path: Path = REFERENCE_CSV) -> dict[str, str]:
    """真值类目。**它只在评测里出现**，运行时那条路径永远不该碰这个文件。"""
    if not path.is_file():
        return {}
    with path.open("r", encoding="utf-8", newline="") as fh:
        rows = dict(csv.reader(fh))
    rows.pop("txn_id", None)
    return rows


def report(tagged, truth: dict[str, str]) -> str:
    """per-class precision / recall。

    两个数都要给，因为它们的失败含义完全不同：

    - **recall 低**：这个类目的东西被漏到别处去了（用户问「餐饮花了多少」
      会少算）
    - **precision 低**：别的东西被算进这个类目了（会多算）

    只看 accuracy 的话，一个占 30% 的类目全错和一个小类目全错看起来一样严重。
    """
    hit: dict[str, int] = defaultdict(int)
    predicted: dict[str, int] = defaultdict(int)
    actual: dict[str, int] = defaultdict(int)

    for txn in tagged:
        want = truth.get(txn.txn_id)
        got = txn.category
        actual[want] += 1
        predicted[got] += 1
        if want == got:
            hit[want] += 1

    lines = [f"{'类目':<8}{'precision':>11}{'recall':>9}{'支持':>7}"]
    for name in CATEGORIES:
        if not actual[name] and not predicted[name]:
            continue
        precision = hit[name] / predicted[name] if predicted[name] else 0.0
        recall = hit[name] / actual[name] if actual[name] else 0.0
        lines.append(f"{name:<8}{precision:>10.0%}{recall:>9.0%}{actual[name]:>7}")

    correct = sum(hit.values())
    total = len(tagged)
    lines.append(f"\n整体 accuracy: {correct}/{total} = {correct / total:.1%}")

    wrong: dict[str, int] = defaultdict(int)
    for txn in tagged:
        if txn.category != truth.get(txn.txn_id):
            wrong[txn.merchant] += 1
    if wrong:
        lines.append("\n判错的商户（注意一个商户错了会让它**所有**交易都错）：")
        for merchant, count in sorted(wrong.items(), key=lambda kv: -kv[1])[:10]:
            lines.append(f"  {merchant:<32} {count} 笔")

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m data.categorize", description="批量给交易分类"
    )
    parser.add_argument("--report", action="store_true", help="顺带出 per-class 报告")
    parser.add_argument("--no-llm", action="store_true", help="只跑规则和缓存")
    args = parser.parse_args(argv)

    try:
        txns = load_transactions(TRANSACTIONS_CSV)
    except IngestError as exc:
        print(exc)
        return 1

    cache = CategoryCache.load(CATEGORY_CACHE)
    model = None if args.no_llm else build_model()

    tagged, stats = categorize(txns, model=model, cache=cache, use_llm=not args.no_llm)

    print(stats.describe())
    print(f"缓存写到 {CATEGORY_CACHE}")

    if args.report:
        truth = load_truth()
        if not truth:
            print(f"\n找不到真值 {REFERENCE_CSV}，出不了报告。")
            return 1
        print()
        print(report(tagged, truth))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
