"""评测口径的回归测试：分母、证据、划分隔离、严格失败及实际模型上下文。"""

from collections import Counter
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from eval.benchmark import Engine, check_vectors, main, matches, retrieval_metrics, rewrite_queries, summarize
from eval.benchmark_agent import collect_one, route_metrics, summarize_agent
from eval.benchmark_cases import Case, Evidence, all_cases, dataset_fingerprint, validate
from eval.ragas_run import score_table, write_findings
from eval.manual_review import summarize_review
from fa.config import KNOWLEDGE_DIR
from fa.retrieval.chunk import Chunk, load_corpus
from fa.retrieval.hybrid import Hit, search


def hit(source="a.md", heading="内容", text="证据"):
    return Hit(Chunk(source, f"{source} > {heading}", text), 1, ("标题",))


def case(evidence=None, **kwargs):
    return Case("id", "family", "literal", "dev", "问题", "答案",
                evidence or (Evidence("a.md", "内容", "证据"),), **kwargs)


def test_corpus_anchored_100_cases_and_60_40_split():
    cases = all_cases()
    validate(cases, load_corpus(KNOWLEDGE_DIR))
    assert Counter(c.group for c in cases) == {
        "literal": 30, "paraphrased": 30, "multi_evidence": 15,
        "unanswerable": 15, "tool_routing": 10,
    }
    assert Counter(c.split for c in cases) == {"dev": 60, "test": 40}
    assert all(c.annotation_status == "assistant_draft_pending_human_review" for c in cases)
    assert len({e.source for c in cases for e in c.evidence}) == 12


def test_broken_quote_and_split_leakage_are_errors():
    cases = all_cases()
    chunks = load_corpus(KNOWLEDGE_DIR)
    wrong = replace(cases[0], evidence=(replace(cases[0].evidence[0], quote="不存在的证据"),))
    with pytest.raises(ValueError, match="证据找不到"):
        validate([wrong], chunks)
    with pytest.raises(ValueError, match="题族跨划分"):
        validate([cases[0], replace(cases[1], split="test")], chunks)
    # 即使题族名称不同，同一证据也不能跨划分。
    with pytest.raises(ValueError, match="证据小节跨划分"):
        validate([cases[0], replace(cases[1], family="other", split="test")], chunks)
    assert dataset_fingerprint(cases) != dataset_fingerprint([wrong, *cases[1:]])


def test_same_heading_wrong_source_or_missing_quote_is_not_a_hit():
    e = Evidence("a.md", "内容", "证据")
    assert matches(hit().chunk, e)
    assert not matches(hit("b.md").chunk, e)
    assert not matches(hit(text="其他内容").chunk, e)


def test_multi_evidence_counts_units_once_and_misses_are_zero():
    c = case((Evidence("a.md", "内容", "证据"), Evidence("b.md", "内容", "证据")))
    metrics = retrieval_metrics(c, [hit(), hit(), hit("b.md")], [hit(), hit()])
    assert metrics["candidate_recall@20"] == 1
    assert metrics["evidence_recall@5"] == .5
    assert metrics["hit@1"] == 1
    assert metrics["mrr@10"] == 1
    assert retrieval_metrics(c, [], [hit("unrelated.md")])["mrr@10"] == 0
    ten = [hit("unrelated.md")] * 9 + [hit()]
    assert retrieval_metrics(c, ten, ten)["mrr@10"] == .1
    assert retrieval_metrics(c, ten + [hit("b.md")], ten[:9] + [hit("unrelated.md"), hit()])["mrr@10"] == 0


def test_unanswerable_and_mixed_routing_not_in_retrieval_denominator():
    for c in all_cases():
        if c.group in ("unanswerable", "tool_routing"):
            assert all(v is None for v in retrieval_metrics(c, [hit()], [hit()]).values())


