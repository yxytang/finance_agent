"""CSV 读入。

钉的重点是**报错信息里有没有能让用户动手改的东西** —— 行号、列名。
「读取失败」这四个字对用户毫无用处。
"""

from datetime import date
from decimal import Decimal

import pytest

from fa.ingest import IngestError, load_transactions, parse_date


# --- 日期 ---------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2026-01-05", date(2026, 1, 5)),
        ("2026/01/05", date(2026, 1, 5)),
        ("01/05/2026", date(2026, 1, 5)),
        ("  2026-01-05  ", date(2026, 1, 5)),
    ],
)
def test_parse_date_accepts_the_common_formats(raw, expected):
    assert parse_date(raw) == expected


def test_parse_date_rejects_nonsense_with_the_format_list():
    with pytest.raises(IngestError) as exc:
        parse_date("去年三月")
    assert "去年三月" in str(exc.value)


# --- 正常路径 -----------------------------------------------------------


def test_loads_rows(make_bill):
    path = make_bill([
        "T1,2026-01-05,STARBUCKS #1234,5.75,credit",
        "T2,2026-01-06,SAFEWAY #9,42.10,credit",
    ])

    txns = load_transactions(path)

    assert [t.txn_id for t in txns] == ["T1", "T2"]
    assert txns[0].date == date(2026, 1, 5)
    assert txns[0].merchant == "STARBUCKS #1234"
    assert txns[0].amount == Decimal("5.75")
    assert txns[0].account == "credit"


def test_ingested_transactions_are_uncategorized(make_bill):
    """原始账单里没有类目 —— 那是第四天分类层要产出的东西。
    ingest 顺手填一个，就等于把答案白送给 agent。"""
    path = make_bill(["T1,2026-01-05,X,5.75,credit"])
    assert load_transactions(path)[0].category is None


def test_flip_sign_for_bank_exports(make_bill):
    """多数银行导出是「支出为负」，我们的约定反过来。这是接真实数据时
    第一个要拧的旋钮。"""
    path = make_bill(["T1,2026-01-05,STARBUCKS,-5.75,credit"])

    assert load_transactions(path)[0].amount == Decimal("-5.75")
    assert load_transactions(path, flip_sign=True)[0].amount == Decimal("5.75")


def test_custom_column_names(make_bill):
    """真实导出的列名千奇百怪，硬编码等于把自己锁死在一个格式上。"""
    path = make_bill(
        ["T1,2026-01-05,STARBUCKS #1234,5.75,credit"],
        header="id,交易日期,摘要,金额,卡号",
    )

    txns = load_transactions(path, columns={
        "txn_id": "id", "date": "交易日期", "merchant": "摘要",
        "amount": "金额", "account": "卡号",
    })

    assert txns[0].txn_id == "T1"
    assert txns[0].date == date(2026, 1, 5)
    assert txns[0].account == "credit"


def test_does_not_dedupe_by_txn_id(make_bill):
    """刻意不去重。

    ingest 按 id 判重、find_duplicates 按「同商户+同金额+时间窗」判重 ——
    两份「重复」的定义会打架。真实的双重扣款是两笔**不同 id** 的同额交易，
    ingest 那条规则根本拦不住，却会在别处悄悄吞数据。判重是异常检测的活。
    """
    path = make_bill([
        "T1,2026-01-05,TARGET,188.40,credit",
        "T1,2026-01-05,TARGET,188.40,credit",
    ])
    assert len(load_transactions(path)) == 2


# --- 出错 ---------------------------------------------------------------


def test_missing_column_names_it_and_lists_what_is_there(make_bill):
    path = make_bill(["T1,2026-01-05,X,5.75"], header="txn_id,date,merchant,amount")

    with pytest.raises(IngestError) as exc:
        load_transactions(path)

    message = str(exc.value)
    assert "account" in message  # 缺了哪个
    assert "txn_id" in message   # 实际有哪些，用户才能对照着改


def test_bad_row_names_the_line_number(make_bill):
    path = make_bill([
        "T1,2026-01-05,X,5.75,credit",     # 第 2 行
        "T2,不是日期,Y,1.00,credit",        # 第 3 行
    ])

    with pytest.raises(IngestError) as exc:
        load_transactions(path)

    assert "第 3 行" in str(exc.value)


def test_bad_amount_names_the_line_number(make_bill):
    path = make_bill([
        "T1,2026-01-05,X,5.75,credit",
        "T2,2026-01-06,Y,一百块,credit",
    ])

    with pytest.raises(IngestError) as exc:
        load_transactions(path)

    assert "第 3 行" in str(exc.value)


def test_missing_file_says_what_to_run(tmp_path):
    with pytest.raises(IngestError) as exc:
        load_transactions(tmp_path / "不存在.csv")
    assert "data.generate" in str(exc.value)
