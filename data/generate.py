"""合成账单生成器 —— 整个项目的真值来源。

**为什么用合成数据。** 这套东西的卖点之一是「可度量」，而可度量的前提是有真值。
真实账单既不能公开、类目也得自己标，而合成数据能让 ground truth 完全可控 ——
包括**故意埋进去的坑**。

**为什么结束日跟今天走。** `START` 和 `SEED` 钉死，只有结束日取 `date.today()`。
这是为了让「本月」在数据里**真实存在** —— 预测类功能需要一个当下的靶子，
否则「这个月还没过完」这条边界永远只能在纸面上讨论。

代价是数据不再随时间不变：今天生成和明天生成的不一样。要复现某次分析就
**记下当时的结束日**，用 `--end` 把它钉回去，会得到同一份数据。可复现靠的是
「同 seed + 同 end」，不是「永远同一份文件」。

（顺带：`--end` 早于最后一个坑的日期时，那个坑会被裁掉，`--check` 会如实报红。
这是对的 —— 反查验的是数据，不是生成代码的意图。）

## 埋的 6 个坑

`--check` 会**从产物里反查**这 6 条，而不是靠生成时打个断言。区别在于：
生成时的断言只能证明「我打算埋」，反查才能证明「埋进去了」—— 而且后面改了
生成逻辑，反查会立刻发现坑没了，断言不会（它跟着代码一起改了）。

| # | 坑 | 考的是什么 |
|---|---|---|
| 1 | 同一商户同一天同金额扣两次（非订阅） | 重复扣款检出 |
| 2 | Netflix 从第 7 个月起涨价 25% | 订阅涨价检出 |
| 3 | 一笔购物远超该类目 p95 | 异常大额检出 |
| 4 | 一个每月小额、名字不显眼的订阅 | 幽灵订阅检出 |
| 5 | 一笔负数退款 | 金额符号处理 |
| 6 | 金额跨度很大的歧义商户 | 分类的边界 |

## 用法

    python -m data.generate                    # 生成到今天的账单
    python -m data.generate --end 2026-08-31   # 钉住结束日，复现旧分析
    python -m data.generate --check            # 只验证 6 个坑还在不在
"""

import argparse
import csv
import random
import sys
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fa.config import CATEGORIES  # noqa: E402
from fa.models import ZERO, Transaction, money  # noqa: E402

SEED = 20250901
START = date(2025, 9, 1)

# 账户：固定的大额走 checking，日常走 credit。真实账单就是分账户导出的。
CHECKING = "checking"
CREDIT = "credit"

# 每月固定项：(商户, 类目, 几号, 基准金额, 账户, 波动比例)
FIXED_ITEMS = [
    ("PACIFIC PROPERTY MGMT RENT", "房租", 1, "3250.00", CHECKING, 0.0),
    ("CITY UTILITIES ELECTRIC", "水电燃气", 8, "104.00", CHECKING, 0.45),
    ("VERIZON WIRELESS", "通讯", 12, "68.00", CHECKING, 0.0),
]

# 订阅。单独列出来是因为第 2 个坑（涨价）要动它。
SUBSCRIPTIONS = [
    ("NETFLIX.COM", "订阅", 5, "15.49"),
    ("SPOTIFY P0A1B2C3", "订阅", 5, "11.99"),
    ("APPLE.COM/BILL ICLOUD", "订阅", 20, "2.99"),
    # 第 3 个坑：幽灵订阅。金额小、名字不显眼，人对它几乎没有感觉。
    ("READLY DIGITAL MAGAZINES", "订阅", 23, "9.99"),
]