def test_failed_service_is_retained_in_operational_denominator():
    good = {"mode": "hybrid", "group": "literal", "status": "ok", "latency_ms": 3,
            "metric_eligible": True, "metrics": retrieval_metrics(case(), [hit()], [hit()])}
    failed = dict(good, status="error", latency_ms=30000, metrics={k: None for k in good["metrics"]})
    s = summarize([good, failed])[0]
    assert s["planned"] == 2 and s["failed"] == 1
    assert s["hit@1"] == 1 and s["hit@1_failure_as_zero"] == .5
    assert s["latency_ms_p95"] > 20000 and s["successful_latency_ms_p95"] == 3


class Embedder:
    model_id = "stub"

    def embed(self, texts):
        return [[float("证据" in t), 1.] for t in texts]


def test_bm25_engine_matches_production_lexical_order():
    chunks = load_corpus(KNOWLEDGE_DIR)
    engine = Engine(chunks, "fp")
    for q in ["LPR是什么", "应急资金", "怎么少交点税"]:
        candidates, ranked = engine.retrieve("bm25", q)
        assert candidates == search(engine.index, q, top_k=20) == ranked


def test_dense_child_dedup_does_not_starve_parent_candidates():
    chunks = [hit(text="证据" * 250).chunk, hit("b.md", text="证据").chunk]
    engine = Engine(chunks, "fp", embedder=Embedder())
    assert len(engine.parents) > 2
    assert set(engine.dense_rank("证据", 2)) == {0, 1}


@pytest.mark.parametrize("vectors", [[], [[]], [[float('nan')]], [[0, 0]], [[1, 2], [1]]])
def test_bad_embedding_output_rejected(vectors):
    with pytest.raises(ValueError):
        check_vectors(vectors, 1 if len(vectors) < 2 else 2)


def test_embedding_and_rerank_failures_never_fall_back():
    engine = Engine([hit().chunk, hit("b.md").chunk], "fp", embedder=Embedder())
    def fail(texts):
        raise TimeoutError()
    engine.embedder.embed = fail
    with pytest.raises(TimeoutError):
        engine.retrieve("hybrid", "证据")
    engine = Engine([hit().chunk, hit("b.md").chunk], "fp", embedder=Embedder(),
                    reranker=SimpleNamespace(scores=lambda query, docs: [None] * len(docs)))
    with pytest.raises(ValueError, match="重排缺失分数"):
        engine.retrieve("rerank", "证据")


def test_retrieval_cli_writes_full_results_without_api(tmp_path):
    output = tmp_path / "baseline.json"
    assert main(["--limit", "2", "--repeat", "2", "--output", str(output)]) == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert len(report["rows"]) == 4
    assert report["status"] == "completed" and report["planned"] == report["completed"] == 4
    assert report["calls"] == {"rewrite_invocations": 0}
    assert report["config"]["cache"].startswith("disabled")
    assert output.with_suffix(".md").is_file()
    assert report["rows"][0]["final_hits"][0]["text"]


def test_tool_routing_requires_both_slots_and_success_is_separate():
    c = case(tool_slots=(("query_transactions",), ("search_knowledge",)))
    events = [{"type": "tool_call", "name": "search_knowledge"},
              {"type": "tool_result", "name": "search_knowledge", "ok": False}]
    m = route_metrics(c, events)
    assert m["required_tool_recall"] == .5 and m["successful_tool_recall"] == 0
    assert m["route_correct"] == 0
    c = replace(c, tool_slots=(("query_transactions",),), forbidden_tools=("search_knowledge",))
    assert route_metrics(c, events)["forbidden_tool_called"]


def test_collector_captures_final_context_and_has_no_write_tools():
    class StubSession:
        messages = []

        def __init__(self, **kwargs):
            self.tools = kwargs["tools"]
            self.event = kwargs["on_event"]
            assert all(t.name not in {"save_memory", "categorize_transactions", "investigate", "use_skill"} for t in self.tools)
            assert "直接只读工具" in kwargs["system_prompt"]

        def send(self, question):
            tool = next(t for t in self.tools if t.name == "search_knowledge")
            self.event({"type": "tool_call", "name": tool.name, "args": {"query": question}})
            context = tool.invoke({"query": question})
            assert "证据" in context
            self.event({"type": "tool_result", "name": tool.name, "ok": True, "content": context})
            return "答案"

    engine = Engine([hit().chunk], "fp")
    row = collect_one(replace(case(), question="证据"), engine, "bm25", session_factory=StubSession)
    assert row["status"] == "ok"
    assert row["retrieved_contexts"] == ["a.md > 内容\n证据"]
    assert row["route"]["route_correct"] == 1
    failed = dict(row, status="error")
    assert summarize_agent([row, failed])[0]["route_correct"] == .5


