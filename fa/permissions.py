"""写操作的权限确认。

做成注入的回调，而不是在工具里直接 input()，有两个理由：

  1. 测试和 CI 里没有 stdin，写死 input() 就没法自动跑；
  2. 无人值守模式（第七天的评测）能直接短路掉，不用在工具里到处判断模式。

读类工具不走确认 —— 每读一个文件都拦一下，确认疲劳之后用户会无脑按 y，
权限就等于没有。
"""

from collections.abc import Callable

Confirm = Callable[[str], bool]


def auto_approve(summary: str) -> bool:
    """无人值守：一律放行。评测和 CI 用这个。"""
    return True


def make_console_confirm() -> Confirm:
    """交互式确认。默认拒绝 —— 回车不等于同意。"""

    def confirm(summary: str) -> bool:
        try:
            answer = input(f"  ? {summary} —— 允许吗？[y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        return answer in {"y", "yes"}

    return confirm
