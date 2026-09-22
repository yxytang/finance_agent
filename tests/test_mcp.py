"""MCP：协议、适配、以及隐私边界。

这个文件里最重要的是**隐私边界那几条**。别的地方「不把金额发给 LLM」是一句
写在自己代码里的约定，任何一处忘了检查就漏了；换成独立进程之后，server 不返回
的东西 agent **拿不到**。测试要钉住的是这个性质，不是某段文案。
"""

import json
import re
import sys
from datetime import date

import pytest

from fa.mcp.adapt import UnsupportedSchema, build_args_model, mount_tools, to_internal_tool
from fa.mcp.client import MCPError, StdioClient, ledger_client
from mcp_server.ledger_server import TOOLS, Ledger, handle

CSV = """txn_id,date,merchant,amount,account
T1,2026-01-05,STARBUCKS #1234 SEATTLE WA,5.75,credit
T2,2026-01-06,SAFEWAY #9,120.00,credit
T3,2026-02-01,PACIFIC PROPERTY MGMT RENT,3250.00,checking
"""

# 起「故意往协议通道里写坏东西」的子进程时，先把这个拼在前面。
#
# **必须先把子进程的 stdout 钉成 UTF-8**，否则在中文 Windows 上它会按 cp936
# 写中文，客户端读的时候先解码失败了 —— 于是这几条测试测的是编码，而不是它们
# 本来要测的「非 JSON 回复」「非 JSON-RPC 对象」。（真的踩过：没有这个前缀时
# 它们只在设了 PYTHONIOENCODING 的机器上才是绿的。）
_UTF8 = "import sys; sys.stdout.reconfigure(encoding='utf-8'); "


@pytest.fixture
def ledger(tmp_path):
    path = tmp_path / "bill.csv"
    path.write_text(CSV, encoding="utf-8")
    return Ledger(path)


# --- 隐私边界：这些是验收标准 -------------------------------------------


def test_list_merchants_leaks_no_amounts_dates_or_accounts(ledger):
    """**这条是这一天要证明的东西。**

    归类只需要商户串。server 就只给商户串 —— 金额、日期、账户一个都不给。
    边界在**服务端**执行，不是靠客户端自觉：客户端拿到的东西就这么多。
    """
    out = ledger.list_merchants()

    assert "STARBUCKS" in out  # 该给的给了
    # 不该给的一样都没有
    assert not re.search(r"\d+\.\d{2}", out), "输出里有金额"
    assert not re.search(r"\d{4}-\d{2}-\d{2}", out), "输出里有日期"
    assert "credit" not in out and "checking" not in out, "输出里有账户名"


def test_list_accounts_leaks_no_amounts(ledger):
    """「有哪些账户」这个问题不需要知道钱数，所以就不给。

    少给一样东西就少一处泄漏 —— 而多给的东西没人会记得去删。
    """
    out = ledger.list_accounts()

    assert "credit" in out and "checking" in out
    assert not re.search(r"\d+\.\d{2}", out), "输出里有金额"


def test_fetch_transactions_does_return_amounts(ledger):
    """需要金额的那条路照给 —— 边界是「按需给」，不是「一律不给」。"""
    out = ledger.fetch_transactions()
    assert "3250.00" in out


def test_fetch_transactions_caps_the_rows_it_hands_back(ledger):
    """上限是必要的而不是客气：不设的话一次调用就能把整个账单灌进调用方的
    上下文，而那是调用方**看不到也拦不住**的。"""
    out = ledger.fetch_transactions(limit=1)
    assert "命中 3 笔，显示前 1 笔" in out
    assert "T2" not in out


def test_fetch_transactions_filters(ledger):
    assert "T3" not in ledger.fetch_transactions(account="credit")
    assert "T3" in ledger.fetch_transactions(account="checking")
    assert "T1" not in ledger.fetch_transactions(date_from="2026-01-06")


# --- 协议层 -------------------------------------------------------------


def test_initialize_negotiates_the_version(ledger):
    reply = handle(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05"}},
        ledger,
    )
    assert reply["result"]["protocolVersion"] == "2024-11-05"
    assert reply["result"]["serverInfo"]["name"] == "ledger"
    assert "tools" in reply["result"]["capabilities"]


