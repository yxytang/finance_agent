"""合成账单生成器。

这里钉的是两条**地基性质**：

  1. **可复现** —— 同 seed + 同结束日必得同一份数据。结束日默认跟今天走，
     所以「跑两次一样」说的是**同一天内**；要跨天复现就得把 `--end` 钉回去。
     月数、总数都随结束日变，所以这里一律**不断言具体月数或总数** —— 断言
     那些会把这条测试变成定时炸弹。
  2. **6 个坑真的在数据里** —— 后面所有异常检测的评测都拿它们当真值。
     坑没了而没人发现，评测会「全过」，那比失败更糟。
"""

from collections import Counter
from datetime import date

import pytest

from data.generate import START, build, check_traps, write_csv, write_reference
from fa.config import CATEGORIES

RENT = "PACIFIC PROPERTY MGMT RENT"


def _month_range(first: date, last: date) -> set[str]:
    """首月到末月之间**应该存在**的所有 year_month，含首尾。"""
    out: set[str] = set()
    year, month = first.year, first.month
    while (year, month) <= (last.year, last.month):
        out.add(f"{year:04d}-{month:02d}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return out


@pytest.fixture(scope="module")
def bill():
    """整个模块共用一份 —— 生成 1000 多笔不便宜，而它又是只读的。"""
    return build()


# --- 确定性 -------------------------------------------------------------


def test_same_seed_gives_the_same_bill():
    a, ref_a = build()
    b, ref_b = build()

    assert [t.txn_id for t in a] == [t.txn_id for t in b]
    assert [t.date for t in a] == [t.date for t in b]
    assert [t.amount for t in a] == [t.amount for t in b]
    assert ref_a == ref_b


def test_different_seed_gives_a_different_bill():
    """种子要是根本没接上，上一个测试也会通过 —— 这条防的是那个。"""
    a, _ = build()
    b, _ = build(seed=1)
    assert [t.amount for t in a] != [t.amount for t in b]


def test_extending_the_end_date_only_appends():
    """延长结束日只能是**追加**，前面一笔都不能动。

    「把账单延到今天」这件事能不能成立，全看这条：`end` 不参与抽样，所以换个
    结束日拿到的永远是同一条随机序列的前缀。一旦有人把 `end` 混进某个 `rng.*`
    调用，前面几个月会整体错位 —— 而**没有任何报错**，只有一堆对不上的数字，
    连那 6 个坑都可能悄悄挪走。
    """
    cutoff = date(2026, 3, 31)
    short, _ = build(end=cutoff)
    long, _ = build(end=date(2026, 6, 30))

    prefix = [t for t in long if t.date <= cutoff]

    assert [t.date for t in prefix] == [t.date for t in short]
    assert [t.merchant for t in prefix] == [t.merchant for t in short]
    assert [t.amount for t in prefix] == [t.amount for t in short]


# --- 6 个坑 -------------------------------------------------------------


def test_all_six_traps_are_present(bill):
    transactions, reference = build()

    results = check_traps(transactions, reference)

    assert len(results) == 6
    missing = [name for name, ok, _ in results if not ok]
    assert not missing, f"这些坑不在数据里了：{missing}"


def test_trap_check_can_actually_fail():
    """反查本身要能红。

    一个永远返回「全部通过」的检查比没有检查更糟 —— 它给人虚假的安全感。
    这里把数据挖空，确认它真的会报出来。
    """
    results = check_traps([], {})
    assert len(results) == 6
    assert not any(ok for _, ok, _ in results)


def test_trap_six_does_not_depend_on_a_lucky_draw(bill):
    """坑 6 的两个端点是**写死**的。

    第一版把它交给随机金额去碰，结果调了一下购物频次、RNG 流跟着变，
    金额跨度从 12× 掉到 6.7×，坑就没了 —— 而且没有任何测试会红。
    """
    transactions, reference = build()
    _, ok, detail = check_traps(transactions, reference)[5]
    assert ok, detail


# --- 数据形状 -----------------------------------------------------------


def test_size_and_span(bill):
    transactions, _ = bill
    months = len({t.year_month for t in transactions})

    # 上下界按「每月多少笔」给，不写死总数 —— 总数随结束日变（见 generate.py
    # 的 docstring），写死它就是个定时炸弹。
    assert 50 * months <= len(transactions) <= 120 * months
    assert transactions[0].date >= START
    assert transactions[-1].date <= date.today()


def test_no_month_is_empty(bill):
    """首月到末月之间一个月都不能少。

    空月份会让「按月分组」的测试碰到一个不存在的桶，而那种失败看起来像查询
    bug，其实怪数据。**断言的是没有空档，不是月数** —— 月数会随结束日变。
    """
    transactions, _ = bill
    months = Counter(t.year_month for t in transactions)

    assert set(months) == _month_range(transactions[0].date, transactions[-1].date)
    assert min(months.values()) > 0


def test_monthly_items_appear_in_every_month(bill):
    transactions, _ = bill
    every_month = {t.year_month for t in transactions}

    rent = [t for t in transactions if t.merchant == RENT]

    assert len(rent) == len(every_month)
    assert {t.year_month for t in rent} == every_month


def test_true_categories_come_from_the_fixed_list(bill):
    """真值类目掺进一个「伙食费」，分类评测的 per-class 表就会多出一行
    谁也不认识的类目。"""
    _, reference = bill
    assert set(reference.values()) <= set(CATEGORIES)


def test_transactions_carry_no_category(bill):
    """transactions.csv 是给 agent 的**输入**，带上答案就等于泄题。"""
    transactions, _ = bill
    assert all(t.category is None for t in transactions)


def test_txn_ids_are_unique_and_ordered(bill):
    transactions, reference = bill

    ids = [t.txn_id for t in transactions]

    assert len(set(ids)) == len(ids)
    assert ids == sorted(ids)
    assert set(ids) == set(reference)


# --- 落盘 ---------------------------------------------------------------


def test_write_csv_does_not_leak_the_answer(tmp_path, bill):
    transactions, _ = bill
    path = tmp_path / "transactions.csv"

    write_csv(transactions, path)

    header = path.read_text(encoding="utf-8").splitlines()[0]
    assert "category" not in header
    assert header.split(",") == ["txn_id", "date", "merchant", "amount", "account"]


def test_reference_covers_every_transaction(tmp_path, bill):
    transactions, reference = bill
    path = tmp_path / "reference.csv"

    write_reference(reference, path)

    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == len(transactions) + 1  # 表头