def test_ragas_invalid_scores_and_dynamic_groups(tmp_path):
    table = score_table([{"m": 1.}, {"m": float("nan")}], ["m"], "test")
    assert "1.00" in table and "1/2" in table and "nan" not in table
    output = tmp_path / "report.md"
    sample = {"group": "multi_evidence", "question": "q", "source": "a.md",
              "search_queries": ["q"], "retrieved_contexts": ["a.md\n证据"],
              "annotation_status": "assistant_draft_pending_human_review"}
    write_findings([{"group": "multi_evidence", "context_recall": 1}],
                   ["context_recall"], [sample], 0, output=output)
    text = output.read_text(encoding="utf-8")
    assert "multi_evidence=1" in text and "17 题字面问法" not in text
    assert "assistant_draft_pending_human_review" in text


def test_manual_review_preserves_pending_and_requires_reviewer():
    row = {"id": "a", "trial": 1, "group": "literal", "reviewer": None,
           "key_point_coverage": None, "evidence_supported": None, "correct_abstention": None}
    result = summarize_review([row])[0]
    assert result["key_point_coverage"] == {"mean": None, "reviewed": 0, "pending": 1}
    with pytest.raises(ValueError, match="注明复核人"):
        summarize_review([dict(row, key_point_coverage=1)])
    with pytest.raises(ValueError, match="0到1"):
        summarize_review([dict(row, reviewer="reviewer", evidence_supported=float("nan"))])
    reviewed = dict(row, reviewer="reviewer", key_point_coverage=.5)
    assert summarize_review([row, dict(reviewed, id="b")])[0]["key_point_coverage"]["mean"] == .5


def test_ragas_dry_validates_export_without_optional_dependency(tmp_path):
    from eval.ragas_run import main as ragas_main
    path = tmp_path / "samples.json"
    path.write_text(json.dumps([{"question": "q", "reference": "reference", "response": "answer",
                                 "retrieved_contexts": ["a.md\n" + "正文" * 100],
                                 "group": "literal", "source": "a.md", "search_queries": ["q"]}]), encoding="utf-8")
    assert ragas_main(["--input", str(path), "--dry"]) == 0


def test_query_rewrites_keep_original_and_reject_malformed_output():
    model = SimpleNamespace(invoke=lambda q: SimpleNamespace(content='["改写一", "改写二", "改写三"]', usage_metadata={"input_tokens": 7}))
    queries, usage = rewrite_queries("原问题", "multi", model)
    assert queries == ["原问题", "改写一", "改写二", "改写三"]
    assert usage["input_tokens"] == 7
    model.invoke = lambda q: SimpleNamespace(content="假设性文档", usage_metadata=None)
    assert rewrite_queries("原问题", "hyde", model)[0] == ["原问题", "假设性文档"]
    model.invoke = lambda q: SimpleNamespace(content="[]", usage_metadata=None)
    with pytest.raises(ValueError, match="3个非空问句"):
        rewrite_queries("原问题", "multi", model)


def test_rerank_uses_original_question_and_ranks_all_candidates():
    calls = []
    def scores(q, docs):
        calls.append((q, len(docs)))
        return [float("b.md" in d) for d in docs]
    engine = Engine([hit().chunk, hit("b.md").chunk], "fp", embedder=Embedder(),
                    reranker=SimpleNamespace(scores=scores))
    candidates, ranked = engine.retrieve("rerank", "原问题", ["证据", "内容"])
    assert calls == [("原问题", len(candidates))]
    assert ranked[0].chunk.source == "b.md"
