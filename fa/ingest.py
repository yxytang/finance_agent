"""CSV → list[Transaction]。

两个旋钮是为真实数据准备的，合成数据上看起来多余，换成真账单时第一个就要拧：

  · `columns` —— 列名映射。真实导出的列名千奇百怪（`Transaction Date` /
    `交易日期`、`Description` / `摘要`），硬编码等于把自己锁死在一个格式上。
  · `flip_sign` —— 符号翻转。我们的约定是支出为正，而多数银行导出是支出为负。

**刻意不做 txn_id 去重。** 看着像是个稳妥的兜底，其实会让「重复」出现两个
打架的定义：ingest 按 id 判重，而第七天的 `find_duplicates` 按「同商户 + 同金额
+ 时间窗」判重。真实的双重扣款是两笔**不同 id** 的同额交易，ingest 那条规则
根本拦不住它，却会在别的地方悄悄吞掉数据。判重是异常检测的活，不该在入口
再定义一遍。
"""

import csv
from datetime import date, datetime
from decimal import InvalidOperation
from pathlib import Path

from fa.models import Transaction, Money, money

DEFAULT_COLUMNS = {
    "date": "date",
    "merchant": "merchant",
    "amount": "amount",
    "account": "account",
    "txn_id": "txn_id",
}

_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y")


class IngestError(Exception):
    """CSV 读不了。消息里一律带**行号或列名** —— 那是唯一能让用户动手改的信息。"""


def parse_date(raw: str) -> date:
    text = raw.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise IngestError(
        f"认不出这个日期：{raw!r}（支持 {'、'.join(_DATE_FORMATS)}）"
    )


def parse_amount(raw: str) -> Money:
    """转成金额。

    `money()` 遇到「一百块」这种会抛 `decimal.InvalidOperation`，
    而它的消息是 `[<class 'decimal.ConversionSyntax'>]` —— 对用户完全没用。
    所以在这一层拦下来，换成带上原始值的说法。
    """
    try:
        return money(raw)
    except InvalidOperation as exc:
        raise IngestError(f"认不出这个金额：{raw!r}") from exc


def load_transactions(
    path: Path,
    columns: dict[str, str] | None = None,
    flip_sign: bool = False,
) -> list[Transaction]:
    """读一份账单 CSV，返回交易列表。

    columns 只需要给出要覆盖的那几项，其余走 DEFAULT_COLUMNS。
    flip_sign=True 把金额符号翻过来，给「支出为负」的银行导出用。
    """
    mapping = {**DEFAULT_COLUMNS, **(columns or {})}

    if not path.is_file():
        raise IngestError(
            f"找不到 {path}。先跑 `python -m data.generate` 生成合成账单。"
        )

    rows: list[Transaction] = []
    with path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        header = reader.fieldnames or []

        missing = sorted(set(mapping.values()) - set(header))
        if missing:
            raise IngestError(
                f"{path.name} 里没有这些列：{'、'.join(missing)}。"
                f"实际有的是：{'、'.join(header)}"
            )

        for lineno, raw in enumerate(reader, start=2):
            try:
                amount = parse_amount(raw[mapping["amount"]])
                if flip_sign:
                    amount = -amount
                rows.append(
                    Transaction(
                        date=parse_date(raw[mapping["date"]]),
                        merchant=raw[mapping["merchant"]].strip(),
                        amount=amount,
                        account=raw[mapping["account"]].strip(),
                        txn_id=raw[mapping["txn_id"]].strip(),
                    )
                )
            except (KeyError, AttributeError) as exc:
                raise IngestError(f"{path.name} 第 {lineno} 行读取失败：{exc}") from exc
            except IngestError as exc:
                raise IngestError(f"{path.name} 第 {lineno} 行：{exc}") from exc

    return rows
