"""只读直接工具配置的 Agent 路由与回答采集；默认 --dry，无API调用。"""

import argparse
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time

from eval.benchmark import Engine, RUN_DIR, percentile
from eval.benchmark_cases import all_cases, dataset_fingerprint, validate
from fa.config import KNOWLEDGE_DIR, TRANSACTIONS_CSV, rag_settings
from fa.retrieval.chunk import load_corpus
from fa.retrieval.index import corpus_fingerprint

READ_ONLY = {"describe_data", "query_transactions", "find_anomalies", "list_subscriptions",
             "project_spending", "upcoming_charges", "search_knowledge"}


def route_metrics(case, events):
    called = {e["name"] for e in events if e["type"] == "tool_call"}
    succeeded = {e["name"] for e in events if e["type"] == "tool_result" and e.get("ok")}
    required = sum(bool(called.intersection(slot)) for slot in case.tool_slots)
    completed = sum(bool(succeeded.intersection(slot)) for slot in case.tool_slots)
    forbidden = bool(called.intersection(case.forbidden_tools))
    # unanswerable 的允许行为包括查后拒答、直接说明资料不足，交给人工判答案。
    eligible = case.group != "unanswerable"
    return {"required_tool_recall": required / len(case.tool_slots) if eligible else None,
            "successful_tool_recall": completed / len(case.tool_slots) if eligible else None,
            "route_correct": float(required == len(case.tool_slots) and not forbidden) if eligible else None,
            "forbidden_tool_called": forbidden}


def collect_one(case, engine, mode, *, session_factory=None):
    from fa.agent import Session
    from fa.prompt import build_system_prompt
    from fa.tools import build_tools
    from fa.retrieval import render
    from langchain_core.tools import tool

    events, searches = [], []
    base_tools = build_tools(use_mcp=False)
    original = next(t for t in base_tools if t.name == "search_knowledge")

    @tool("search_knowledge", description=original.description)
    def knowledge(query: str) -> str:
        candidates, ranked = engine.retrieve(mode, query)
        final = ranked[:5]
        searches.append({"query": query, "candidates": [asdict(h.chunk) for h in candidates],
                         "chunks": [asdict(h.chunk) for h in final]})
        return render(final) + "\n（知识库快照；具体政策以最新官方信息为准。）"

    tools = [knowledge if t.name == "search_knowledge" else t
             for t in base_tools if t.name in READ_ONLY]
    prompt = build_system_prompt([], [])
    prompt += "\n本次仅使用直接只读工具；不加载技能、个人记忆、MCP或子agent。"
    started = time.perf_counter()
    session = (session_factory or Session)(tools=tools, system_prompt=prompt, on_event=events.append)
    error_type = None
    try:
        answer = session.send(case.question)
    except Exception as exc:
        answer, error_type = "", type(exc).__name__
    errors = [e for e in events if e["type"] in ("error", "stopped") or
              (e["type"] == "tool_result" and not e.get("ok"))]
    status = "error" if error_type or errors or not answer.strip() else "ok"
    chunks = []
    for call in searches:
        for chunk in call["chunks"]:
            if chunk not in chunks:
                chunks.append(chunk)
    # 取实际喂给模型的最终块，保留全部检索轮次；不是重排前候选。
    contexts = [c["breadcrumb"] + "\n" + c["text"] for c in chunks]
    usage = Counter()
    for message in getattr(session, "messages", []):
        for key, value in (getattr(message, "usage_metadata", None) or {}).items():
            if isinstance(value, (int, float)):
                usage[key] += value
    return {**asdict(case), "status": status, "error_type": error_type,
            "response": answer, "searches": searches, "retrieved_contexts": contexts,
            "search_queries": [s["query"] for s in searches],
            "tool_calls": [e for e in events if e["type"] == "tool_call"],
            "tool_results": [e for e in events if e["type"] == "tool_result"],
            "error_count": len(errors), "route": route_metrics(case, events),
            "latency_ms": (time.perf_counter() - started) * 1000, "token_usage": dict(usage)}


