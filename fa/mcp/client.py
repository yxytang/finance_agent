"""MCP 客户端 —— 手写 JSON-RPC over stdio，不装 SDK。

## 为什么手写

装个 SDK 当然更快。但那样这个项目就变成「我调用了某个库」，而不是「我知道这个
协议长什么样」—— 而后者才是这里要展示的东西。

协议本身很小：一行一条 JSON 出去，一行一条 JSON 回来。

    出去  {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    回来  {"jsonrpc": "2.0", "id": 1, "result": {"tools": [...]}}

手写一遍之后，「为什么 MCP 比每家自己写 plugin 好」才有具体的答案：
**因为客户端读的是运行时的 schema，不是编译进代码的接口。**

## 两个传输层的细节

**stderr 不接管道，直接继承。** 接管道而不读的话，服务端写满管道缓冲区就会
阻塞在写 stderr 上，然后客户端在等它的 stdout —— 双方互相等，**死锁**。
这是子进程通信里最经典的一个坑，而它的表现是「跑着跑着不动了」，
极难定位。继承 stderr 就没有这个问题，顺带日志直接打在控制台上。

**每次写完都要 flush。** 缓冲里压着的请求会让服务端一直等，看起来像它卡住了。
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
LEDGER_SERVER = PROJECT_ROOT / "mcp_server" / "ledger_server.py"

DEFAULT_TIMEOUT = 30.0


class MCPError(RuntimeError):
    """和 MCP server 打交道时出的问题。

    一律带上足够的上下文（方法名、服务端说了什么），否则「连不上」这三个字
    对使用者毫无帮助。
    """


class StdioClient:
    """把一个 MCP server 当子进程起来，通过标准输入输出和它说话。"""

    def __init__(self, command: list[str], *, cwd: Path | None = None, sandbox: bool = True):
        self.command = command
        self.cwd = cwd
        self.sandbox = sandbox
        self._process: subprocess.Popen | None = None
        self._next_id = 0
        self.server_info: dict = {}
        self.protocol_version: str = ""

    # --- 生命周期 -------------------------------------------------------

    def start(self) -> "StdioClient":
        if self._process is not None:
            return self
        try:
            self._process = subprocess.Popen(
                self.command,
                cwd=str(self.cwd) if self.cwd else None,
                env=_sandboxed_env() if self.sandbox else None,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                # 见模块 docstring：接管道而不读会死锁，所以让它直接继承。
                stderr=None,
                text=True,
                encoding="utf-8",
                bufsize=1,
            )
        except OSError as exc:
            raise MCPError(f"起不了 MCP server（{self.command}）：{exc}") from exc
        return self

    def close(self) -> None:
        if self._process is None:
            return
        try:
            if self._process.stdin:
                self._process.stdin.close()
            self._process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            self._process.kill()
        finally:
            self._process = None

    def __enter__(self) -> "StdioClient":
        return self.start()

    def __exit__(self, *exc_info) -> None:
        self.close()

    # --- 协议 -----------------------------------------------------------

    def _write(self, payload: dict) -> None:
        if self._process is None or self._process.stdin is None:
            raise MCPError("客户端还没启动")
        self._process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self._process.stdin.flush()

    def _read_reply(self, request_id: int) -> dict:
        """读到我们要的那条回复为止。

        服务端可能**主动发通知**（比如日志），那些没有 id，要跳过 ——
        不跳的话，第一条通知就会被当成「我们的回复」，然后解析失败。
        """
        assert self._process is not None and self._process.stdout is not None

        while True:
            line = self._process.stdout.readline()
            if not line:
                raise MCPError("MCP server 没打招呼就退出了（stdout 到 EOF）")

            try:
                message = json.loads(line)
            except json.JSONDecodeError as exc:
                raise MCPError(f"MCP server 发来的不是 JSON：{line[:200]!r}") from exc

            # 合法 JSON 不一定是合法的 **JSON-RPC 消息** —— `"hello"` 和 `42`
            # 都能过 json.loads。不挡这一下的话，下面那行 `.get` 会抛
            # AttributeError，而那个报错离真正的原因（服务端往协议通道里
            # 写了非协议内容）很远。
            if not isinstance(message, dict):
                raise MCPError(f"MCP server 发来的不是 JSON-RPC 对象：{line[:200]!r}")

            if message.get("id") != request_id:
                continue
            if "error" in message:
                error = message["error"]
                raise MCPError(f"服务端报错 {error.get('code')}：{error.get('message')}")
            return message.get("result") or {}

    def request(self, method: str, params: dict | None = None) -> dict:
        self._next_id += 1
        request_id = self._next_id
        self._write(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
        )
        return self._read_reply(request_id)

    def notify(self, method: str, params: dict | None = None) -> None:
        """通知：有 method 没有 id，服务端不该回。"""
        self._write({"jsonrpc": "2.0", "method": method, "params": params or {}})

    # --- 三个高层动作（对应协议的三步）----------------------------------

    def initialize(self) -> dict:
        """能力协商。双方先谈好按哪版协议说话。"""
        result = self.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "finance-agent", "version": "0.1.0"},
            },
        )
        self.protocol_version = result.get("protocolVersion", "")
        self.server_info = result.get("serverInfo", {})
        # 协商完要发一条通知确认 —— 这是规范要求的，不是可选的。
        self.notify("notifications/initialized")
        return result

    def list_tools(self) -> list[dict]:
        """发现服务端有哪些工具。**工具是运行时读到的，不是编译进代码的。**"""
        return self.request("tools/list").get("tools", [])

    def call_tool(self, name: str, arguments: dict | None = None) -> str:
        """调一个工具，把返回的 content 拼成文本。

        注意 `isError` 的处理：工具**执行失败**和**协议出错**是两回事。
        前者说明参数或数据有问题，调用方该看消息然后改；后者是通信层坏了。
        混在一起的话，「参数写错了」会被当成「服务端挂了」。
        """
        result = self.request("tools/call", {"name": name, "arguments": arguments or {}})
        parts = [
            block.get("text", "")
            for block in result.get("content", [])
            if block.get("type") == "text"
        ]
        text = "\n".join(part for part in parts if part)
        if result.get("isError"):
            return f"工具 {name} 执行失败：{text}"
        return text


# 子进程**只需要**这些环境变量就能跑起来。
#
# 用允许清单而不是「过滤掉名字里带 KEY 的」：后者要猜哪些名字算敏感
# （`DEEPSEEK_API_KEY` 拦得住，`AWS_SESSION_TOKEN` 也拦得住，但下一个人加个
# `FOO_CRED` 就漏了）。允许清单的默认方向相反 —— 忘了加，子进程少一个变量，
# 大不了启动失败；漏一个，密钥就白送了。
_ENV_ALLOWLIST = frozenset(
    {
        "PATH", "PYTHONPATH", "PYTHONIOENCODING", "PYTHONUTF8",
        # Windows 上 Python 启动要用到这几个
        "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "TEMP", "TMP",
        "PATHEXT", "COMSPEC", "NUMBER_OF_PROCESSORS",
        # POSIX
        "HOME", "USERPROFILE", "LANG", "LC_ALL", "TZ",
    }
)


def _sandboxed_env() -> dict[str, str]:
    """给子进程一份干净的环境。

    账单 server 只读一个 CSV —— 它**没有任何理由**看得到 API key。
    权限最小化在这里是免费做到的（一个字典推导），不做就是在白送。
    """
    return {key: value for key, value in os.environ.items() if key.upper() in _ENV_ALLOWLIST}


def ledger_client(command: list[str] | None = None, *, sandbox: bool = True) -> StdioClient:
    """起一个账单 server 客户端。"""
    return StdioClient(
        command or [sys.executable, str(LEDGER_SERVER)],
        cwd=PROJECT_ROOT,
        sandbox=sandbox,
    )


def describe_tools(tools: list[dict]) -> str:
    """把 tools/list 的结果摊成人能读的样子（给 /mcp 命令用）。"""
    lines = []
    for tool in tools:
        schema: dict[str, Any] = tool.get("inputSchema") or {}
        params = ", ".join((schema.get("properties") or {}).keys()) or "（无参数）"
        lines.append(f"  {tool['name']:<22} {params}")
        lines.append(f"  {'':<22} {tool.get('description', '')}")
    return "\n".join(lines)