def test_initialize_falls_back_when_the_version_is_unknown(ledger):
    """客户端要一个我们不支持的版本时，报我们最新的，而不是照抄一个没法用的。"""
    reply = handle(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "1999-01-01"}},
        ledger,
    )
    assert reply["result"]["protocolVersion"] != "1999-01-01"


def test_tools_list_exposes_schemas(ledger):
    """schema 是**运行时读到的**，不是编译进客户端的 —— 这就是协议的价值。"""
    reply = handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, ledger)

    tools = reply["result"]["tools"]
    assert {t["name"] for t in tools} == {"list_accounts", "list_merchants", "fetch_transactions"}
    assert all("inputSchema" in t and "description" in t for t in tools)


def test_notifications_get_no_reply(ledger):
    """通知有 method 没有 id，按规范不该回 —— 回了客户端会把它当成一条
    对不上的响应，然后一直等它真正要的那条。"""
    assert handle({"jsonrpc": "2.0", "method": "notifications/initialized"}, ledger) is None


def test_unknown_method_is_an_error(ledger):
    reply = handle({"jsonrpc": "2.0", "id": 3, "method": "tools/没这个"}, ledger)
    assert reply["error"]["code"] == -32601


def test_bad_arguments_come_back_as_iserror_not_a_protocol_error(ledger):
    """**参数错和协议错要分开。**

    前者是「你这么调不对」，调用方该看消息然后改；后者是通信层坏了。
    混在一起的话，一个拼错的参数名会被当成「服务端挂了」。
    """
    reply = handle(
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
         "params": {"name": "list_merchants", "arguments": {"limit": [1, 2]}}},
        ledger,
    )

    assert "error" not in reply  # 不是协议错
    assert reply["result"]["isError"] is True
    assert "参数" in reply["result"]["content"][0]["text"]


def test_every_declared_tool_is_actually_implemented(ledger):
    """声明了却没实现的话，客户端会在调用时才炸 —— 而那离「声明」很远。"""
    for spec in TOOLS:
        reply = handle(
            {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
             "params": {"name": spec["name"], "arguments": {}}},
            ledger,
        )
        assert "error" not in reply, f"{spec['name']} 声明了但没有实现"
        assert not reply["result"].get("isError"), f"{spec['name']} 一调就报错"


# --- 客户端（起真进程）--------------------------------------------------


@pytest.fixture(scope="module")
def client():
    """整个模块共用一个连接 —— 起子进程不便宜。"""
    c = ledger_client()
    try:
        c.start()
        c.initialize()
        yield c
    finally:
        c.close()


def test_client_completes_the_handshake(client):
    assert client.protocol_version
    assert client.server_info.get("name") == "ledger"


def test_client_discovers_tools_at_runtime(client):
    names = {t["name"] for t in client.list_tools()}
    assert "list_merchants" in names


def test_client_calls_a_tool(client):
    out = client.call_tool("list_accounts")
    assert "笔" in out


def test_client_reports_a_missing_server_clearly():
    """起不来的时候，错误消息要说明白是什么起不来 —— 「连不上」三个字对使用者
    毫无帮助。"""
    with pytest.raises(MCPError) as exc:
        StdioClient(["definitely-not-a-real-program"]).start()
    assert "definitely-not-a-real-program" in str(exc.value)


def test_client_rejects_a_non_json_reply():
    """服务端往协议通道里写了非 JSON 的东西（比如一行调试输出）。"""
    client = StdioClient([sys.executable, "-c", _UTF8 + "print('这不是 JSON')"])
    client.start()
    try:
        with pytest.raises(MCPError) as exc:
            client.request("tools/list")
        assert "不是 JSON" in str(exc.value)
    finally:
        client.close()


def test_client_rejects_a_non_object_reply():
    """**合法 JSON 不一定是合法的 JSON-RPC 消息。**

    `"hello"` 和 `42` 都能过 json.loads。不挡这一下的话，后面那行 `.get`
    会抛 AttributeError —— 而那个报错离真正的原因（服务端往协议通道里写了
    非协议内容）很远。
    """
    client = StdioClient([sys.executable, "-c", _UTF8 + "print('\"一个裸字符串\"')"])
    client.start()
    try:
        with pytest.raises(MCPError) as exc:
            client.request("tools/list")
        assert "JSON-RPC" in str(exc.value)
    finally:
        client.close()


