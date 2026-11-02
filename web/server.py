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
from contextlib import asynccontextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from web.charts import extract_series
from web.sessions import SessionManager, stream, SessionBusy, ExecutionQueueFull, ManagerClosed

HERE = Path(__file__).resolve().parent

# 需要的话设一个 token；**设了就要求带**。不设就是本机开发模式。
WEB_TOKEN = os.environ.get("FINANCE_WEB_TOKEN", "").strip()

# 限流：每个会话每分钟最多几轮。默认给得宽松，因为它挡的是「刷」，不是「用」。
TURNS_PER_MINUTE = int(os.environ.get("FINANCE_WEB_TURNS_PER_MINUTE", "6"))

manager = SessionManager()
runtime = None


@asynccontextmanager
async def lifespan(app):
    global runtime, manager
    from backend.runtime import Runtime
    runtime = Runtime()
    manager = runtime.manager
    try:
        yield
    finally:
        await run_in_threadpool(runtime.close)
        runtime = None
        manager = SessionManager()


app = FastAPI(title="finance_agent web", docs_url=None, redoc_url=None, lifespan=lifespan)


def _runtime():
    if runtime is None:
        raise HTTPException(503, detail="后端正在启动，请稍后重试")
    return runtime


def _knowledge_root():
    from fa.config import KNOWLEDGE_DIR
    return KNOWLEDGE_DIR

_rate: dict[str, deque] = defaultdict(deque)
_rate_lock = threading.Lock()


class Message(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class Verdict(BaseModel):
    allow: bool
    confirmation_id: str | None = None


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
        "persistence": runtime is not None,
        "document_worker": os.environ.get("FINANCE_DOCUMENT_WORKER", "embedded"),
    }


@app.post("/api/sessions")
def create_session(request: Request) -> dict:
    _check(request)
    try:
        live = manager.create()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"id": live.id}


@app.get("/api/knowledge")
def knowledge_files(request: Request) -> dict:
    _check(request)
    from fa.knowledge import inventory
    return inventory(_knowledge_root())


@app.post("/api/knowledge/files/{filename}", status_code=202)
async def upload_knowledge(filename: str, request: Request) -> dict:
    _check(request)
    from fa.knowledge import validate_filename
    from fa.retrieval.documents import DocumentError, MAX_FILE_BYTES
    try:
        validate_filename(filename)
    except DocumentError as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    content = bytearray()
    async for block in request.stream():
        if len(content) + len(block) > MAX_FILE_BYTES:
            raise HTTPException(413, detail="单个文件不能超过20MB")
        content.extend(block)
    try:
        return await run_in_threadpool(_runtime().jobs.submit, _knowledge_root(), filename, bytes(content))
    except FileExistsError as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    except DocumentError as exc:
        raise HTTPException(422, detail=str(exc)) from exc


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, request: Request):
    _check(request)
    result = _runtime().jobs.get(job_id)
    if result is None:
        raise HTTPException(404, detail="没有这个入库任务")
    return result


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str, request: Request):
    _check(request)
    result = _runtime().jobs.cancel(job_id)
    if result is None:
        raise HTTPException(404, detail="没有这个入库任务")
    return result


@app.post("/api/jobs/{job_id}/retry", status_code=202)
def retry_job(job_id: str, request: Request):
    _check(request)
    try:
        result = _runtime().jobs.retry(job_id)
    except ValueError as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(404, detail="没有这个入库任务")
    return result


@app.post("/api/ledger/import", status_code=201)
async def import_ledger(request: Request):
    """CSV import is atomic; repeated source records never create new charges."""
    _check(request)
    import tempfile
    from fa.categorize import categorize
    from fa.ingest import load_transactions, IngestError
    from backend.ledger import LedgerConflict, LedgerValidationError
    content = bytearray()
    async for block in request.stream():
        if len(content) + len(block) > 20 * 1024 * 1024:
            raise HTTPException(413, detail="账单文件不能超过20MB")
        content.extend(block)
    def execute():
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "import.csv"
            path.write_bytes(content)
            tagged, _ = categorize(load_transactions(path), use_llm=False)
            return _runtime().ledger.import_csv(path, transactions=tagged)
    try:
        return await run_in_threadpool(execute)
    except LedgerConflict as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    except (IngestError, LedgerValidationError, UnicodeError) as exc:
        raise HTTPException(422, detail=str(exc)) from exc


@app.get("/api/finance/summary")
def finance_summary(request: Request, account: str | None = None):
    """Asynchronous projections, not the authoritative live ledger query."""
    _check(request)
    from backend.projections import ProjectionService
    from backend.outbox import event_status
    db = _runtime().db
    return {"items": ProjectionService(db).summary(account), "status": event_status(db)}