# 随机项：(类目, 商户池, (每月次数下限, 上限), (金额下限, 上限), 账户)
VARIABLE_ITEMS = [
    (
        "超市",
        ["SAFEWAY #1234 SEATTLE WA", "TRADER JOES #567", "WHOLE FOODS MKT #10263",
         "COSTCO WHSE #123"],
        (12, 15),
        ("28.00", "180.00"),
        CREDIT,
    ),
    (
        "餐饮外卖",
        ["TST* SUSHI PALACE", "DOORDASH*THAI GARDEN", "UBER *EATS",
         "CHIPOTLE 1234", "TST* RAMEN KOBO"],
        (23, 29),
        ("11.00", "68.00"),
        CREDIT,
    ),
    (
        "咖啡",
        ["STARBUCKS #1234 SEATTLE WA", "BLUE BOTTLE COFFEE", "PEETS #88"],
        (14, 18),
        ("4.25", "9.75"),
        CREDIT,
    ),
    (
        "交通",
        ["UBER *TRIP", "LYFT *RIDE", "SHELL OIL 57440", "TRANSIT AUTHORITY"],
        (11, 15),
        ("3.00", "72.00"),
        CREDIT,
    ),
    (
        # 第 6 个坑就在这个池子里：AMZN Mktp 的金额跨度极大，
        # 小额的像订阅、大额的像购物，纯看商户名判不出来。
        "购物",
        ["AMZN Mktp US*2H4KJ", "TARGET T-1234", "BEST BUY #245", "AMZN Mktp US*9X2LP"],
        (7, 10),
        ("14.00", "260.00"),
        CREDIT,
    ),
    ("娱乐", ["STEAM GAMES", "AMC THEATRES #4412", "TICKETMASTER"], (2, 3),
     ("12.00", "95.00"), CREDIT),
    ("医疗", ["CVS PHARMACY #2231", "QUEST DIAGNOSTICS"], (0, 2),
     ("15.00", "220.00"), CREDIT),
    ("其他", ["USPS POSTAL STORE", "FEDEX OFFICE 1234"], (1, 2),
     ("6.00", "48.00"), CREDIT),
]

# 一次性事件：(年月偏移, 日, 商户, 类目, 金额)
ONE_OFFS = [
    (2, 17, "GEICO AUTO INSURANCE", "交通", "742.00"),
    (5, 3, "DELTA AIR LINES 0062", "旅行", "418.60"),
    (5, 6, "MARRIOTT HOTELS 8891", "旅行", "534.20"),
]

# --- 坑的具体参数（--check 按这些去反查）---------------------------------

TRAP_DUP_MERCHANT = "TARGET T-1234"       # 坑 1
TRAP_DUP_DATE = date(2026, 3, 14)
TRAP_DUP_AMOUNT = "188.40"

TRAP_PRICE_INCREASE_MERCHANT = "NETFLIX.COM"  # 坑 2
TRAP_PRICE_INCREASE_MONTH = 7                 # 第 7 个月起
TRAP_PRICE_INCREASE_RATIO = Decimal("1.25")   # 生成时涨这么多
# 校验时放的容忍：money() 量化到分，15.49 × 1.25 = 19.3625 存进去是 19.36，
# 比值只有 1.2498。拿生成用的 1.25 去校验会被自己的四舍五入卡掉。
TRAP_PRICE_CHECK_RISE = Decimal("1.20")

TRAP_OUTLIER_MERCHANT = "BEST BUY #245"       # 坑 3
TRAP_OUTLIER_AMOUNT = "3899.00"
TRAP_OUTLIER_RATIO = Decimal("3")             # 至少是同类目 p95 的 3 倍

TRAP_GHOST_MERCHANT = "READLY DIGITAL MAGAZINES"  # 坑 4

TRAP_REFUND_MERCHANT = "AMZN Mktp US*9X2LP"   # 坑 5
TRAP_REFUND_AMOUNT = "-139.99"
TRAP_REFUND_DATE = date(2026, 4, 9)

TRAP_AMBIGUOUS_MERCHANT = "AMZN Mktp US*2H4KJ"  # 坑 6
TRAP_AMBIGUOUS_SPREAD = Decimal("10")           # 最大/最小至少差这么多倍


def _months_to_cover(end: date) -> int:
    """START 所在月到 end 所在月，含首尾，一共要生成几个月。

    末月通常是残缺的（只过到 end 那天），由 `generate()` 在最后裁掉多余的日子，
    这里只管**要铺几个月**。
    """
    return (end.year - START.year) * 12 + (end.month - START.month) + 1


