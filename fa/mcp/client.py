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
import math
import queue
import threading
import time
from dataclasses import dataclass, field
import subprocess
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
LEDGER_SERVER = PROJECT_ROOT / "mcp_server" / "ledger_server.py"

DEFAULT_TIMEOUT = 30.0
MAX_REPLY_CHARS = 1_000_000


class MCPError(RuntimeError):
    """和 MCP server 打交道时出的问题。

    一律带上足够的上下文（方法名、服务端说了什么），否则「连不上」这三个字
    对使用者毫无帮助。
    """


@dataclass
class _Transport:
    process: subprocess.Popen
    stopped: threading.Event = field(default_factory=threading.Event)
    replies: queue.Queue = field(default_factory=lambda: queue.Queue(maxsize=128))
    writes: queue.Queue = field(default_factory=lambda: queue.Queue(maxsize=1))
    reader: threading.Thread | None = None
    writer: threading.Thread | None = None


def _offer(transport: _Transport, value) -> None:
    while not transport.stopped.is_set():
        try:
            transport.replies.put(value, timeout=0.1)
            return
        except queue.Full:
            pass


def _reader_loop(transport: _Transport) -> None:
    """One reader per connection; never one blocked thread per request."""
    try:
        while not transport.stopped.is_set():
            line = transport.process.stdout.readline(MAX_REPLY_CHARS + 1)
            if len(line) > MAX_REPLY_CHARS:
                _offer(transport, ("error", MCPError("MCP server 响应超过大小上限")))
                return
            if not line:
                _offer(transport, ("error", MCPError("MCP server 退出了（stdout 到 EOF）")))
                return
            _offer(transport, ("line", line))
    except UnicodeDecodeError as exc:
        _offer(transport, ("error", MCPError(f"MCP server 的协议通道不是 UTF-8：{exc}")))
    except (OSError, ValueError) as exc:
        _offer(transport, ("error", MCPError(f"MCP server 读取失败：{exc}")))


def _writer_loop(transport: _Transport) -> None:
    # A fixed writer also makes a full stdin pipe subject to the request deadline.
    # Terminating the child during close releases a blocked pipe write.
    while not transport.stopped.is_set():
        try:
            payload, done = transport.writes.get(timeout=0.1)
        except queue.Empty:
            continue
        error = None
        try:
            transport.process.stdin.write(payload)
            transport.process.stdin.flush()
        except (OSError, ValueError) as exc:
            error = MCPError(f"MCP server 写入失败：{exc}")
        done.put(error)
        if error is not None:
            return


