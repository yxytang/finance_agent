"""用 Node 执行页面事件回归测试，不调用模型或外部服务。"""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 Node 运行前端事件测试")
def test_frontend_events():
    result = subprocess.run(
        ["node", "--test", str(Path(__file__).with_name("frontend_harness.cjs"))],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stdout + result.stderr