@app.get("/api/finance/anomalies")
def finance_anomalies(request: Request, account: str | None = None):
    _check(request)
    from backend.projections import ProjectionService
    from backend.outbox import event_status
    db = _runtime().db
    return {"items": ProjectionService(db).anomalies(account), "status": event_status(db),
            "description": "同账户、同商户、同正金额且相邻间隔不超过3天的疑似重复扣款，需人工核对"}


@app.get("/api/finance/events/status")
def finance_events_status(request: Request):
    _check(request)
    from backend.outbox import event_status
    return event_status(_runtime().db)


@app.get("/api/knowledge/source")
def knowledge_source(path: str, request: Request):
    _check(request)
    from fa.retrieval.documents import SUFFIXES
    root = _knowledge_root().resolve()
    target = (root / path).resolve()
    if (not target.is_relative_to(root) or not target.is_file() or target.suffix.lower() not in SUFFIXES
            or any(p.startswith(".") for p in target.relative_to(root).parts)):
        raise HTTPException(404, detail="找不到知识库原文件")
    return FileResponse(target, filename=target.name, media_type="application/octet-stream")


@app.get("/api/sessions")
def list_sessions(request: Request) -> dict:
    """已有的会话，最近活动的在前。

    **没有这个接口，浏览器就只能自己记一个 id** —— 刷新或换标签页之后那个
    会话再也回不去，而服务端的回放明明还在。列表是那个回放的入口。
    """
    _check(request)
    return {"sessions": [live.summary() for live in manager.list()]}


@app.get("/api/sessions/{session_id}/events")
def events(session_id: str, request: Request):
    _check(request)
    live = manager.get(session_id)
    if live is None:
        raise HTTPException(status_code=404, detail="没有这个会话")

    # **先 subscribe 再返回**：订阅动作里带着回放，必须发生在响应开始流式输出
    # 之前。放在生成器里做的话，中间产生的事件会掉进缝隙里。
    raw_cursor = request.headers.get("last-event-id") or request.query_params.get("after", "0")
    try:
        after_seq = int(raw_cursor)
        if after_seq < 0:
            raise ValueError()
    except ValueError:
        raise HTTPException(422, detail="事件序号必须是非负整数")
    listener = live.subscribe(after_seq=after_seq)
    return StreamingResponse(
        stream(live, listener, after_seq=after_seq),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/sessions/{session_id}/messages")
def send(session_id: str, message: Message, request: Request) -> dict:
    _check(request)
    live = manager.get(session_id)
    if live is None:
        raise HTTPException(status_code=404, detail="没有这个会话")

    _check_rate(session_id)
    try:
        manager.submit(live, message.text)
    except SessionBusy as exc:
        raise HTTPException(409, detail="上一轮还没跑完") from exc
    except ExecutionQueueFull as exc:
        raise HTTPException(429, detail="分析任务已满，请稍后再试") from exc
    except ManagerClosed as exc:
        raise HTTPException(503, detail="服务正在关闭") from exc
    return {"accepted": True}


@app.post("/api/sessions/{session_id}/confirm")
def confirm(session_id: str, verdict: Verdict, request: Request) -> dict:
    _check(request)
    live = manager.get(session_id)
    if live is None:
        raise HTTPException(status_code=404, detail="没有这个会话")

    if runtime is not None and not verdict.confirmation_id:
        raise HTTPException(422, detail="请携带当前确认请求的编号，刷新页面后重试")

    if not live.answer(verdict.allow, confirmation_id=verdict.confirmation_id):
        # 没有人在等 —— 多半是重复点击，或者上一次已经超时了。
        # 返回 200 而不是错误：对用户来说这不是失败。
        return {"accepted": False, "reason": "没有正在等待的确认"}
    return {"accepted": True}


@app.post("/api/sessions/{session_id}/stop")
def stop_turn(session_id: str, request: Request) -> dict:
    """请求打断正在跑的那一轮。

    **不会立刻停。** agent 那边会在当前这一步跑完之后才停（模型调用拦不住），
    所以返回的是 `stopping` 而不是「已停止」—— 界面该显示「正在停」，否则用户
    会以为已经停了，而实际上还有一次模型往返在跑。
    """
    _check(request)
    live = manager.get(session_id)
    if live is None:
        raise HTTPException(status_code=404, detail="没有这个会话")

    if not live.stop():
        return {"accepted": False, "reason": "这一轮已经跑完了"}
    return {"accepted": True, "stopping": True}


@app.delete("/api/sessions/{session_id}")
def close_session(session_id: str, request: Request) -> dict:
    """关掉一个会话 —— 连同它的历史一起。

    会删除数据库中的会话及事件日志，所以前端有内容的会话会先问一句。
    接口本身不拦：反复关同一个会话，第二次给 404，对用户来说不是失败。

    正在跑的话先停掉再摘（见 `SessionManager.close` 的说明）。
    """
    _check(request)
    if manager.get(session_id) is None:
        raise HTTPException(status_code=404, detail="没有这个会话")

    return {"closed": True, "was_running": manager.close(session_id)}


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
