"""Web 后端 —— FastAPI + SSE。

    python -m web.server              # 起服务，默认只监听本机
    python -m web.server --port 8000

## 为什么是 SSE 而不是 WebSocket

agent 的事件是**单向**的：服务端推、浏览器收。SSE 恰好就是为这个设计的：

- 单向 —— 不用自己维护连接状态机
- **浏览器自动重连** —— 断了自己接回来，一行重连代码都不用写
- 就是一个普通的 HTTP 响应，没有协议升级握手，代理和防火墙对它更友好

WebSocket 是双向的，这里用不上它的另一半，却要接管它全部的生命周期。

## 有写权限的 agent 不能裸放公网

这个 agent 能改分类缓存、写长期记忆、跑子 agent。挂到公网上不加防护，等于给
每个路过的人一个能改你数据的对话框。

三件事各挡不同的东西：

- **访问控制**（挡「谁能用」）—— 最重要的一条。没有它，其余两条只是延缓。
- **限流**（挡「用多快」）—— 每次提问都是真金白银的 LLM 调用，一个人就能刷完额度。
- **工作区隔离**（挡「能碰到什么」）—— 这个项目里模型**不接收路径**（Day 1 的
  决定），所以这一条的风险本来就低；但**额度不会因为隔离而省下来**。

下面三条**都实现了最小可用版本**，默认只监听 127.0.0.1。README 里写清楚了
哪些做了、哪些没做 —— 不写的话，「部署了」这三个字会让人以为该有的都有。
"""

import argparse
import os
import sys
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field

from web.charts import extract_series
from web.sessions import SessionManager, stream

HERE = Path(__file__).resolve().parent

# 需要的话设一个 token；**设了就要求带**。不设就是本机开发模式。
WEB_TOKEN = os.environ.get("FINANCE_WEB_TOKEN", "").strip()

# 限流：每个会话每分钟最多几轮。默认给得宽松，因为它挡的是「刷」，不是「用」。
TURNS_PER_MINUTE = int(os.environ.get("FINANCE_WEB_TURNS_PER_MINUTE", "6"))

app = FastAPI(title="finance_agent web", docs_url=None, redoc_url=None)
manager = SessionManager()

_rate: dict[str, deque] = defaultdict(deque)
_rate_lock = threading.Lock()


class Message(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class Verdict(BaseModel):
    allow: bool


# --- 访问控制 -----------------------------------------------------------


def _check(request: Request) -> None:
    """带 token 就校验。

    支持 `?token=` 和 `Authorization: Bearer` 两种。**用比较固定时间的比较方式**
    不是必须的（token 是共享的秘密，不是逐用户的凭据），但用 token 做访问控制
    时，把它放在 URL 里会让它出现在日志和 Referer 里 —— README 里写了这个取舍。
    """
    if not WEB_TOKEN:
        return

    supplied = request.query_params.get("token", "")
    if not supplied:
        header = request.headers.get("authorization", "")
        if header.lower().startswith("bearer "):
            supplied = header[7:].strip()

    if supplied != WEB_TOKEN:
        raise HTTPException(status_code=401, detail="token 不对")


def _check_rate(session_id: str) -> None:
    """每个会话每分钟最多 `TURNS_PER_MINUTE` 轮。

    按会话而不是按 IP：同一台机器上可能有多个用户，按 IP 会误伤；
    而按会话至少能保证「一个人不能靠刷新页面绕过」。
    """
    now = time.time()
    with _rate_lock:
        hits = _rate[session_id]
        while hits and now - hits[0] > 60:
            hits.popleft()
        if len(hits) >= TURNS_PER_MINUTE:
            raise HTTPException(
                status_code=429,
                detail=f"太快了：每分钟最多 {TURNS_PER_MINUTE} 轮，等一下再问。",
            )
        hits.append(now)


# --- 路由 ---------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (HERE / "index.html").read_text(encoding="utf-8")


@app.get("/api/health")
def health() -> dict:
    return {
        "ok": True,
        "sessions": manager.count(),
        "auth": bool(WEB_TOKEN),
        "turns_per_minute": TURNS_PER_MINUTE,
    }


@app.post("/api/sessions")
def create_session(request: Request) -> dict:
    _check(request)
    try:
        live = manager.create()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"id": live.id}


@app.get("/api/sessions/{session_id}/events")
def events(session_id: str, request: Request):
    _check(request)
    live = manager.get(session_id)
    if live is None:
        raise HTTPException(status_code=404, detail="没有这个会话")

    # **先 subscribe 再返回**：订阅动作里带着回放，必须发生在响应开始流式输出
    # 之前。放在生成器里做的话，中间产生的事件会掉进缝隙里。
    listener = live.subscribe()
    return StreamingResponse(
        stream(live, listener),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/sessions/{session_id}/messages")
def send(session_id: str, message: Message, request: Request) -> dict:
    _check(request)
    live = manager.get(session_id)
    if live is None:
        raise HTTPException(status_code=404, detail="没有这个会话")

    with live.lock:
        if live.busy:
            raise HTTPException(status_code=409, detail="上一轮还没跑完")
        live.busy = True
    _check_rate(session_id)

    # 不在这里等 —— agent 跑一轮要几十秒，等着的话这个请求会超时，
    # 而结果是通过事件流回去的。这里立刻返回，让浏览器知道「开始了」。
    threading.Thread(target=live.run, args=(message.text,), daemon=True).start()
    return {"accepted": True}


@app.post("/api/sessions/{session_id}/confirm")
def confirm(session_id: str, verdict: Verdict, request: Request) -> dict:
    _check(request)
    live = manager.get(session_id)
    if live is None:
        raise HTTPException(status_code=404, detail="没有这个会话")

    if not live.answer(verdict.allow):
        # 没有人在等 —— 多半是重复点击，或者上一次已经超时了。
        # 返回 200 而不是错误：对用户来说这不是失败。
        return {"accepted": False, "reason": "没有正在等待的确认"}
    return {"accepted": True}


@app.post("/api/sessions/{session_id}/chart")
def chart(session_id: str, payload: dict) -> dict:
    """把一段工具结果转成图表数据。

    前端不发这个请求 —— 它是给排查用的：想知道某段输出为什么没出图时，
    把那段文本贴进来就能看到解析结果（或者 None）。
    """
    return {"series": extract_series(payload.get("content", ""))}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m web.server")
    parser.add_argument("--host", default="127.0.0.1", help="默认只监听本机")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)

    import uvicorn

    if args.host != "127.0.0.1" and not WEB_TOKEN:
        print(
            "⚠️ 你在监听非本机地址，但没设 FINANCE_WEB_TOKEN。\n"
            "   这个 agent 能改数据、能烧 LLM 额度 —— 别这么放着。\n"
            "   要对外开就先设 token：见 README 的「部署」。"
        )

    print(f"起在 http://{args.host}:{args.port}  （{manager.count()} 个会话）")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
