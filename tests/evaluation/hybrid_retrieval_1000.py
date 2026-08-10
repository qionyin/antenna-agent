from __future__ import annotations

import argparse
import json
import statistics
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from rank_bm25 import BM25Okapi

from agent_runtime.embedding import cosine_similarity, embed_text
from agent_runtime.memory import MemoryManager
from agent_runtime.redis_store import RedisStore
from agent_runtime.retrieval import RetrievalEngine, _tokens
from agent_runtime.utils import now_iso


SEED = 20260721


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _normalized_relevance(
    query_tokens: set[str],
    text_tokens: set[str],
    fuzzy_score: float,
    vector_score: float,
) -> tuple[float, dict[str, float]]:
    overlap_ratio = len(query_tokens & text_tokens) / max(1, len(query_tokens))
    components = {
        "overlap": _clamp01(overlap_ratio),
        "fuzzy": _clamp01(fuzzy_score),
        "vector": _clamp01(vector_score),
    }
    score = components["overlap"] * 0.30 + components["fuzzy"] * 0.15 + components["vector"] * 0.55
    return _clamp01(score), components

L2_DOMAINS = [
    ("patch antenna", "microstrip radiator", "贴片天线"),
    ("slot antenna", "aperture radiator", "缝隙天线"),
    ("MIMO antenna", "multiport antenna", "多输入多输出天线"),
    ("circular polarization", "CP radiator", "圆极化天线"),
    ("antenna array", "phased radiator array", "天线阵列"),
    ("CST simulation", "electromagnetic solver", "CST仿真"),
    ("PaperWise evidence", "paper evidence base", "论文证据库"),
    ("React frontend", "web component interface", "前端组件"),
    ("Redis memory", "shared memory store", "Redis记忆"),
    ("technical report", "engineering report", "技术报告"),
]

L2_INTENTS = [
    ("target setting", "acceptance target", "目标设置"),
    ("user preference", "operator preference", "用户偏好"),
    ("execution constraint", "runtime restriction", "执行约束"),
    ("review policy", "validation rule", "审查规则"),
    ("output format", "result presentation", "输出格式"),
]

L3_DOMAINS = [
    ("paper reproduction", "literature replication", "论文复现"),
    ("antenna optimization", "radiator optimization", "天线优化"),
    ("CST single run", "single electromagnetic solve", "CST单次运行"),
    ("geometry reconstruction", "structure reconstruction", "几何重建"),
    ("claim assessment", "research claim validation", "结论评估"),
    ("evidence supplementation", "evidence gap filling", "证据补充"),
    ("frontend task display", "task dashboard rendering", "前端任务展示"),
    ("multi agent review", "cooperative agent review", "多智能体审查"),
    ("memory consolidation", "shared memory promotion", "记忆整理"),
    ("failure recovery", "interrupted task recovery", "失败恢复"),
]

L3_PHASES = [
    ("evidence gathering", "source collection", "证据收集"),
    ("geometry modeling", "model construction", "几何建模"),
    ("simulation preflight", "solver readiness check", "仿真预检"),
    ("result review", "output verification", "结果审查"),
    ("report generation", "report composition", "报告生成"),
]

UNRELATED_QUERIES = [
    "番茄炒蛋火候和盐量",
    "明天上海是否下雨",
    "古典钢琴和弦练习",
    "旅游酒店早餐时间",
    "篮球比赛罚球规则",
]

AMBIGUOUS_QUERIES = ["优化", "性能", "报告", "检查一下", "继续处理"]


@dataclass(frozen=True)
class Concept:
    record_id: str
    layer: str
    canonical: str
    paraphrase_en: str
    paraphrase_zh: str
    namespace: str
    confidence: float
    active: bool = True