def summarize_agent(rows):
    summaries = []
    for group in sorted({r["group"] for r in rows}):
        items = [r for r in rows if r["group"] == group]
        scored = [r for r in items if r["route"]["route_correct"] is not None]
        s = {"group": group, "planned": len(items), "succeeded": sum(r["status"] == "ok" for r in items),
             "routing_denominator": len(scored), "manual_answer_review_pending": len(items),
             "latency_ms_p50": percentile([r["latency_ms"] for r in items], .5),
             "latency_ms_p95": percentile([r["latency_ms"] for r in items], .95)}
        for metric in ("required_tool_recall", "successful_tool_recall", "route_correct"):
            # 记录了工具调用但整轮失败也不能算操作成功。
            values = [r["route"][metric] if r["status"] == "ok" else 0 for r in scored]
            s[metric] = sum(values) / len(values) if values else None
        summaries.append(s)
    return summaries


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="明确执行真实模型调用；不传则只检查清单")
    parser.add_argument("--split", choices=("dev", "test", "all"), default="dev")
    parser.add_argument("--group", choices=("literal", "paraphrased", "multi_evidence", "unanswerable", "tool_routing"))
    parser.add_argument("--limit", type=int)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--mode", choices=("bm25", "dense", "hybrid", "rerank"), default="bm25")
    parser.add_argument("--output", type=Path, default=RUN_DIR / "agent.json")
    args = parser.parse_args(argv)
    if args.repeat < 1 or (args.limit is not None and args.limit < 1):
        parser.error("repeat和limit必须大于0")
    cases = all_cases()
    chunks = load_corpus(KNOWLEDGE_DIR)
    validate(cases, chunks)
    todo = [c for c in cases if (args.split == "all" or c.split == args.split) and
            (args.group is None or c.group == args.group)]
    if args.limit:
        todo = todo[:args.limit]
    print(f"计划{len(todo)}题 × {args.repeat}次；只读直接工具配置；检索模式{args.mode}")
    if not args.run:
        print("清单检查通过。未调用API；传 --run 才采集。")
        return 0
    embedder = reranker = None
    if args.mode != "bm25":
        settings = rag_settings()
        if settings is None:
            parser.error("该模式需要RAG_API_KEY；不降级")
        from fa.retrieval.dense import HttpEmbedder
        embedder = HttpEmbedder(settings)
        if args.mode == "rerank":
            from fa.retrieval.rerank import HttpReranker
            if not settings.rerank_enabled:
                parser.error("重排未配置")
            reranker = HttpReranker(settings)
    from fa.config import build_model
    model_name = build_model().model_name  # 缺key在采集前失败。
    fp = corpus_fingerprint(KNOWLEDGE_DIR)
    engine = Engine(chunks, fp, embedder=embedder, reranker=reranker)
    rows = []
    for trial in range(1, args.repeat + 1):
        for case in todo:
            row = collect_one(case, engine, args.mode)
            row["trial"] = trial
            rows.append(row)
            print(f"{trial}/{args.repeat} {case.id}: {row['status']}，检索{len(row['searches'])}次", flush=True)
            # 逐题落盘，长批次中断仍保留已完成结果及计划分母。
            args.output.parent.mkdir(parents=True, exist_ok=True)
            report = {"created_at": datetime.now(timezone.utc).isoformat(), "schema_version": 1,
                      "config": {"profile": "direct_readonly_no_memory_no_mcp_no_subagent",
                                 "mode": args.mode, "split": args.split, "repeat": args.repeat,
                                 "planned": len(todo) * args.repeat, "completed": len(rows),
                                 "model": model_name, "tools": sorted(READ_ONLY),
                                 "corpus_fingerprint": fp, "dataset_fingerprint": dataset_fingerprint(cases),
                                 "transactions_fingerprint": hashlib.sha256(TRANSACTIONS_CSV.read_bytes()).hexdigest()},
                      "summary": summarize_agent(rows), "rows": rows}
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    # RAGAS仅评正向知识题；账单数值、无答案题另做人工检查，不混入知识检索分数。
    ragas_rows = [{**r, "source": ";".join(dict.fromkeys(e["source"] for e in r["evidence"]))}
                  for r in rows if r["group"] in ("literal", "paraphrased", "multi_evidence") and r["status"] == "ok"]
    args.output.with_suffix(".ragas.json").write_text(json.dumps(ragas_rows, ensure_ascii=False, indent=2), encoding="utf-8")
    review = [{"id": r["id"], "trial": r["trial"], "group": r["group"], "question": r["question"],
               "reference": r["reference"], "response": r["response"], "contexts": r["retrieved_contexts"],
               "reviewer": None, "key_point_coverage": None, "evidence_supported": None,
               "correct_abstention": None, "notes": "待人工复核；未打分不能当0分或成功"} for r in rows]
    args.output.with_suffix(".review.json").write_text(json.dumps(review, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"采集与人工复核模板已写到：{args.output}；RAGAS导出{len(ragas_rows)}/{len(rows)}题，失败未计入judge输入")
    return int(any(r["status"] != "ok" for r in rows))


if __name__ == "__main__":
    raise SystemExit(main())
