"""账单数据源 MCP server —— JSON-RPC over stdio，从零手写，不装 SDK。

## 为什么账单要放在一个独立进程里

真实世界里银行数据本来就来自外部服务，把账单包成 MCP server 不是硬凑 ——
它就是「数据源」这个概念的自然实现。

更有价值的是另一件事：**它是独立进程，所以边界是真的。**

在同一个进程里，「不把金额发给 LLM」只是一句约定 —— 任何一处忘了检查就漏了，
而且没有症状。跨进程之后，**server 不返回的东西，agent 拿不到**，不是「不该拿」。
约定会被人忘记，进程边界不会。

## 协议分层

    initialize               能力协商，双方报各自支持什么
      ↓
    notifications/initialized 客户端确认（通知，不需要回复）
      ↓
    tools/list               发现有哪些工具、参数是什么 schema
      ↓
    tools/call               调用，参数按 schema 校验

这三步的意义在于：**客户端不需要预先知道服务端有什么。** 工具是运行时发现的，
schema 是运行时读到的。所以换一个 server、加一个工具，客户端一行都不用改 ——
这就是它比「每家自己写 plugin」强的地方：plugin 要把对方的接口编进自己的代码里，
MCP 只要实现一次协议。

## 一个容易踩的坑

**stdout 是协议通道，一个多余的 print 就会把协议搅坏。**

调试信息必须走 stderr。这不是洁癖：混进去的那行会被客户端当成一个 JSON-RPC
消息去解析，解析失败，然后表现为「server 忽然不响应了」——
而真正的原因是一行无害的调试输出。
"""

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SERVER_NAME = "ledger"
SERVER_VERSION = "0.1.0"

# 我们支持的协议版本，从新到旧。
SUPPORTED_PROTOCOLS = ("2025-06-18", "2024-11-05")
LATEST_PROTOCOL = SUPPORTED_PROTOCOLS[0]

# JSON-RPC 标准错误码
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


def log(message: str) -> None:
    """调试输出**只能走 stderr**，见模块 docstring。"""
    print(message, file=sys.stderr, flush=True)


# --- 数据 ---------------------------------------------------------------


class Ledger:
    """账单数据。

    只做「取数据」这一件事 —— 分类、查询、异常检测全在客户端那边。
    一个 server 该做的就是把数据给出去，不是替客户端决定怎么用。
    """

    def __init__(self, csv_path: Path):
        self.csv_path = csv_path
        self._rows: list[dict] | None = None

    @property
    def rows(self) -> list[dict]:
        if self._rows is None:
            with self.csv_path.open("r", encoding="utf-8", newline="") as fh:
                self._rows = list(csv.DictReader(fh))
        return self._rows

    # --- 三个工具的实现 -------------------------------------------------

    def list_accounts(self) -> str:
        """账户清单 + 各自笔数。

        **刻意不返回金额。** 「有哪些账户」这个问题不需要知道钱数，
        而少给一样东西就少一处泄漏。
        """
        counts = Counter(row["account"] for row in self.rows)
        lines = [f"共 {len(self.rows)} 笔交易，分布在 {len(counts)} 个账户："]
        for account, count in sorted(counts.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {account}：{count} 笔")
        return "\n".join(lines)

    def list_merchants(self, limit: int = 200) -> str:
        """出现过的商户（去重、按笔数降序）。

        **这条是隐私边界的执行点。** 返回里没有金额、没有日期、没有账户 ——
        只有商户串本身。归类这件事只需要商户串，所以给这些就够了。

        注意这条边界是在**服务端**执行的，不是靠客户端自觉。客户端拿到的东西
        就这么多，它想多要也没有。
        """
        counts = Counter(row["merchant"] for row in self.rows)
        items = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
        lines = [f"共 {len(counts)} 个不同的商户（按笔数降序，最多 {limit} 个）："]
        lines += [f"  {merchant}  ({count} 笔)" for merchant, count in items]
        return "\n".join(lines)

    def fetch_transactions(
        self,
        date_from: str = "",
        date_to: str = "",
        account: str = "",
        limit: int = 50,
    ) -> str:
        """取交易明细，带金额。

        `limit` 是必要的而不是可选的客气：不设上限的话一次调用就能把整个账单
        灌进调用方的上下文，而那是调用方**看不到也拦不住**的 —— 数据在服务端，
        决定给多少也在服务端。
        """
        rows = self.rows
        if date_from:
            rows = [r for r in rows if r["date"] >= date_from]
        if date_to:
            rows = [r for r in rows if r["date"] <= date_to]
        if account:
            rows = [r for r in rows if r["account"] == account]

        shown = rows[:limit]
        header = f"命中 {len(rows)} 笔，显示前 {len(shown)} 笔" if len(rows) > len(shown) else f"命中 {len(rows)} 笔"
        lines = [header, "  txn_id      日期        金额        账户      商户"]
        lines += [
            f"  {r['txn_id']:<10}  {r['date']}  {r['amount']:>10}  "
            f"{r['account']:<8}  {r['merchant']}"
            for r in shown
        ]
        return "\n".join(lines)


# --- 工具声明（tools/list 返回的东西）-----------------------------------

TOOLS = [
    {
        "name": "list_accounts",
        "description": (
            "列出账单里有哪些账户，以及各自的交易笔数。**不返回金额。**"
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "list_merchants",
        "description": (
            "列出账单里出现过的商户（去重、按笔数降序）。"
            "**不返回金额、日期、账户** —— 只有商户串本身。"
            "需要给商户归类时用这个，它给的信息刚好够，不多给。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "最多返回多少个商户，默认 200。",
                }
            },
            "required": [],
        },
    },
    {
        "name": "fetch_transactions",
        "description": (
            "按时间范围、账户取交易明细，**带金额**。"
            "不传条件就是全部（受 limit 限制）。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "date_from": {"type": "string", "description": "起始日期 YYYY-MM-DD，含当天。"},
                "date_to": {"type": "string", "description": "结束日期 YYYY-MM-DD，含当天。"},
                "account": {"type": "string", "description": "账户名，不传就是全部账户。"},
                "limit": {"type": "integer", "description": "最多返回多少笔，默认 50。"},
            },
            "required": [],
        },
    },
]