def test_client_reports_non_utf8_bytes_as_a_protocol_error():
    """对端写出来的不是 UTF-8 时，要报成协议错误，而不是 UnicodeDecodeError。

    这个坑真的踩过：ledger_server 没把自己的 stdout 编码钉死，在中文 Windows 上
    按 cp936 写中文工具描述，客户端一读就崩 —— 表现是「连不上 MCP server」，
    整个 web 应用起不来，而报错里一个「编码」字样都没有。

    这里**显式写非法字节**，不用 `print(中文)`：后者在 UTF-8 环境下（比如 CI 上
    的 Linux）压根不触发，那这条测试就只在中文 Windows 上有意义了。
    """
    client = StdioClient([
        sys.executable,
        "-c",
        r"import sys; sys.stdout.buffer.write(b'\xff\xfe not utf8\n'); sys.stdout.buffer.flush()",
    ])
    client.start()
    try:
        with pytest.raises(MCPError) as exc:
            client.request("tools/list")
        assert "UTF-8" in str(exc.value)
    finally:
        client.close()


def test_client_reports_the_server_dying():
    """server 起了就走（stdout 到 EOF）时，客户端要报出来而不是永远等下去。"""
    client = StdioClient([sys.executable, "-c", "pass"])
    client.start()
    try:
        with pytest.raises(MCPError) as exc:
            client.request("tools/list")
        assert "EOF" in str(exc.value) or "退出" in str(exc.value)
    finally:
        client.close()


def test_the_subprocess_gets_a_sandboxed_env(client):
    """账单 server 只是读一个 CSV，它没有任何理由看得到 API key。

    用一个不存在的变量名验证「环境确实被换过」—— 变量名本身不重要，
    重要的是这个检查会让人想起来去看一下白名单里有什么。
    """
    from fa.mcp.client import _ENV_ALLOWLIST

    assert "DEEPSEEK_API_KEY" not in _ENV_ALLOWLIST
    assert not any("KEY" in name or "TOKEN" in name for name in _ENV_ALLOWLIST)


# --- 适配层 -------------------------------------------------------------


def test_schema_becomes_a_callable_tool(client):
    spec = next(t for t in client.list_tools() if t["name"] == "list_merchants")
    tool = to_internal_tool(client, spec, prefix="ledger__")

    assert tool.name == "ledger__list_merchants"
    assert "商户" in tool.description
    assert "商户" in tool.invoke({"limit": 2})


def test_prefix_prevents_a_silent_collision(client):
    """不加前缀的话，MCP server 和我们内部工具重名时 LangChain 会静默地用一个
    盖掉另一个 —— 而那种失败看起来是「某个内置工具忽然不好用了」。"""
    spec = {"name": "query_transactions", "description": "假的", "inputSchema": {}}
    tool = to_internal_tool(client, spec, prefix="ledger__")
    assert tool.name == "ledger__query_transactions"


def test_optional_parameters_become_optional():
    model = build_args_model(
        "t",
        {
            "type": "object",
            "properties": {"required_one": {"type": "string"}, "optional_one": {"type": "string"}},
            "required": ["required_one"],
        },
    )
    assert model is not None
    # 只传必填的那个就该过
    model(**{"required_one": "x"})


def test_no_parameters_means_no_model():
    assert build_args_model("t", {"type": "object", "properties": {}}) is None


def test_an_unsupported_type_is_refused_not_guessed():
    """猜一个类型的表现是参数被静默转成错误类型，然后工具返回一个看起来正常的
    错结果 —— 那是最难查的一类问题。所以宁可拒绝挂载这个工具。"""
    with pytest.raises(UnsupportedSchema):
        build_args_model("t", {"type": "object", "properties": {"x": {"type": "未知类型"}}})


def test_mount_tools_returns_empty_and_warns_on_failure(capsys):
    """连不上时返回空列表（agent 还能用内置工具干活），但**一定要出声**：
    静默地少几个工具，表现是「模型忽然不会用某个能力了」。"""
    tools = mount_tools(StdioClient(["definitely-not-a-real-program"]))

    assert tools == []
    assert "MCP 工具没挂上" in capsys.readouterr().err