def concepts_for_layer(layer: str) -> list[Concept]:
    domains = L2_DOMAINS if layer == "l2" else L3_DOMAINS
    intents = L2_INTENTS if layer == "l2" else L3_PHASES
    concepts: list[Concept] = []
    for domain_index, domain in enumerate(domains):
        for intent_index, intent in enumerate(intents):
            index = domain_index * len(intents) + intent_index
            record_id = f"eval_{layer}_{index:03d}"
            concepts.append(
                Concept(
                    record_id=record_id,
                    layer=layer,
                    canonical=f"{domain[0]} {intent[0]}",
                    paraphrase_en=f"{domain[1]} {intent[1]}",
                    paraphrase_zh=f"{domain[2]}{intent[2]}",
                    namespace=f"eval_20260721_{layer}_{domain_index:02d}",
                    confidence=round(0.72 + (index % 7) * 0.035, 3),
                )
            )
    for index in range(25):
        concepts.append(
            Concept(
                record_id=f"eval_{layer}_inactive_{index:03d}",
                layer=layer,
                canonical=f"deprecated legacy {layer} workflow rule {index}",
                paraphrase_en=f"retired obsolete {layer} process rule {index}",
                paraphrase_zh=f"已停用的{layer}旧流程规则{index}",
                namespace=f"eval_20260721_{layer}_inactive",
                confidence=0.99,
                active=False,
            )
        )
    return concepts


def typo(text: str) -> str:
    words = text.split()
    for index, word in enumerate(words):
        if len(word) >= 6 and word.isascii():
            words[index] = word[:2] + word[3] + word[2] + word[4:]
            return " ".join(words)
    return text + " typo"


def positive_queries(concept: Concept) -> list[tuple[str, str]]:
    words = concept.canonical.split()
    return [
        ("exact", concept.canonical),
        ("token_reorder", " ".join(reversed(words))),
        ("synonym_zh", concept.paraphrase_zh),
        ("paraphrase_en", concept.paraphrase_en),
        ("typo", typo(concept.canonical)),
        ("noise", f"临时上下文 2026 请优先找回 {concept.paraphrase_zh} 其余忽略"),
        ("partial_drop", " ".join(words[:-1]) if len(words) > 2 else words[0]),
        ("cross_domain_noise", f"{concept.canonical} React finance weather unrelated"),
    ]


def build_cases() -> tuple[list[dict[str, Any]], dict[str, list[Concept]]]:
    cases: list[dict[str, Any]] = []
    corpora: dict[str, list[Concept]] = {}
    case_number = 1
    for layer in ("l2", "l3"):
        concepts = concepts_for_layer(layer)
        corpora[layer] = concepts
        active = [item for item in concepts if item.active]
        inactive = [item for item in concepts if not item.active]
        for concept in active:
            related = [item.record_id for item in active if item.namespace == concept.namespace]
            target_index = related.index(concept.record_id)
            related_limit = 3 if layer == "l2" else 2
            relevant_ids = [concept.record_id]
            for offset in range(1, len(related)):
                candidate_id = related[(target_index + offset) % len(related)]
                if candidate_id not in relevant_ids:
                    relevant_ids.append(candidate_id)
                if len(relevant_ids) >= related_limit:
                    break
            for category, query in positive_queries(concept):
                cases.append(
                    {
                        "case_id": f"HR{case_number:04d}",
                        "layer": layer,
                        "category": category,
                        "query": query,
                        "expected_behavior": "retrieve",
                        "expected_id": concept.record_id,
                        "relevant_ids": relevant_ids,
                        "forbidden_id": None,
                        "difficulty": "easy" if category in {"exact", "token_reorder"} else "hard",
                    }
                )
                case_number += 1
        for index in range(25):
            negative_groups = [
                ("unrelated", f"{UNRELATED_QUERIES[index % len(UNRELATED_QUERIES)]} {index}"),
                ("ambiguous_short", AMBIGUOUS_QUERIES[index % len(AMBIGUOUS_QUERIES)]),
                ("explicit_negation", f"不要召回 {active[index].canonical}"),
                ("inactive_memory", inactive[index].canonical),
            ]
            for category, query in negative_groups:
                forbidden_id = active[index].record_id if category == "explicit_negation" else inactive[index].record_id if category == "inactive_memory" else None
                cases.append(
                    {
                        "case_id": f"HR{case_number:04d}",
                        "layer": layer,
                        "category": category,
                        "query": query,
                        "expected_behavior": "reject",
                        "expected_id": None,
                        "relevant_ids": [],
                        "forbidden_id": forbidden_id,
                        "difficulty": "boundary",
                    }
                )
                case_number += 1
    assert len(cases) == 1000, len(cases)
    return cases, corpora


