"""MCP：手写协议实现。

- `client` —— JSON-RPC over stdio 的客户端
- `adapt`  —— 把 MCP 工具 schema 转成内部工具
"""

from fa.mcp.client import MCPError, StdioClient, ledger_client

__all__ = ["MCPError", "StdioClient", "ledger_client"]
