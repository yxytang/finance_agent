"""严格检索消融实验；默认离线，不调用模型。见 eval/README.md。"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import platform
import time

from eval.benchmark_cases import all_cases, dataset_fingerprint, validate
from fa.config import KNOWLEDGE_DIR, rag_settings
from fa.retrieval.chunk import load_corpus
from fa.retrieval.hybrid import Hit, PER_CHANNEL, RRF_K
from fa.retrieval.index import SearchIndex, corpus_fingerprint

RUN_DIR = Path(__file__).parent / "runs"
METRICS = ("hit@1", "hit@3", "hit@5", "mrr@10", "candidate_recall@20", "evidence_recall@5")


def matches(chunk, evidence) -> bool:
    """来源、小节和原文锚点共同匹配；同名标题不能串文档。"""
    return (chunk.source == evidence.source and
            chunk.breadcrumb.split(" > ")[-1].removesuffix("（续）") == evidence.heading and
            evidence.quote in chunk.text)


def retrieval_metrics(case, candidates, ranked) -> dict:
    if not case.evidence or case.group == "tool_routing":
        return {name: None for name in METRICS}

    def covered(hits):
        return sum(any(matches(h.chunk, e) for h in hits) for e in case.evidence) / len(case.evidence)

    ranks = [i for i, h in enumerate(ranked[:10], 1)
             if any(matches(h.chunk, e) for e in case.evidence)]
    return {
        "hit@1": float(covered(ranked[:1]) > 0),
        "hit@3": float(covered(ranked[:3]) > 0),
        "hit@5": float(covered(ranked[:5]) > 0),
        "mrr@10": 1 / ranks[0] if ranks else 0.0,
        "candidate_recall@20": covered(candidates[:20]),
        "evidence_recall@5": covered(ranked[:5]),
    }


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lo = math.floor(position)
    hi = math.ceil(position)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (position - lo)


def summarize(rows):
    """失败保留在计划分母；同时报成功样本均值与失败按0计的操作成绩。"""
    groups = sorted({(r["mode"], r["group"]) for r in rows})
    summaries = []
    for mode, group in groups:
        items = [r for r in rows if r["mode"] == mode and r["group"] == group]
        success = [r for r in items if r["status"] == "ok"]
        scored = [r for r in success if r["metrics"]["hit@1"] is not None]
        eligible = [r for r in items if r["metric_eligible"]]
        summary = {"mode": mode, "group": group, "planned": len(items),
                   "succeeded": len(success), "failed": len(items) - len(success),
                   "metric_planned": len(eligible), "metric_succeeded": len(scored),
                   "latency_ms_p50": percentile([r["latency_ms"] for r in items], .5),
                   "latency_ms_p95": percentile([r["latency_ms"] for r in items], .95),
                   "successful_latency_ms_p50": percentile([r["latency_ms"] for r in success], .5),
                   "successful_latency_ms_p95": percentile([r["latency_ms"] for r in success], .95)}
        for metric in METRICS:
            values = [r["metrics"][metric] for r in scored]
            summary[metric] = sum(values) / len(values) if values else None
            summary[metric + "_failure_as_zero"] = sum(values) / len(eligible) if eligible else None
        summaries.append(summary)
    return summaries


def check_vectors(vectors, expected, dimension=None):
    if len(vectors) != expected:
        raise ValueError("embedding 返回条数错误")
    for v in vectors:
        if not v or (dimension is not None and len(v) != dimension):
            raise ValueError("embedding 维度错误")
        dimension = len(v)
        if not all(isinstance(x, (int, float)) and math.isfinite(x) for x in v) or not any(v):
            raise ValueError("embedding 包含非有限数或零向量")
    return dimension


class Engine:
    """复用生产分词/分块/客户端，严格检查服务输出，实验库与生产库隔离。"""

    def __init__(self, chunks, fingerprint, *, embedder=None, reranker=None,
                 store=None, embedding_unit="child", lexical="both", rrf_k=RRF_K, per_channel=PER_CHANNEL):
        self.index = SearchIndex().build(chunks, fingerprint)
        self.embedder, self.reranker, self.store = embedder, reranker, store
        self.stats = Counter()
        self.parents = {}
        self.dimension = None
        self.lexical, self.rrf_k, self.per_channel = lexical, rrf_k, per_channel
        if embedder is not None:
            from fa.retrieval.children import children_of
            from fa.retrieval.dense import MemoryStore, parent_key
            self.store = store if store is not None else MemoryStore()
            ids, texts = [], []
            for doc_id, chunk in enumerate(chunks):
                units = children_of(chunk.text) if embedding_unit == "child" else [chunk.text]
                for i, text in enumerate(units):
                    child_id = f"{parent_key(chunk)}#{i}"
                    ids.append(child_id)
                    texts.append(text)
                    self.parents[child_id] = doc_id
            vectors = self.embed(texts)
            self.store.reset(fingerprint + ":" + embedding_unit + ":" + embedder.model_id, ids, vectors)

    def embed(self, texts):
        self.stats["embedding_invocations"] += 1
        self.stats["embedding_inputs"] += len(texts)
        vectors = self.embedder.embed(texts)
        self.dimension = check_vectors(vectors, len(texts), self.dimension)
        return vectors

    def dense_rank(self, query, k):
        if self.embedder is None:
            raise RuntimeError("实验要求向量路，但未初始化")
        vector = self.embed([query])[0]
        # 全量取子块再合并父块：当前小语料可精确比较，避免重复子块挤掉父块。
        # 这与生产 DenseIndex 的 k 个子块截断不同，报告明确记录此实验设置。
        raw = self.store.query(vector, len(self.parents))
        if not raw and self.parents:
            raise ValueError("向量库非空但查询没有结果")
        output = []
        for child_id, distance in raw:
            if child_id not in self.parents or not math.isfinite(distance):
                raise ValueError("向量库返回未知ID或非有限距离")
            parent = self.parents[child_id]
            if parent not in output:
                output.append(parent)
        return output[:k]

    def recall(self, mode, query, limit=20):
        rankings = []
        if mode != "dense":
            rankings += [(label, [i for i, _ in self.index.rank(field, query)[:self.per_channel]])
                         for field, label in (("title", "标题"), ("body", "正文"))
                         if self.lexical in ("both", field)]
        if mode != "bm25":
            rankings.append(("语义", self.dense_rank(query, self.per_channel)))
        scores, channels = {}, {}
        for label, ids in rankings:
            for rank, doc_id in enumerate(ids, 1):
                scores[doc_id] = scores.get(doc_id, 0) + 1 / (self.rrf_k + rank)
                channels.setdefault(doc_id, []).append(label)
        hits = [Hit(self.index.chunks[i], score, tuple(channels[i])) for i, score in scores.items()]
        hits.sort(key=lambda h: (-h.score, -len(h.channels), h.chunk.source))
        return hits[:limit]

    def retrieve(self, mode, question, queries=None):
        queries = queries or [question]
        pools = [self.recall(mode, q) for q in queries]
        if len(pools) == 1:
            candidates = pools[0]
        else:
            # 多查询之间再做一次RRF，保留原始查询，与单查询对照。
            scores, objects = {}, {}
            for pool in pools:
                for rank, hit in enumerate(pool, 1):
                    key = (hit.chunk.source, hit.chunk.breadcrumb, hit.chunk.text)
                    scores[key] = scores.get(key, 0) + 1 / (self.rrf_k + rank)
                    objects[key] = hit
            keys = sorted(scores, key=lambda key: (-scores[key], key))
            candidates = [Hit(objects[k].chunk, scores[k], objects[k].channels) for k in keys[:20]]
        ranked = candidates
        if mode == "rerank" and candidates:
            if self.reranker is None:
                raise RuntimeError("实验要求重排，但未初始化")
            self.stats["rerank_invocations"] += 1
            self.stats["rerank_documents"] += len(candidates)
            values = self.reranker.scores(question, [str(h.chunk) for h in candidates])
            if len(values) != len(candidates) or any(v is None or not math.isfinite(v) for v in values):
                raise ValueError("重排缺失分数、条数错误或包含非有限数；本题实验失败")
            ranked = [candidates[i] for i in sorted(range(len(values)), key=lambda i: (-values[i], i))]
        return candidates, ranked


def rewrite_queries(question, strategy, model):
    if strategy == "none":
        return [question], {}
    if strategy == "multi":
        prompt = ('将用户问题改写为3种中文检索问法，保持意图，不回答问题。只返回JSON字符串数组。'
                  '\n用户问题：' + question)
    else:
        prompt = ('为用户问题写一段不超过300字的假设性检索文档。它仅用于检索，不是真实答案。'
                  '\n用户问题：' + question)
    answer = model.invoke(prompt)
    if not isinstance(answer.content, str):
        raise ValueError("改写模型没有返回文本")
    if strategy == "multi":
        values = json.loads(answer.content)
        if not isinstance(values, list) or len(values) != 3 or any(
            not isinstance(v, str) or not v.strip() or len(v) > 400 for v in values
        ):
            raise ValueError("多查询改写必须返回3个非空问句")
    else:
        if not answer.content.strip() or len(answer.content) > 2000:
            raise ValueError("HyDE文档为空或过长")
        values = [answer.content]
    return list(dict.fromkeys([question, *values])), getattr(answer, "usage_metadata", None) or {}


def report_markdown(report):
    meta = report["config"]
    lines = ["# 检索评测结果", "", "新题均为助手起草、待人工复核；指标衡量固定语料内的小节及证据锚点命中。",
             "", f"划分：{meta['split']}；重复次数：{meta['repeat']}；候选20，最终5；MRR截断10，未命中记0。",
             f"语料指纹：`{meta['corpus_fingerprint']}`", f"题库指纹：`{meta['dataset_fingerprint']}`", "",
             "Hit表示至少命中一个证据，多证据完整性看 evidence_recall@5。无答案题不计检索命中率。",
             "服务失败保留在计划分母，以下指标采用失败按0计；成功样本均值另见JSON。", "",
             "|模式|题型|成功/计划|Hit@1|Hit@3|MRR@10|候选证据Recall@20|最终证据Recall@5|p50/p95 ms|",
             "|---|---|---|---|---|---|---|---|---|"]
    def fmt(v):
        return "—" if v is None else f"{v:.3f}"
    for s in report["summary"]:
        metrics = [fmt(s[m + "_failure_as_zero"]) for m in
                   ("hit@1", "hit@3", "mrr@10", "candidate_recall@20", "evidence_recall@5")]
        timing = "/".join(fmt(s[k]) for k in ("latency_ms_p50", "latency_ms_p95"))
        lines.append(f"|{s['mode']}|{s['group']}|{s['succeeded']}/{s['planned']}|" + "|".join(metrics) + f"|{timing}|")
    lines += ["", f"初始化耗时：{report['initialization_ms']:.1f} ms，单独计量。",
              f"调用量（客户端方法调用及输入量，不等同HTTP重试/账单费用）：`{json.dumps(report['calls'])}`", "",
              "## 需要检查的逐题结果", ""]
    misses = [r for r in report["rows"] if r["status"] != "ok" or
              (r["metric_eligible"] and r["metrics"]["evidence_recall@5"] < 1)]
    for r in misses:
        top = ", ".join(h["source"] + ":" + h["breadcrumb"].split(" > ")[-1] for h in r.get("final_hits", [])[:3])
        lines.append(f"- {r['mode']} / {r['id']} / 第{r['trial']}次：{r['question']} → {r.get('error_type', top or '空结果')}")
    return "\n".join(lines) + "\n"


def write_review(cases, path):
    lines = ["# 100题人工复核清单", "", "所有题目当前均为助手草案。需人工核对题意、参考要点、可替代证据、无答案边界和工具槽位。",
             "政策题只按仓库快照评测；不能用此清单证明现行法规正确。", ""]
    for c in cases:
        lines += [f"## {c.id} · {c.split} · {c.group}", "", f"- [ ] 人工复核（当前：{c.annotation_status}）",
                  f"- 题族：{c.family}", f"- 问题：{c.question}", f"- 参考要点：{c.reference}",
                  f"- 必需工具槽位：{json.dumps(c.tool_slots, ensure_ascii=False)}", ""]
        for e in c.evidence:
            lines += [f"证据：{e.source} → {e.heading}", "", f"> {e.quote}", ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--modes", nargs="+", choices=("bm25", "dense", "hybrid", "rerank"), default=["bm25"])
    parser.add_argument("--split", choices=("dev", "test", "all"), default="dev")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--embedding-unit", choices=("parent", "child"), default="child")
    parser.add_argument("--store", choices=("memory", "chroma"), default="memory")
    parser.add_argument("--rewrite", choices=("none", "multi", "hyde"), default="none")
    parser.add_argument("--lexical", choices=("title", "body", "both"), default="both")
    parser.add_argument("--rrf-k", type=int, default=RRF_K)
    parser.add_argument("--per-channel", type=int, default=PER_CHANNEL)
    parser.add_argument("--output", type=Path, default=RUN_DIR / "retrieval.json")
    parser.add_argument("--review", type=Path, help="仅导出全部题目及证据，不跑实验")
    args = parser.parse_args(argv)
    if args.repeat < 1 or (args.limit is not None and args.limit < 1):
        parser.error("repeat和limit必须大于0")
    if args.rrf_k < 0 or args.per_channel < 1:
        parser.error("rrf-k必须非负，per-channel必须为正")
    chunks = load_corpus(KNOWLEDGE_DIR)
    cases = all_cases()
    validate(cases, chunks)
    if args.review:
        write_review(cases, args.review)
        print(f"题库检查通过：{len(cases)}题；复核清单：{args.review}")
        return 0
    todo = [c for c in cases if c.group != "tool_routing" and (args.split == "all" or c.split == args.split)]
    if args.limit:
        todo = todo[:args.limit]
    modes = list(dict.fromkeys(args.modes))
    settings = rag_settings() if any(m != "bm25" for m in modes) else None
    if any(m != "bm25" for m in modes) and settings is None:
        parser.error("向量/融合/重排实验需要RAG_API_KEY，未配置时不降级；默认bm25可离线运行")
    started = time.perf_counter()
    embedder = reranker = store = model = None
    if settings:
        from fa.retrieval.dense import ChromaStore, HttpEmbedder
        embedder = HttpEmbedder(settings)
        if args.store == "chroma":
            store = ChromaStore(RUN_DIR / "chroma")
        if "rerank" in modes:
            from fa.retrieval.rerank import HttpReranker
            if not settings.rerank_enabled:
                parser.error("重排实验需要配置重排模型与端点")
            reranker = HttpReranker(settings)
    if args.rewrite != "none":
        from fa.config import build_model
        model = build_model()
    fp = corpus_fingerprint(KNOWLEDGE_DIR)
    engine = Engine(chunks, fp, embedder=embedder, reranker=reranker, store=store,
                    embedding_unit=args.embedding_unit, lexical=args.lexical,
                    rrf_k=args.rrf_k, per_channel=args.per_channel)
    init_ms = (time.perf_counter() - started) * 1000
    config = {"modes": modes, "split": args.split, "repeat": args.repeat,
              "limit": args.limit, "candidate_k": 20, "final_k": 5, "rrf_k": args.rrf_k,
              "per_channel": args.per_channel, "lexical": args.lexical, "rewrite": args.rewrite,
              "store": args.store if settings else None, "embedding_unit": args.embedding_unit if settings else None,
              "dense_child_fetch": "all_then_deduplicate_parents", "cache": "disabled; rebuild_index_each_run",
              "embed_model": settings.embed_model if settings else None,
              "rerank_model": settings.rerank_model if reranker else None,
              "rewrite_model": getattr(model, "model_name", None),
              "corpus_fingerprint": fp, "dataset_fingerprint": dataset_fingerprint(cases),
              "python": platform.python_version(), "documents": len({c.source for c in chunks}),
              "parent_chunks": len(chunks), "vector_units": len(engine.parents),
              "annotation_status": "assistant_draft_pending_human_review",
              "code_fingerprint": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    rows = []
    rewrite_calls = 0
    planned = len(todo) * args.repeat * len(modes)
    args.output.parent.mkdir(parents=True, exist_ok=True)

    def checkpoint(finished=False):
        report = {"schema_version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
                  "status": "completed" if finished else "running", "planned": planned,
                  "completed": len(rows), "config": config, "initialization_ms": init_ms,
                  "calls": dict(engine.stats) | {"rewrite_invocations": rewrite_calls},
                  "summary": summarize(rows), "rows": rows}
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        if finished:
            args.output.with_suffix(".md").write_text(report_markdown(report), encoding="utf-8")

    checkpoint()
    for trial in range(1, args.repeat + 1):
        for case in todo:
            rewrite_start = time.perf_counter()
            query_error = None
            usage = {}
            try:
                if model:
                    rewrite_calls += 1
                queries, usage = rewrite_queries(case.question, args.rewrite, model)
            except Exception as exc:
                queries, query_error = [], type(exc).__name__
            rewrite_ms = (time.perf_counter() - rewrite_start) * 1000
            for mode in modes:
                start = time.perf_counter()
                row = {**asdict(case), "mode": mode, "trial": trial, "queries": queries,
                       "rewrite_usage": usage, "rewrite_ms": rewrite_ms,
                       "metric_eligible": bool(case.evidence), "status": "ok"}
                try:
                    if query_error:
                        raise RuntimeError("query rewrite failed")
                    candidates, ranked = engine.retrieve(mode, case.question, queries)
                    row.update(metrics=retrieval_metrics(case, candidates, ranked),
                               candidate_hits=[asdict(h.chunk) | {"score": h.score, "channels": h.channels} for h in candidates],
                               final_hits=[asdict(h.chunk) | {"score": h.score, "channels": h.channels} for h in ranked[:5]])
                except Exception as exc:
                    # 不保存HTTP错误正文或客户端repr，以免错误包含凭证。
                    row.update(status="error", error_type=query_error or type(exc).__name__,
                               metrics={m: None for m in METRICS}, candidate_hits=[], final_hits=[])
                row["latency_ms"] = (time.perf_counter() - start) * 1000 + rewrite_ms
                rows.append(row)
                if row["status"] != "ok":
                    print(f"失败：{mode} {case.id} {row['error_type']}", flush=True)
                if len(rows) % 10 == 0:
                    checkpoint()
                    print(f"进度：{len(rows)}/{planned}，失败{sum(r['status'] != 'ok' for r in rows)}", flush=True)
    checkpoint(finished=True)
    print(f"完成{len(rows)}次检索；失败{sum(r['status'] != 'ok' for r in rows)}次；结果：{args.output}")
    return int(any(r["status"] != "ok" for r in rows))


if __name__ == "__main__":
    raise SystemExit(main())