def concept_record(concept: Concept) -> dict[str, Any]:
    text = f"{concept.canonical}; aliases: {concept.paraphrase_en}; {concept.paraphrase_zh}"
    if concept.layer == "l2":
        return {
            "memory_id": concept.record_id,
            "namespace": concept.namespace,
            "status": "active" if concept.active else "inactive",
            "confidence": concept.confidence,
            "fact": {"text": text, "source": "hybrid_retrieval_1000_fixture", "confidence": concept.confidence},
        }
    return {
        "memory_id": concept.record_id,
        "workflow_key": concept.record_id,
        "domain": concept.namespace,
        "goal_pattern": concept.canonical,
        "summary": text,
        "status": "active" if concept.active else "inactive",
        "task_status": "completed",
        "unresolved_failures": 0,
        "graph_step": 8,
        "workflow_quality": concept.confidence,
    }


def populate(memory: MemoryManager, corpora: dict[str, list[Concept]]) -> None:
    for concept in corpora["l2"]:
        record = concept_record(concept)
        memory.l2[concept.record_id] = record
        if memory.store is not None:
            key = f"mem:l2:{concept.namespace}:{concept.record_id}"
            text = memory._l2_text(record)
            memory.store.set_json(key, record)
            memory.store.upsert_vector("l2", concept.record_id, text, memory.embed_text(text), record)
    for concept in corpora["l3"]:
        record = concept_record(concept)
        memory.l3[concept.record_id] = record
        if memory.store is not None:
            key = f"mem:l3:workflow:{concept.record_id}"
            text = memory._l3_text(concept.record_id, record)
            memory.store.set_json(key, record)
            memory.store.upsert_vector("l3", concept.record_id, text, memory.embed_text(text), record)


def record_text(layer: str, record: dict[str, Any]) -> str:
    if layer == "l2":
        return f"{record.get('namespace', '')} {record.get('fact', '')}"
    return f"{record.get('workflow_key', '')} {record}"


def baseline_rankings(query: str, layer: str, records: list[dict[str, Any]], top_k: int) -> dict[str, list[str]]:
    query_tokens = _tokens(query)
    query_vector = embed_text(query)
    scored: dict[str, list[tuple[float, float, str]]] = defaultdict(list)
    for record in records:
        if record.get("status", "active") != "active":
            continue
        record_id = str(record.get("memory_id") or record.get("workflow_key"))
        text = record_text(layer, record)
        overlap = len(query_tokens & _tokens(text))
        fuzzy = SequenceMatcher(None, query.lower(), text.lower()).ratio()
        vector = cosine_similarity(query_vector, embed_text(text))
        confidence = float(record.get("confidence", record.get("workflow_quality", 0.0)) or 0.0)
        if overlap > 0:
            scored["keyword"].append((float(overlap), 0.0, record_id))
        if fuzzy >= 0.32:
            scored["fuzzy"].append((fuzzy, 0.0, record_id))
        if vector > 0:
            scored["vector"].append((vector, 0.0, record_id))
        if overlap > 0 or fuzzy >= 0.32 or vector > 0:
            relevance, _ = _normalized_relevance(query_tokens, _tokens(text), fuzzy, vector)
            scored["hybrid_full_scan"].append((relevance, confidence, record_id))
    return {
        mode: [record_id for _, _, record_id in sorted(rows, key=lambda item: (-item[0], -item[1], item[2]))[:top_k]]
        for mode, rows in scored.items()
    }


