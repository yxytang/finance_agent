"""测试共用的 fixture。

原则：**不碰仓库里的真实产物**。账单由 `make_bill` 现造，skill 目录换到临时
目录 —— 否则改一次 `data/generate.py` 的参数就会弄红一堆和它无关的测试。
"""

import pytest

from fa import config


@pytest.fixture
def skills_dir(tmp_path, monkeypatch):
    """把 skill 目录换成一个空的临时目录，返回它的 Path。

    `fa.skills.discover()` 在**调用时**读 `config.SKILLS_DIR`（不是 import 时
    抄一份快照），所以这里 patch 得动它。这条约定在 forge 上踩过两次，
    搬过来时原样保留。
    """
    directory = (tmp_path / "skills").resolve()
    directory.mkdir()
    monkeypatch.setattr(config, "SKILLS_DIR", directory)
    return directory


@pytest.fixture
def make_skill(skills_dir):
    """建一个 skill 目录，返回它的 Path。"""

    def build(name: str, text: str):
        directory = skills_dir / name
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(text, encoding="utf-8")
        return directory

    return build


@pytest.fixture
def make_bill(tmp_path):
    """手写一份小账单 CSV，返回它的 Path。

    刻意不用 `data/generate.py` 的产物：那些测试要钉的是 ingest 的**边界**
    （列名缺失、符号翻转、坏日期），手写三五行比造一千行清楚得多，也不会
    因为生成器调参而跟着抖。
    """

    def build(rows: list[str], header: str | None = None, name: str = "bill.csv"):
        path = tmp_path / name
        head = header or "txn_id,date,merchant,amount,account"
        path.write_text("\n".join([head, *rows]) + "\n", encoding="utf-8")
        return path

    return build
