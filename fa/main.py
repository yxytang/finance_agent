"""REPL 入口。

    python -m fa            正常启动
    python -m fa --yes      关掉确认（第一个会写数据的工具在第四天才有）
"""

import argparse
import sys

from fa.agent import Session
from fa.config import (
    CATEGORIES,
    MEMORY_FILE,
    SKILLS_DIR,
    SKILL_LIST_BUDGET,
    TRANSACTIONS_CSV,
    build_model,
)
from fa.events import console_listener
from fa.memory import forget
from fa.memory import load as load_memories
from fa.permissions import make_console_confirm
from fa.prompt import render_skills_section
from fa.skills import discover

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# stdin 是管道时（比如 echo "问题" | python -m fa），Python 会用系统 locale
# 解码 —— 中文 Windows 上是 cp936，UTF-8 的输入会被解成乱码甚至孤立代理字符，
# 最后在发请求时炸出一个看不懂的 UnicodeEncodeError。
# 真正的交互终端不受影响（那时 stdin 走 Windows 控制台 API），所以只在非交互时改。
if not sys.stdin.isatty() and hasattr(sys.stdin, "reconfigure"):
    sys.stdin.reconfigure(encoding="utf-8")

BANNER = f"""finance_agent —— 个人财务分析
账单：{TRANSACTIONS_CSV}
输入 /help 看命令，/exit 退出。
"""

HELP = """命令：
  /help        显示这份帮助
  /tools       列出当前可用的工具
  /skills      列出发现到的 skill，并显示清单占多少字节
  /categories  列出固定类目表
  /memory      查看记住的事；/memory rm N 删掉第 N 条
  /reset       清空对话历史（上下文快满时用）
  /exit        退出

其余输入都会交给 agent 处理。
"""


def cmd_memory(arg: str) -> None:
    """查看和删除跨会话记忆。

    删除做得和查看一样随手，这不是可有可无的功能：**记忆出错的时候用户得能
    自己修**。一条记错的偏好会一直跟着他，如果只能靠编辑文件或者写脚本才能删，
    那这条错误就是永久的。
    """
    memories = load_memories(MEMORY_FILE)

    if arg.strip():
        parts = arg.split()
        if len(parts) == 2 and parts[0] == "rm" and parts[1].isdigit():
            removed = forget(MEMORY_FILE, int(parts[1]))
            print(
                f"  已删除：{removed.text}" if removed else "  没有这个序号。"
            )
            return
        print("  用法：/memory 或 /memory rm N")
        return

    if not memories:
        print(f"  （还没有记忆。文件在 {MEMORY_FILE}，直接编辑它也能改。）")
        return

    for index, memory in enumerate(memories, start=1):
        print(f"  {index}. [{memory.kind}] {memory.text}")
    print(f"\n  共 {len(memories)} 条。删第 N 条：/memory rm N")


def cmd_tools(session: Session) -> None:
    for tool in session.tools:
        first_line = (tool.description or "").strip().splitlines()[0]
        print(f"  {tool.name:<18} {first_line[:76]}")


def cmd_skills() -> None:
    """列出 skill，并报出清单体积。

    报字节数不是装饰：这份清单在 messages[0] 里，**每一轮都在**，
    所以它是上下文预算里最该被盯住的一块。看不见就管不住。
    """
    found = discover()
    if not found:
        print(f"  （{SKILLS_DIR} 下没有 skill）")
        return

    for skill in found:
        print(f"  {skill.name:<16} {skill.description[:70]}")
        for item in skill.resources():
            print(f"  {'':<16} └ {item}")

    size = len(render_skills_section(found).encode("utf-8"))
    print(f"\n  清单共 {size} 字节 / 预算 {SKILL_LIST_BUDGET} —— 每轮都会进 prompt。")


def handle_command(line: str, session: Session) -> bool:
    """处理斜杠命令。返回 True 表示该退出了。

    这些命令**一个 token 都不进提示词** —— 它们在 Python 里就被拦下了，
    模型根本不知道它们存在。所以改 /help 的文案不会影响模型行为，
    这点和「写进 system prompt 的规则」是完全不同的两回事。
    """
    command = line[1:].split(maxsplit=1)[0].lower() if len(line) > 1 else ""

    if command in {"exit", "quit", "q"}:
        return True
    if command == "help":
        print(HELP)
    elif command == "tools":
        cmd_tools(session)
    elif command == "skills":
        cmd_skills()
    elif command == "categories":
        print("  " + "、".join(CATEGORIES))
    elif command == "memory":
        _, _, rest = line[1:].partition(" ")
        cmd_memory(rest)
    elif command == "reset":
        session.reset()
        print("  对话历史已清空。")
    else:
        print(f"  没有 {command!r} 这个命令。输入 /help 看有哪些。")
    return False


def main() -> int:
    parser = argparse.ArgumentParser(prog="fa", description="个人财务分析 agent")
    parser.add_argument("-y", "--yes", action="store_true", help="写操作不再逐次确认")
    args = parser.parse_args()

    # 账单没生成时，先说清楚下一步做什么 —— 否则用户问第一句会得到
    # 「查不到数据」，然后开始怀疑是 agent 坏了。
    if not TRANSACTIONS_CSV.is_file():
        print(
            f"没有找到账单：{TRANSACTIONS_CSV}\n"
            f"先跑一次：python -m data.generate"
        )
        return 1

    # 提前构造一次模型，配置有问题（比如没填 key）在这里就暴露，
    # 而不是等用户问完第一个问题。
    try:
        build_model()
    except Exception as exc:  # noqa: BLE001
        print(f"模型配置有问题：{exc}")
        return 1

    session = Session(
        confirm=None if args.yes else make_console_confirm(),
        on_event=console_listener(),
    )
    print(BANNER)

    while True:
        try:
            line = input("fa> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见。")
            return 0

        if not line:
            continue

        try:
            if line.startswith("/"):
                if handle_command(line, session):
                    print("再见。")
                    return 0
            else:
                # 答案由 console_listener 打印，这里不重复打 —— CLI 和第九天的
                # 浏览器走同一条路：都是事件流的消费者。
                session.send(line)
        except KeyboardInterrupt:
            print("\n（已中断，可以继续输入）")


if __name__ == "__main__":
    raise SystemExit(main())