def build_bm25_indexes(records: dict[str, list[dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    indexes = {}
    for layer, rows in records.items():
        active = [row for row in rows if row.get("status", "active") == "active"]
        ids = [str(row.get("memory_id") or row.get("workflow_key")) for row in active]
        corpus = [sorted(_tokens(record_text(layer, row))) for row in active]
        started = time.perf_counter()
        model = BM25Okapi(corpus)
        build_ms = (time.perf_counter() - started) * 1000
        indexes[layer] = {"model": model, "ids": ids, "build_ms": build_ms}
    return indexes


def bm25_ranking(index: dict[str, Any], query: str, top_k: int) -> list[str]:
    scores = index["model"].get_scores(sorted(_tokens(query)))
    ranked = sorted(
        ((float(score), record_id) for score, record_id in zip(scores, index["ids"]) if float(score) > 0.0),
        key=lambda item: (-item[0], item[1]),
    )
    return [record_id for _, record_id in ranked[:top_k]]


def rank_of(target: str | None, ids: list[str]) -> int | None:
    if not target:
        return None
    try:
        return ids.index(target) + 1
    except ValueError:
        return None


def f1_score(precision: float, recall: float) -> float:
    if precision + recall == 0.0:
        return 0.0
    return 2.0 * precision * recall / (precision + recall)


def case_result(case: dict[str, Any], mode: str, ids: list[str], latency_ms: float) -> dict[str, Any]:
    rank = rank_of(case.get("expected_id"), ids)
    relevant_ids = set(case.get("relevant_ids") or [])
    relevant_returned = [record_id for record_id in ids if record_id in relevant_ids]
    recall_at_k = len(relevant_returned) / len(relevant_ids) if relevant_ids else None
    precision_at_k = len(relevant_returned) / len(ids) if ids and relevant_ids else 0.0 if relevant_ids else None
    forbidden_hit = bool(case.get("forbidden_id") and case["forbidden_id"] in ids)
    if case["expected_behavior"] == "retrieve":
        passed = rank is not None
    else:
        passed = not ids and not forbidden_hit
    return {
        **case,
        "mode": mode,
        "returned_ids": ids,
        "returned_count": len(ids),
        "rank": rank,
        "top1_hit": rank == 1,
        "topk_hit": rank is not None,
        "hit_rate_at_k": 1.0 if rank is not None else 0.0,
        "relevant_returned": relevant_returned,
        "recall_at_k": round(recall_at_k, 4) if recall_at_k is not None else None,
        "precision_at_k": round(precision_at_k, 4) if precision_at_k is not None else None,
        "forbidden_hit": forbidden_hit,
        "passed": passed,
        "latency_ms": round(latency_ms, 4),
    }


def evaluate_engine(name: str, memory: MemoryManager, cases: list[dict[str, Any]], corpora: dict[str, list[Concept]]) -> list[dict[str, Any]]:
    engine = RetrievalEngine(memory)
    records = {layer: [concept_record(item) for item in concepts] for layer, concepts in corpora.items()}
    bm25_indexes = build_bm25_indexes(records) if name == "redis_hybrid" else {}
    results: list[dict[str, Any]] = []
    for case in cases:
        layer = case["layer"]
        top_k = 5 if layer == "l2" else 3
        started = time.perf_counter()
        hits = engine.retrieve_l2(case["query"], top_k) if layer == "l2" else engine.retrieve_l3(case["query"], top_k)
        elapsed = (time.perf_counter() - started) * 1000
        ids = [str(item.get("memory_id") or item.get("workflow_key") or item.get("id")) for item in hits]
        results.append(case_result(case, name, ids, elapsed))
        if name == "redis_hybrid":
            started = time.perf_counter()
            rankings = baseline_rankings(case["query"], layer, records[layer], top_k)
            baseline_elapsed = (time.perf_counter() - started) * 1000
            for baseline_name in ("keyword", "fuzzy", "vector", "hybrid_full_scan"):
                results.append(case_result(case, baseline_name, rankings.get(baseline_name, []), baseline_elapsed))
            started = time.perf_counter()
            bm25_ids = bm25_ranking(bm25_indexes[layer], case["query"], top_k)
            bm25_elapsed = (time.perf_counter() - started) * 1000
            bm25_result = case_result(case, "bm25_okapi", bm25_ids, bm25_elapsed)
            bm25_result["index_build_ms"] = round(float(bm25_indexes[layer]["build_ms"]), 4)
            results.append(bm25_result)
    return results


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    by_mode: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for result in results:
        by_mode[result["mode"]].append(result)
    mode_summaries = {}
    for mode, rows in sorted(by_mode.items()):
        positives = [row for row in rows if row["expected_behavior"] == "retrieve"]
        negatives = [row for row in rows if row["expected_behavior"] == "reject"]
        latencies = [row["latency_ms"] for row in rows]
        reciprocal_ranks = [1.0 / row["rank"] if row["rank"] else 0.0 for row in positives]
        recalls = [float(row["recall_at_k"] or 0.0) for row in positives]
        precisions = [float(row["precision_at_k"] or 0.0) for row in positives]
        recall_at_k = sum(recalls) / len(recalls)
        precision_at_k = sum(precisions) / len(precisions)
        mode_summaries[mode] = {
            "cases": len(rows),
            "positive_cases": len(positives),
            "negative_cases": len(negatives),
            "overall_pass_rate": round(sum(row["passed"] for row in rows) / len(rows), 4),
            "positive_top1_rate": round(sum(row["top1_hit"] for row in positives) / len(positives), 4),
            "positive_topk_rate": round(sum(row["topk_hit"] for row in positives) / len(positives), 4),
            "hit_rate_at_k": round(sum(row["hit_rate_at_k"] for row in positives) / len(positives), 4),
            "recall_at_k": round(recall_at_k, 4),
            "precision_at_k": round(precision_at_k, 4),
            "f1_at_k": round(f1_score(precision_at_k, recall_at_k), 4),
            "mrr": round(sum(reciprocal_ranks) / len(reciprocal_ranks), 4),
            "negative_rejection_rate": round(sum(row["passed"] for row in negatives) / len(negatives), 4),
            "forbidden_hit_rate": round(sum(row["forbidden_hit"] for row in negatives) / len(negatives), 4),
            "latency_ms_mean": round(statistics.mean(latencies), 4),
            "latency_ms_p50": round(statistics.median(latencies), 4),
            "latency_ms_p95": round(sorted(latencies)[max(0, int(len(latencies) * 0.95) - 1)], 4),
            "latency_ms_max": round(max(latencies), 4),
            "latency_ms_total": round(sum(latencies), 4),
            "index_build_ms": round(max(float(row.get("index_build_ms") or 0.0) for row in rows), 4),
        }
    category_summary = {}
    for (mode, category), rows in sorted(_group(results, lambda row: (row["mode"], row["category"])).items()):
        recall_at_k = sum(float(row["recall_at_k"] or 0.0) for row in rows) / len(rows)
        precision_at_k = sum(float(row["precision_at_k"] or 0.0) for row in rows) / len(rows)
        category_summary[f"{mode}:{category}"] = {
            "cases": len(rows),
            "pass_rate": round(sum(row["passed"] for row in rows) / len(rows), 4),
            "top1_rate": round(sum(row["top1_hit"] for row in rows) / len(rows), 4),
            "topk_rate": round(sum(row["topk_hit"] for row in rows) / len(rows), 4),
            "recall_at_k": round(recall_at_k, 4),
            "precision_at_k": round(precision_at_k, 4),
            "f1_at_k": round(f1_score(precision_at_k, recall_at_k), 4),
        }
    layer_summary = {}
    for (mode, layer), rows in sorted(_group(results, lambda row: (row["mode"], row["layer"])).items()):
        positives = [row for row in rows if row["expected_behavior"] == "retrieve"]
        negatives = [row for row in rows if row["expected_behavior"] == "reject"]
        recall_at_k = sum(float(row["recall_at_k"] or 0.0) for row in positives) / len(positives)
        precision_at_k = sum(float(row["precision_at_k"] or 0.0) for row in positives) / len(positives)
        layer_summary[f"{mode}:{layer}"] = {
            "cases": len(rows),
            "hit_rate_at_k": round(sum(row["hit_rate_at_k"] for row in positives) / len(positives), 4),
            "recall_at_k": round(recall_at_k, 4),
            "precision_at_k": round(precision_at_k, 4),
            "f1_at_k": round(f1_score(precision_at_k, recall_at_k), 4),
            "negative_rejection_rate": round(sum(row["passed"] for row in negatives) / len(negatives), 4),
        }
    return {
        "schema_version": "1.0",
        "evaluation": "hybrid_retrieval_1000",
        "fixture_seed": SEED,
        "created_at": now_iso(),
        "case_count": len({row["case_id"] for row in results}),
        "result_row_count": len(results),
        "modes": mode_summaries,
        "layers": layer_summary,
        "categories": category_summary,
        "failure_counts": dict(Counter(f"{row['mode']}:{row['category']}" for row in results if not row["passed"])),
    }


def _group(rows: list[dict[str, Any]], key):
    groups = defaultdict(list)
    for row in rows:
        groups[key(row)].append(row)
    return groups


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def write_report(path: Path, summary: dict[str, Any], redis_health: dict[str, Any]) -> None:
    lines = [
        "# Hybrid Retrieval 1000 条评测",
        "",
        f"- 固定种子: `{SEED}`",
        f"- 测试案例: `{summary['case_count']}`",
        "- L2: 500；L3: 500；正向: 800；拒绝边界: 200。",
        f"- Redis: `{redis_health}`",
        "- 生产向量器: 本地 64 维 token/trigram 哈希向量。",
        "",
        "## 总体结果",
        "",
        "| 模式 | Precision@K | Recall@K | F1@K | Hit@K | 平均 ms | P50 ms | P95 ms | 最大 ms | 1000 条累计 ms | 索引构建 ms |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for mode, row in summary["modes"].items():
        lines.append(
            f"| {mode} | {row['precision_at_k']:.1%} | {row['recall_at_k']:.1%} | "
            f"{row['f1_at_k']:.1%} | {row['hit_rate_at_k']:.1%} | {row['latency_ms_mean']:.3f} | "
            f"{row['latency_ms_p50']:.3f} | {row['latency_ms_p95']:.3f} | "
            f"{row['latency_ms_max']:.3f} | {row['latency_ms_total']:.1f} | {row['index_build_ms']:.3f} |"
        )
    lines.extend(
        [
            "",
            "## L2/L3 分层",
            "",
            "| 模式与层级 | Precision@K | Recall@K | F1@K | Hit@K |",
            "|---|---:|---:|---:|---:|",
            *[
                f"| {key} | {row['precision_at_k']:.1%} | {row['recall_at_k']:.1%} | "
                f"{row['f1_at_k']:.1%} | {row['hit_rate_at_k']:.1%} |"
                for key, row in summary["layers"].items()
            ],
            "",
            "## 判定说明",
            "",
            "- Precision@K：返回结果中属于相关记忆集合的比例。",
            "- F1@K：宏平均 Precision@K 与 Recall@K 的调和平均，用作阈值选择主指标。",
            "- 负向案例：无返回且未召回禁止目标才通过，属于严格拒绝指标。",
            "- `redis_hybrid` 是当前生产检索器在 Redis 可用但无 RediSearch 时的真实路径。",
            "- `memory_fallback_hybrid` 是 Redis 不可用时的真实内存路径。",
            "- `hybrid_full_scan` 使用相同启发式分数但扫描全部活动记录，用于定位候选召回损失。",
            "- `bm25_okapi` 使用 `rank_bm25.BM25Okapi`，只统计查询延迟；索引构建耗时单列。",
            "",
            "## 边界覆盖",
            "",
            "`exact`、`token_reorder`、`synonym_zh`、`paraphrase_en`、`typo`、`noise`、",
            "`partial_drop`、`cross_domain_noise`、`unrelated`、`ambiguous_short`、",
            "`explicit_negation`、`inactive_memory`。",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--redis-url", default="redis://127.0.0.1:6379/15")
    args = parser.parse_args()
    evaluation_started = time.perf_counter()
    output_dir = Path(args.output_dir)
    cases, corpora = build_cases()

    store = RedisStore(args.redis_url)
    if not store.available:
        raise RuntimeError(f"Redis is required for redis_hybrid evaluation: {store.health()}")
    existing = store.count_keys("*")
    if existing:
        raise RuntimeError(f"evaluation Redis database must be empty, found {existing} keys")
    redis_memory = MemoryManager(store=store)
    populate(redis_memory, corpora)
    redis_results = evaluate_engine("redis_hybrid", redis_memory, cases, corpora)

    fallback_memory = MemoryManager(store=None)
    populate(fallback_memory, corpora)
    fallback_results = evaluate_engine("memory_fallback_hybrid", fallback_memory, cases, corpora)
    results = redis_results + fallback_results
    summary = summarize(results)
    summary["evaluation_wall_time_ms"] = round((time.perf_counter() - evaluation_started) * 1000, 4)

    write_jsonl(output_dir / "hybrid_retrieval_1000_cases.jsonl", cases)
    write_jsonl(output_dir / "hybrid_retrieval_1000_results.jsonl", results)
    (output_dir / "hybrid_retrieval_1000_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    write_report(output_dir / "hybrid_retrieval_1000_report.md", summary, store.health())
    cleanup = store.delete_pattern("mem:l2:eval_20260721_*") + store.delete_pattern("mem:l3:workflow:eval_*")
    print(json.dumps({"summary": summary, "cleanup_keys": cleanup}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
