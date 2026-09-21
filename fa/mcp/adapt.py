"""把 MCP 工具 schema 转成内部工具 —— 一次适配，agent 主体不用改。

## 这就是协议的价值

MCP 给的是 JSON Schema，内部工具是 LangChain 的 `BaseTool`。这层转换写一次，
之后**任何** MCP server 的工具都能挂上来 —— agent 的循环、工具列表、
prompt 组装全都不用动。

价值不在「少写几行代码」，而在**加一个新数据源不需要改主体**。
plugin 模式要把对方的接口编进自己的代码里，所以每加一个就要改一次；
MCP 把这件事变成了运行时的事。

## 类型转换要 fail-loud

JSON Schema 的类型比我们需要的多。遇到不认识的**就拒绝挂载这个工具**，
而不是猜一个 —— 猜错的表现是参数被静默转成错误的类型，然后工具返回一个
看起来正常的错结果。那是最难查的一类问题。

同理：**发现不了工具时要出声音**。悄悄少几个工具的话，表现是「模型忽然不会
用某个能力了」，而没有任何东西提示你去查 MCP。
"""

import sys
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import Field, create_model

from fa.mcp.client import MCPError, StdioClient

# JSON Schema 类型 → Python 类型。只列我们真的会遇到的，其余一律拒绝。
_TYPES: dict[str, type] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}


class UnsupportedSchema(ValueError):
    """这个 schema 我们转不了。拒绝挂载，不猜。"""


def _python_type(prop: dict, where: str) -> type:
    kind = prop.get("type")
    if kind is None and "anyOf" in prop:
        # 可选参数常被写成 anyOf [T, null]，取非 null 的那一支
        branches = [b for b in prop["anyOf"] if b.get("type") != "null"]
        if len(branches) == 1:
            return _python_type(branches[0], where)
    if kind in _TYPES:
        return _TYPES[kind]
    raise UnsupportedSchema(f"{where} 的类型 {kind!r} 我们转不了")


def build_args_model(name: str, schema: dict) -> type | None:
    """按 JSON Schema 造一个 pydantic 模型，给 LangChain 当参数校验用。"""
    properties = schema.get("properties") or {}
    if not properties:
        return None

    required = set(schema.get("required") or [])
    fields: dict[str, Any] = {}
    for prop_name, prop in properties.items():
        annotation = _python_type(prop, f"{name}.{prop_name}")
        description = prop.get("description") or None
        if prop_name in required:
            fields[prop_name] = (annotation, Field(..., description=description))
        else:
            # 非必填：给个默认值。默认值取 schema 里写的，没有就用 None ——
            # **不能给一个「看起来像」的默认值**（比如空字符串或 0），
            # 那会让「用户没传」和「用户传了空值」变成同一件事。
            fields[prop_name] = (
                annotation | None,
                Field(prop.get("default"), description=description),
            )

    return create_model(f"{name}_args", **fields)


def to_internal_tool(client: StdioClient, spec: dict, *, prefix: str = "") -> BaseTool:
    """把一个 MCP 工具声明转成内部工具。

    `prefix` 用来避免重名 —— MCP server 不知道我们内部有哪些工具，
    两个名字撞上时 LangChain 会静默地用一个盖掉另一个。
    """
    name = spec["name"]
    full_name = f"{prefix}{name}"

    def run(**kwargs) -> str:
        return client.call_tool(name, kwargs)

    return StructuredTool.from_function(
        func=run,
        name=full_name,
        description=spec.get("description") or "",
        args_schema=build_args_model(full_name, spec.get("inputSchema") or {}),
    )


def mount_tools(
    client: StdioClient, *, prefix: str = "ledger__", on_error: str | None = None
) -> list[BaseTool]:
    """连上 server、发现工具、全部挂载。

    任何一步出问题都返回空列表（agent 还能用内置工具干活），但**一定要出声**：
    静默地少几个工具，表现是「模型忽然不会用某个能力了」，而没有任何东西提示
    你该去查 MCP。
    """
    try:
        client.start()
        client.initialize()
        specs = client.list_tools()
    except MCPError as exc:
        print(f"警告：MCP 工具没挂上 —— {exc}", file=sys.stderr)
        return []

    tools: list[BaseTool] = []
    for spec in specs:
        try:
            tools.append(to_internal_tool(client, spec, prefix=prefix))
        except (UnsupportedSchema, KeyError, TypeError) as exc:
            print(
                f"警告：跳过 MCP 工具 {spec.get('name')!r} —— {exc}",
                file=sys.stderr,
            )
    return tools
