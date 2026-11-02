"""Start the local web service once, then open it in the default browser."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener
import webbrowser


ROOT = Path(__file__).resolve().parents[1]
PORT = 8000
URL = f"http://127.0.0.1:{PORT}"
# A machine-wide HTTP proxy must not intercept this localhost probe.
HTTP = build_opener(ProxyHandler({}))


def ready() -> bool:
    try:
        with HTTP.open(f"{URL}/api/health", timeout=1) as response:
            data = json.load(response)
        return (
            data.get("ok") is True
            and "sessions" in data
            and "document_worker" in data
        )
    except (URLError, OSError, ValueError, AttributeError):
        return False


def start_service() -> bool:
    with socket.socket() as probe:
        probe.settimeout(1)
        if probe.connect_ex(("127.0.0.1", PORT)) == 0:
            print(f"端口 {PORT} 已被占用，但财务助手尚未就绪。请稍后再试，或检查占用程序。")
            return False

    logs = ROOT / ".runtime" / "web-local"
    logs.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    stdout_path = logs / f"{stamp}.stdout.log"
    stderr_path = logs / f"{stamp}.stderr.log"
    environment = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    with stdout_path.open("ab") as stdout, stderr_path.open("ab") as stderr:
        process = subprocess.Popen(
            [sys.executable, "-u", "-m", "web.server", "--host", "127.0.0.1", "--port", str(PORT)],
            cwd=ROOT,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            start_new_session=os.name != "nt",
        )
    (logs / "latest.json").write_text(
        json.dumps({
            "pid": process.pid,
            "port": PORT,
            "stdout": str(stdout_path),
            "stderr": str(stderr_path),
            "started": datetime.now().astimezone().isoformat(),
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print("正在启动财务助手，首次启动可能需要等待一会儿……", flush=True)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if ready():
            return True
        if process.poll() is not None:
            break
        time.sleep(0.5)
    print(f"服务尚未就绪，请查看启动日志：{stderr_path}")
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-browser", action="store_true", help="只启动并检查服务")
    args = parser.parse_args()
    if not ready() and not start_service():
        return 1
    print(f"财务助手已就绪：{URL}", flush=True)
    if not args.no_browser and not webbrowser.open(URL, new=2):
        print(f"请在浏览器地址栏输入：{URL}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