# --- JSON-RPC 分发 ------------------------------------------------------


def _result(request_id, value) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": value}


def _error(request_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def handle(request: dict, ledger: Ledger) -> dict | None:
    """处理一条 JSON-RPC 消息。返回 None 表示这是通知，不用回。"""
    method = request.get("method")
    request_id = request.get("id")
    params = request.get("params") or {}

    if method == "initialize":
        # 客户端报它想用的版本，我们支持就跟着它，不支持就报我们最新的。
        # 这一步就是「能力协商」的全部含义 —— 双方先谈好按哪版说话。
        wanted = params.get("protocolVersion")
        agreed = wanted if wanted in SUPPORTED_PROTOCOLS else LATEST_PROTOCOL
        return _result(
            request_id,
            {
                "protocolVersion": agreed,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        )

    if method in ("notifications/initialized", "initialized"):
        log("客户端确认初始化完成")
        return None

    if method == "tools/list":
        return _result(request_id, {"tools": TOOLS})

    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        handler = {
            "list_accounts": lambda **_: ledger.list_accounts(),
            "list_merchants": ledger.list_merchants,
            "fetch_transactions": ledger.fetch_transactions,
        }.get(name)

        if handler is None:
            return _error(request_id, INVALID_PARAMS, f"没有名为 {name!r} 的工具")

        try:
            text = handler(**arguments)
        except TypeError as exc:
            # 参数不匹配是**调用方**的问题，要让它能看懂并改 —— 所以走
            # isError 而不是 JSON-RPC error：前者是「工具执行失败」，
            # 后者是「协议层出错」，客户端的处理方式不一样。
            return _result(
                request_id,
                {
                    "content": [{"type": "text", "text": f"参数不对：{exc}"}],
                    "isError": True,
                },
            )

        return _result(request_id, {"content": [{"type": "text", "text": text}]})

    if request_id is None:
        return None  # 不认识的**通知**，按规范忽略
    return _error(request_id, METHOD_NOT_FOUND, f"不认识的方法：{method}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ledger_server", description="账单数据源 MCP server")
    parser.add_argument(
        "--data",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "data" / "transactions.csv",
    )
    args = parser.parse_args(argv)

    if not args.data.is_file():
        log(f"找不到账单：{args.data}")
        return 1

    ledger = Ledger(args.data)
    log(f"{SERVER_NAME} 启动，数据来自 {args.data}")

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            # id 是 null：连请求都解析不出来，没法回给某一条
            response = _error(None, PARSE_ERROR, "这行不是合法的 JSON")
        else:
            response = handle(request, ledger)

        if response is not None:
            # 协议通道是 stdout。**每次都要 flush** —— 缓冲区里压着的响应
            # 在没有 flush 的情况下会让客户端一直等，看起来像 server 卡死了。
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