class StdioClient:
    """Serialized stdio RPC with bounded queueing, writes and response waits."""

    def __init__(self, command: list[str], *, cwd: Path | None = None, sandbox: bool = True,
                 env_overrides: dict | None = None, timeout: float = DEFAULT_TIMEOUT):
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be positive and finite")
        self.command = command
        self.cwd = cwd
        self.sandbox = sandbox
        self.env_overrides = dict(env_overrides or {})
        if sandbox and any(key.upper() not in _ENV_ALLOWLIST | {"FINANCE_LEDGER_DATABASE_URL"}
                           for key in self.env_overrides):
            raise ValueError("MCP sandbox environment overrides must use the allowlist")
        self.timeout = timeout
        self._request_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._transport: _Transport | None = None
        self._process: subprocess.Popen | None = None
        self._next_id = 0
        self.server_info: dict = {}
        self.protocol_version: str = ""

    def start(self) -> "StdioClient":
        with self._state_lock:
            if self._transport is not None:
                return self
            try:
                process = subprocess.Popen(
                    self.command, cwd=str(self.cwd) if self.cwd else None,
                    env={**(_sandboxed_env() if self.sandbox else os.environ), **self.env_overrides},
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None,
                    text=True, encoding="utf-8", bufsize=1,
                )
            except OSError as exc:
                raise MCPError(f"起不了 MCP server（{self.command}）：{exc}") from exc
            transport = _Transport(process)
            transport.reader = threading.Thread(target=_reader_loop, args=(transport,),
                                                name=f"mcp-reader-{process.pid}", daemon=True)
            transport.writer = threading.Thread(target=_writer_loop, args=(transport,),
                                                name=f"mcp-writer-{process.pid}", daemon=True)
            self._transport = transport
            self._process = process
            transport.reader.start()
            transport.writer.start()
        return self

    def _disconnect(self, expected: _Transport | None = None) -> None:
        # Never wait for the request lock: close must interrupt an in-flight RPC.
        with self._state_lock:
            transport = self._transport
            if transport is None or (expected is not None and transport is not expected):
                return
            self._transport = None
            self._process = None
            transport.stopped.set()
        process = transport.process
        if process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=0.5)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                except OSError:
                    pass
                process.wait(timeout=1)
        for thread in (transport.reader, transport.writer):
            if thread is not None:
                thread.join(timeout=1)
        # Child exit unblocks pipe I/O before closing wrappers, avoiding the
        # TextIOWrapper lock deadlock seen when another thread is in readline.
        if transport.writer is None or not transport.writer.is_alive():
            if process.stdin:
                try:
                    process.stdin.close()
                except (OSError, ValueError):
                    pass
        if transport.reader is None or not transport.reader.is_alive():
            if process.stdout:
                process.stdout.close()

    def close(self) -> None:
        self._disconnect()

    def __enter__(self) -> "StdioClient":
        return self.start()

    def __exit__(self, *exc_info) -> None:
        self.close()

    def _wait(self, transport: _Transport, source: queue.Queue, deadline: float):
        while True:
            if transport.stopped.is_set():
                raise MCPError("MCP 连接已关闭")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MCPError(f"MCP 请求超时（{self.timeout:g} 秒）")
            try:
                return source.get(timeout=min(remaining, 0.1))
            except queue.Empty:
                pass

    def _write(self, transport: _Transport, payload: dict, deadline: float) -> None:
        done = queue.Queue(maxsize=1)
        wire = json.dumps(payload, ensure_ascii=False) + "\n"
        try:
            transport.writes.put((wire, done), timeout=max(0, deadline - time.monotonic()))
        except queue.Full as exc:
            raise MCPError("MCP 请求写入超时") from exc
        error = self._wait(transport, done, deadline)
        if error is not None:
            raise error

    def _read_reply(self, transport: _Transport, request_id: int, deadline: float) -> dict:
        while True:
            kind, value = self._wait(transport, transport.replies, deadline)
            if kind == "error":
                raise value
            line = value
            try:
                message = json.loads(line)
            except json.JSONDecodeError as exc:
                raise MCPError(f"MCP server 发来的不是 JSON：{line[:200]!r}") from exc
            if not isinstance(message, dict):
                raise MCPError(f"MCP server 发来的不是 JSON-RPC 对象：{line[:200]!r}")
            if message.get("id") != request_id:
                continue
            if "error" in message:
                error = message["error"]
                if not isinstance(error, dict):
                    raise MCPError("MCP server error 字段不是对象")
                raise MCPError(f"服务端报错 {error.get('code')}：{error.get('message')}")
            result = message.get("result", {})
            if not isinstance(result, dict):
                raise MCPError("MCP server result 字段不是对象")
            return result

    def _exchange(self, method: str, params: dict | None, *, notify: bool = False):
        deadline = time.monotonic() + self.timeout
        if not self._request_lock.acquire(timeout=self.timeout):
            raise MCPError(f"MCP 请求排队超时（{self.timeout:g} 秒）")
        transport = None
        try:
            with self._state_lock:
                transport = self._transport
            if transport is None:
                raise MCPError("客户端还没启动或已关闭")
            payload = {"jsonrpc": "2.0", "method": method, "params": params or {}}
            if not notify:
                self._next_id += 1
                payload["id"] = self._next_id
            try:
                self._write(transport, payload, deadline)
            except MCPError:
                if notify or transport.stopped.is_set():
                    raise
                # A short-lived child may emit a protocol diagnostic before
                # closing stdin. Preserve response order and parse that output.
                return self._read_reply(transport, payload["id"], min(deadline, time.monotonic() + 0.2))
            if not notify:
                return self._read_reply(transport, payload["id"], deadline)
        except MCPError:
            if transport is not None:
                self._disconnect(transport)
            raise
        finally:
            self._request_lock.release()

    def request(self, method: str, params: dict | None = None) -> dict:
        return self._exchange(method, params)

    def notify(self, method: str, params: dict | None = None) -> None:
        self._exchange(method, params, notify=True)

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


def ledger_client(command: list[str] | None = None, *, sandbox: bool = True, database_url: str | None = None) -> StdioClient:
    """起一个账单 server 客户端。"""
    return StdioClient(
        command or [sys.executable, str(LEDGER_SERVER)],
        cwd=PROJECT_ROOT,
        sandbox=sandbox,
        env_overrides={"FINANCE_LEDGER_DATABASE_URL": database_url} if database_url else None,
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
