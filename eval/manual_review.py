"""汇总人工填写的 *.review.json；未复核项保持空，不推断为正确。"""

import argparse
from collections import defaultdict
import json
import math
from pathlib import Path

FIELDS = ("key_point_coverage", "evidence_supported", "correct_abstention")


def summarize_review(rows):
    groups = defaultdict(list)
    seen = set()
    for row in rows:
        key = (row["id"], row["trial"])
        if key in seen:
            raise ValueError(f"重复复核记录：{key}")
        seen.add(key)
        values = [row.get(k) for k in FIELDS]
        if any(v is not None for v in values) and not str(row.get("reviewer") or "").strip():
            raise ValueError(f"填写分数须注明复核人：{key}")
        for value in values:
            if value is not None and (not isinstance(value, (int, float)) or
                                      not math.isfinite(value) or not 0 <= value <= 1):
                raise ValueError(f"分数须为0到1或null：{key}")
        if row["group"] != "unanswerable" and row.get("correct_abstention") is not None:
            raise ValueError(f"拒答指标仅用于unanswerable题：{key}")
        groups[row["group"]].append(row)
    result = []
    for group, items in sorted(groups.items()):
        entry = {"group": group, "planned": len(items)}
        for field in FIELDS:
            scores = [r[field] for r in items if r.get(field) is not None]
            entry[field] = {"mean": sum(scores) / len(scores) if scores else None,
                            "reviewed": len(scores), "pending": len(items) - len(scores)}
        result.append(entry)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    rows = json.loads(args.input.read_text(encoding="utf-8"))
    result = summarize_review(rows)
    output = args.output or args.input.with_suffix(".summary.json")
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"复核统计已写到：{output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