def _month_start(index: int) -> date:
    """第 index 个月（0 起）的 1 号。"""
    year = START.year + (START.month - 1 + index) // 12
    month = (START.month - 1 + index) % 12 + 1
    return date(year, month, 1)


def _day_in(month_start: date, day: int) -> date:
    """尽量取该月的第 day 天，超出月末就退回月末。"""
    year = month_start.year + (month_start.month // 12)
    month = month_start.month % 12 + 1
    last_day = (date(year, month, 1) - timedelta(days=1)).day
    return date(month_start.year, month_start.month, min(day, last_day))


def _jitter(rng: random.Random, base: Decimal, ratio: float) -> Decimal:
    if ratio <= 0:
        return base
    factor = Decimal(str(1 + rng.uniform(-ratio, ratio)))
    return money(base * factor)


def generate(seed: int = SEED, end: date | None = None) -> list[tuple]:
    """产出 (date, merchant, amount, account, category)，只到 end 当天。

    返回元组而不是 Transaction，因为 txn_id 要等排序之后再分配 ——
    这样 CSV 读起来是日期递增的，id 也顺。

    `end` 默认今天。它**不参与抽样**，只决定铺几个月、以及最后裁掉哪些行 ——
    所以换一个 `end` 拿到的永远是同一条随机序列的**前缀**，前面的月份一笔不变。
    已埋的 6 个坑全靠这条性质才不会被延长数据挤走：别把 `end` 混进任何
    `rng.*` 调用里。
    """
    end = end or date.today()
    rng = random.Random(seed)
    entries: list[tuple] = []

    for index in range(_months_to_cover(end)):
        month_start = _month_start(index)

        for merchant, category, day, base, account, ratio in FIXED_ITEMS:
            entries.append(
                (_day_in(month_start, day), merchant,
                 _jitter(rng, money(base), ratio), account, category)
            )

        for merchant, category, day, base in SUBSCRIPTIONS:
            price = money(base)
            if (
                merchant == TRAP_PRICE_INCREASE_MERCHANT
                and index + 1 >= TRAP_PRICE_INCREASE_MONTH
            ):
                # 坑 2：从第 7 个月起涨价，之后每个月都按新价扣。
                price = money(price * TRAP_PRICE_INCREASE_RATIO)
            entries.append((_day_in(month_start, day), merchant, price, CHECKING, category))

        for category, pool, (low, high), (amin, amax), account in VARIABLE_ITEMS:
            for _ in range(rng.randint(low, high)):
                day = rng.randint(1, 28)
                amount = money(rng.uniform(float(amin), float(amax)))
                entries.append(
                    (_day_in(month_start, day), rng.choice(pool), amount, account, category)
                )

    for offset, day, merchant, category, amount in ONE_OFFS:
        entries.append(
            (_day_in(_month_start(offset), day), merchant, money(amount), CREDIT, category)
        )

    # --- 把坑埋进去 ---------------------------------------------------
    # 一律**显式注入**，不靠随机采样碰运气。第一版把坑 6 交给随机金额去碰，
    # 结果调了一下每月的购物频次、RNG 流跟着变，跨度就从 12× 掉到 6.7×，
    # 坑没了。一个「已知真值」如果不写死，它就不是已知的。
    entries.append((TRAP_DUP_DATE, TRAP_DUP_MERCHANT, money(TRAP_DUP_AMOUNT), CREDIT, "购物"))
    entries.append((TRAP_DUP_DATE, TRAP_DUP_MERCHANT, money(TRAP_DUP_AMOUNT), CREDIT, "购物"))
    entries.append((date(2026, 2, 21), TRAP_OUTLIER_MERCHANT, money(TRAP_OUTLIER_AMOUNT), CREDIT, "购物"))
    entries.append((TRAP_REFUND_DATE, TRAP_REFUND_MERCHANT, money(TRAP_REFUND_AMOUNT), CREDIT, "购物"))
    # 坑 6 的两个端点：4.99 看着像订阅，329 看着像购物，商户名一模一样。
    entries.append((date(2025, 10, 4), TRAP_AMBIGUOUS_MERCHANT, money("4.99"), CREDIT, "购物"))
    entries.append((date(2026, 5, 16), TRAP_AMBIGUOUS_MERCHANT, money("329.00"), CREDIT, "购物"))

    # 末月裁到 end 当天。统一在这里裁，免得每个来源各写一遍 ——
    # ONE_OFFS 和 6 个坑都是写死日期的，也一样受这条约束。
    entries = [row for row in entries if row[0] <= end]
    entries.sort(key=lambda row: (row[0], row[1]))
    return entries


def build(seed: int = SEED, end: date | None = None) -> tuple[list[Transaction], dict[str, str]]:
    """返回 (交易列表, txn_id → 真值类目)。

    真值单独给出来，是因为它**不能混进 transactions.csv** —— 那是 agent 要读的
    输入，把答案写在里面等于泄题。所以分成两个文件。
    """
    transactions: list[Transaction] = []
    reference: dict[str, str] = {}

    for i, (day, merchant, amount, account, category) in enumerate(generate(seed, end), start=1):
        txn_id = f"T{i:06d}"
        transactions.append(
            Transaction(
                date=day, merchant=merchant, amount=amount,
                account=account, txn_id=txn_id,
            )
        )
        reference[txn_id] = category

    return transactions, reference


def write_csv(transactions: list[Transaction], path: Path) -> None:
    """只写 agent 该看到的列 —— **不含类目**。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["txn_id", "date", "merchant", "amount", "account"])
        for txn in transactions:
            writer.writerow(
                [txn.txn_id, txn.date.isoformat(), txn.merchant,
                 f"{txn.amount:.2f}", txn.account]
            )


def write_reference(reference: dict[str, str], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["txn_id", "category"])
        for txn_id, category in reference.items():
            writer.writerow([txn_id, category])


# --- 反查：6 个坑还在不在 ------------------------------------------------


def _p95(values: list[Decimal]) -> Decimal:
    if not values:
        return ZERO
    ordered = sorted(values)
    idx = min(int(len(ordered) * 0.95), len(ordered) - 1)
    return ordered[idx]


def check_traps(
    transactions: list[Transaction], reference: dict[str, str]
) -> list[tuple[str, bool, str]]:
    """从产物里反查 6 个坑。返回 [(名字, 是否通过, 说明)]。

    刻意**不引用上面的常量、不看 seed、不看生成逻辑** —— 只看交易和标签本身。
    这样它验证的是**数据**，而不是**生成代码的意图**。区别很实在：改了生成
    逻辑之后，跟着代码一起改的断言会一直通过，反查则会立刻发现坑没了。

    陷阱 3 需要真值类目（「同类目 p95」），所以得把 reference 传进来。
    """
    results: list[tuple[str, bool, str]] = []
    by_merchant: dict[str, list[Transaction]] = defaultdict(list)
    for txn in transactions:
        by_merchant[txn.merchant].append(txn)

    # 坑 1：同商户 + 同日 + 同金额，出现两次以上，且不是订阅
    # （订阅本来就每月扣一次，但不会同一天扣两次）
    buckets: dict[tuple, list[Transaction]] = defaultdict(list)
    for txn in transactions:
        buckets[(txn.merchant, txn.date, txn.amount)].append(txn)
    dupes = [
        f"{merchant} {day} {amount}"
        for (merchant, day, amount), group in buckets.items()
        if len(group) >= 2 and merchant not in {s[0] for s in SUBSCRIPTIONS}
    ]
    results.append(("1 重复扣款", bool(dupes),
                    f"检出 {len(dupes)} 组" + (f"，例如 {dupes[0]}" if dupes else "")))

    # 坑 2：订阅后期均价明显高于前期
    # 阈值用 1.2 而不是生成时那个 1.25 —— money() 会量化到分，
    # 15.49 × 1.25 = 19.3625 存进去变成 19.36，比值只有 1.2498。
    # 拿生成用的常量来校验，会被自己的四舍五入卡掉。
    netflix = sorted(by_merchant.get(TRAP_PRICE_INCREASE_MERCHANT, []), key=lambda t: t.date)
    half = len(netflix) // 2
    early = sum((t.amount for t in netflix[:half]), ZERO) / half if half else ZERO
    rest = len(netflix) - half
    late = sum((t.amount for t in netflix[half:]), ZERO) / rest if rest else ZERO
    rose = early > ZERO and late > early * TRAP_PRICE_CHECK_RISE
    results.append(("2 订阅涨价", rose,
                    f"{TRAP_PRICE_INCREASE_MERCHANT} 前 {half} 期均价 {early:.2f} → 后 {rest} 期 {late:.2f}"))

    # 坑 3：某笔购物远超**同类目** p95
    shopping = [t.amount for t in transactions if reference.get(t.txn_id) == "购物"]
    threshold = _p95(shopping)
    biggest = max(shopping) if shopping else ZERO
    outlier = threshold > ZERO and biggest > threshold * TRAP_OUTLIER_RATIO
    results.append(("3 异常大额", outlier,
                    f"购物类 p95 {threshold:.2f}，最大一笔 {biggest:.2f}（{biggest / threshold:.1f}×）" if threshold else "购物类为空"))

    # 坑 4：一个小额、按月出现、覆盖全部 12 期的订阅
    ghost = by_merchant.get(TRAP_GHOST_MERCHANT, [])
    ghost_ok = len(ghost) >= 12 and all(t.amount <= money("15.00") for t in ghost)
    results.append(("4 幽灵订阅", ghost_ok,
                    f"{TRAP_GHOST_MERCHANT} 出现 {len(ghost)} 次，单笔 ≤ 15 元"))

    # 坑 5：存在负数（退款）
    refunds = [t for t in transactions if t.amount < ZERO]
    detail = f"负数交易 {len(refunds)} 笔"
    if refunds:
        detail += f"，例如 {refunds[0].merchant} {refunds[0].amount}"
    results.append(("5 负数退款", bool(refunds), detail))

    # 坑 6：歧义商户的金额跨度足够大
    ambiguous = [t.amount for t in by_merchant.get(TRAP_AMBIGUOUS_MERCHANT, [])]
    smallest = min(ambiguous) if ambiguous else ZERO
    spread = (max(ambiguous) / smallest) if smallest > ZERO else ZERO
    results.append(("6 分类歧义", spread >= TRAP_AMBIGUOUS_SPREAD,
                    f"{TRAP_AMBIGUOUS_MERCHANT} 出现 {len(ambiguous)} 次，最大/最小 = {spread:.1f}×"))

    return results


def _parse_day(text: str) -> date:
    """命令行里的日期。出错时报清楚它该长什么样。"""
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"日期要写成 YYYY-MM-DD，收到了 {text!r}") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m data.generate", description="合成账单生成器")
    parser.add_argument("--out-dir", type=Path, default=None, help="输出目录，默认 data/")
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument(
        "--end", type=_parse_day, default=None,
        help="结束日 YYYY-MM-DD，默认今天。想复现旧的分析结果就把它钉回去。",
    )
    parser.add_argument("--check", action="store_true", help="只验证 6 个坑，不写文件")
    args = parser.parse_args(argv)

    from fa.config import DATA_DIR

    out_dir = args.out_dir or DATA_DIR
    transactions, reference = build(args.seed, args.end)

    if args.check:
        results = check_traps(transactions, reference)
        for name, ok, detail in results:
            print(f"  {'OK  ' if ok else 'GONE'}  {name}  —— {detail}")
        missing = [name for name, ok, _ in results if not ok]
        if missing:
            print(f"\n有 {len(missing)} 个坑不在数据里：{'、'.join(missing)}")
            return 1
        print("\n6 个坑都在。")
        return 0

    write_csv(transactions, out_dir / "transactions.csv")
    write_reference(reference, out_dir / "reference.csv")

    total = sum((t.amount for t in transactions), ZERO)
    counts: dict[str, int] = defaultdict(int)
    for category in reference.values():
        counts[category] += 1

    print(f"共 {len(transactions)} 笔，跨度 {transactions[0].date} ~ {transactions[-1].date}")
    print(f"净支出 {total:,.2f}")
    print("类目分布：" + "、".join(f"{k} {v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])))
    print(f"\n写到 {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
