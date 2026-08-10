from __future__ import annotations

import argparse
import json
import re
import statistics
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import numpy as np
from rank_bm25 import BM25Okapi

from agent_runtime.utils import atomic_write_json
from tests.evaluation.bm25_coarse_embedding_rerank_1000 import QwenEmbeddingCache
from tests.evaluation.hybrid_retrieval_1000 import (
    build_cases,
    case_result,
    concept_record,
    record_text,
    summarize,
)


BM25_TOP_K = 50
EMBEDDING_TOP_K = 15
FINAL_TOP_K = 5
SEMANTIC_THRESHOLD = 0.51
DISTRACTORS_PER_LAYER = 250
SEED_EMBEDDING_CACHE = Path(
    r"D:\pythoncode\antenna_agent_lab\docs\evaluations\hybrid_retrieval_1000_bm25_coarse_qwen_rerank_20260722\qwen_embedding_cache.json"
)

DOMAIN_TERMS = [
    "贴片天线",
    "微带天线",
    "缝隙天线",
    "多输入多输出天线",
    "圆极化天线",
    "天线阵列",
    "超表面天线",
    "滤波天线",
    "去耦结构",
    "轴比带宽",
    "回波损耗",
    "辐射效率",
    "特征模态分析",
    "代理模型",
    "论文复现",
    "几何重建",
    "几何建模",
    "仿真预检",
    "结果审查",
    "CST仿真",
    "S11",
    "S21",
    "VSWR",
    "ARBW",
    "CPW",
    "SIW",
    "RHCP",
    "LHCP",
    "GWO",
    "PSO",
    "GA",
    "CNN",
]

DISTRACTOR_TOPICS = [
    ("金融风险模型", "financial risk model"),
    ("股票回撤分析", "stock drawdown analysis"),
    ("网页性能优化", "web performance optimization"),
    ("React组件渲染", "React component rendering"),
    ("数据库查询计划", "database query plan"),
    ("图像语义分割", "image semantic segmentation"),
    ("自然语言分类", "natural language classification"),
    ("医学影像诊断", "medical image diagnosis"),
    ("天气趋势预测", "weather trend forecasting"),
    ("物流路径规划", "logistics route planning"),
    ("电池寿命估计", "battery lifetime estimation"),
    ("电机故障检测", "motor fault detection"),
    ("雷达目标识别", "radar target recognition"),
    ("音频降噪处理", "audio denoising processing"),
    ("推荐系统排序", "recommendation system ranking"),
    ("搜索引擎索引", "search engine indexing"),
    ("编译器性能分析", "compiler performance analysis"),
    ("容器资源调度", "container resource scheduling"),
    ("网络流量检测", "network traffic detection"),
    ("工业质量控制", "industrial quality control"),
    ("建筑能耗建模", "building energy modeling"),
    ("材料强度仿真", "material strength simulation"),
    ("流体边界计算", "fluid boundary computation"),
    ("机器人运动控制", "robot motion control"),
    ("教育报告生成", "education report generation"),
]

DISTRACTOR_INTENTS = [
    ("目标设置", "target setting"),
    ("参数约束", "parameter constraint"),
    ("证据收集", "evidence gathering"),
    ("模型构建", "model construction"),
    ("运行预检", "runtime preflight"),
    ("结果审查", "result review"),
    ("报告生成", "report generation"),
    ("用户偏好", "user preference"),
    ("失败恢复", "failure recovery"),
    ("输出格式", "output format"),
]


def current_ngram_tokens(text: str) -> list[str]:
    normalized = str(text or "").lower().replace("_", " ").replace("-", " ")
    tokens: list[str] = []
    for part in re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]+", normalized):
        if re.fullmatch(r"[\u4e00-\u9fff]+", part):
            tokens.append(part)
            tokens.extend(part[index:index + 2] for index in range(len(part) - 1))
            tokens.extend(part[index:index + 3] for index in range(len(part) - 2))
        else:
            tokens.append(part)
    return tokens


def _clean_segmented_tokens(parts: list[str]) -> list[str]:
    tokens: list[str] = []
    for part in parts:
        tokens.extend(re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]+", str(part).lower()))
    return tokens


@dataclass
class TokenizerVariant:
    name: str
    tokenize: Callable[[str], list[str]]
    init_ms: float
    implementation: str


def build_tokenizer(name: str) -> TokenizerVariant:
    started = time.perf_counter()
    if name == "current_ngram":
        return TokenizerVariant(name, current_ngram_tokens, (time.perf_counter() - started) * 1000, "regex+bigrams+trigrams")
    if name == "jieba_domain":
        import jieba

        tokenizer = jieba.Tokenizer()
        for term in DOMAIN_TERMS:
            tokenizer.add_word(term, freq=100000)

        def tokenize(text: str) -> list[str]:
            normalized = str(text or "").lower().replace("_", " ").replace("-", " ")
            return _clean_segmented_tokens(list(tokenizer.cut_for_search(normalized, HMM=False)))

        return TokenizerVariant(name, tokenize, (time.perf_counter() - started) * 1000, "jieba.cut_for_search+domain_dict")
    if name == "spacy_pkuseg_domain":
        import spacy_pkuseg

        tokenizer = spacy_pkuseg.pkuseg(user_dict=DOMAIN_TERMS)

        def tokenize(text: str) -> list[str]:
            normalized = str(text or "").lower().replace("_", " ").replace("-", " ")
            return _clean_segmented_tokens(tokenizer.cut(normalized))

        return TokenizerVariant(name, tokenize, (time.perf_counter() - started) * 1000, "spacy-pkuseg+domain_dict")
    raise ValueError(f"unknown tokenizer: {name}")


def build_records(corpora: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    records_by_layer: dict[str, list[dict[str, Any]]] = {}
    for layer, concepts in corpora.items():
        records = []
        for concept in concepts:
            if not concept.active:
                continue
            record = concept_record(concept)
            records.append({"id": concept.record_id, "text": record_text(layer, record), "distractor": False})
        for topic_index, (topic_zh, topic_en) in enumerate(DISTRACTOR_TOPICS):
            for intent_index, (intent_zh, intent_en) in enumerate(DISTRACTOR_INTENTS):
                records.append({
                    "id": f"tokenizer_{layer}_distractor_{topic_index:02d}_{intent_index:02d}",
                    "text": f"{topic_zh} {intent_zh}; {topic_en} {intent_en}; unrelated cross-domain operational record",
                    "distractor": True,
                })
        if len(records) != 50 + DISTRACTORS_PER_LAYER:
            raise AssertionError(f"unexpected {layer} corpus size: {len(records)}")
        records_by_layer[layer] = records
    return records_by_layer


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, int(len(ordered) * fraction) - 1))]


def build_semantic_rankings(
    records_by_layer: dict[str, list[dict[str, Any]]],
    cases: list[dict[str, Any]],
    embeddings: dict[str, list[float]],
) -> tuple[dict[str, list[tuple[str, float]]], float]:
    started = time.perf_counter()
    record_matrices = {
        layer: np.asarray([embeddings[record["text"]] for record in records], dtype=np.float32)
        for layer, records in records_by_layer.items()
    }
    rankings: dict[str, list[tuple[str, float]]] = {}
    for case in cases:
        layer = case["layer"]
        scores = record_matrices[layer] @ np.asarray(embeddings[case["query"]], dtype=np.float32)
        ids = [record["id"] for record in records_by_layer[layer]]
        order = sorted(range(len(ids)), key=lambda index: (-float(scores[index]), ids[index]))
        rankings[case["case_id"]] = [(ids[index], float(scores[index])) for index in order]
    return rankings, (time.perf_counter() - started) * 1000


def evaluate_variant(
    variant: TokenizerVariant,
    records_by_layer: dict[str, list[dict[str, Any]]],
    cases: list[dict[str, Any]],
    semantic_rankings: dict[str, list[tuple[str, float]]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    corpus_token_ms = 0.0
    corpus_token_counts: list[int] = []
    indexes: dict[str, dict[str, Any]] = {}
    index_build_ms = 0.0
    for layer, records in records_by_layer.items():
        corpus_tokens = []
        for record in records:
            started = time.perf_counter()
            tokens = variant.tokenize(record["text"])
            corpus_token_ms += (time.perf_counter() - started) * 1000
            corpus_token_counts.append(len(tokens))
            corpus_tokens.append(tokens)
        started = time.perf_counter()
        model = BM25Okapi(corpus_tokens)
        index_build_ms += (time.perf_counter() - started) * 1000
        indexes[layer] = {"model": model, "ids": [record["id"] for record in records]}

    results: list[dict[str, Any]] = []
    query_token_ms: list[float] = []
    query_token_counts: list[int] = []
    retrieval_started = time.perf_counter()
    for case in cases:
        started = time.perf_counter()
        token_started = time.perf_counter()
        query_tokens = variant.tokenize(case["query"])
        query_token_ms.append((time.perf_counter() - token_started) * 1000)
        query_token_counts.append(len(query_tokens))
        index = indexes[case["layer"]]
        bm25_scores = index["model"].get_scores(query_tokens)
        bm25_order = sorted(
            range(len(index["ids"])),
            key=lambda item: (-float(bm25_scores[item]), index["ids"][item]),
        )
        bm25_ids = [index["ids"][item] for item in bm25_order[:BM25_TOP_K]]
        semantic_order = semantic_rankings[case["case_id"]]
        embedding_ids = [record_id for record_id, _ in semantic_order[:EMBEDDING_TOP_K]]
        candidate_ids = set([*bm25_ids, *embedding_ids])
        reranked = [
            (record_id, score)
            for record_id, score in semantic_order
            if record_id in candidate_ids and score >= SEMANTIC_THRESHOLD
        ][:FINAL_TOP_K]
        returned_ids = [record_id for record_id, _ in reranked]
        latency_ms = (time.perf_counter() - started) * 1000
        row = case_result(case, variant.name, returned_ids, latency_ms)
        expected_id = case.get("expected_id")
        row["pipeline_trace"] = {
            "bm25_expected_rank": bm25_ids.index(expected_id) + 1 if expected_id in bm25_ids else None,
            "bm25_expected_hit": bool(expected_id and expected_id in bm25_ids),
            "embedding_expected_rank": next(
                (index + 1 for index, (record_id, _) in enumerate(semantic_order[:EMBEDDING_TOP_K]) if record_id == expected_id),
                None,
            ),
            "candidate_expected_hit": bool(expected_id and expected_id in candidate_ids),
            "candidate_count_before_dedupe": len(bm25_ids) + len(embedding_ids),
            "candidate_count_after_dedupe": len(candidate_ids),
            "returned_scores": [round(score, 6) for _, score in reranked],
        }
        results.append(row)

    positives = [row for row in results if row["expected_behavior"] == "retrieve"]
    bm25_ranks = [row["pipeline_trace"]["bm25_expected_rank"] for row in positives]
    bm25_ranks_present = [rank for rank in bm25_ranks if rank is not None]
    metrics = {
        "implementation": variant.implementation,
        "tokenizer_init_ms": round(variant.init_ms, 4),
        "corpus_records": sum(len(records) for records in records_by_layer.values()),
        "distractor_records": DISTRACTORS_PER_LAYER * len(records_by_layer),
        "corpus_tokenization_ms": round(corpus_token_ms, 4),
        "corpus_average_tokens": round(statistics.mean(corpus_token_counts), 4),
        "bm25_index_build_ms": round(index_build_ms, 4),
        "query_tokenization_ms_mean": round(statistics.mean(query_token_ms), 4),
        "query_tokenization_ms_p95": round(percentile(query_token_ms, 0.95), 4),
        "query_average_tokens": round(statistics.mean(query_token_counts), 4),
        "bm25_expected_hit_at_50": round(sum(row["pipeline_trace"]["bm25_expected_hit"] for row in positives) / len(positives), 4),
        "bm25_expected_top1_rate": round(sum(rank == 1 for rank in bm25_ranks) / len(positives), 4),
        "bm25_expected_top5_rate": round(sum(rank is not None and rank <= 5 for rank in bm25_ranks) / len(positives), 4),
        "bm25_expected_top10_rate": round(sum(rank is not None and rank <= 10 for rank in bm25_ranks) / len(positives), 4),
        "bm25_expected_mean_rank_if_hit": round(statistics.mean(bm25_ranks_present), 4) if bm25_ranks_present else None,
        "candidate_expected_hit": round(sum(row["pipeline_trace"]["candidate_expected_hit"] for row in positives) / len(positives), 4),
        "candidate_count_after_dedupe_mean": round(
            statistics.mean(row["pipeline_trace"]["candidate_count_after_dedupe"] for row in results), 4
        ),
        "retrieval_wall_time_ms": round((time.perf_counter() - retrieval_started) * 1000, 4),
    }
    return results, metrics


def write_report(path: Path, summary: dict[str, Any]) -> None:
    lines = [
        "# BM25 中文分词器隔离对比",
        "",
        f"- 查询数：{summary['case_count']}",
        f"- 每层语料：{summary['pipeline']['records_per_layer']}（含 {DISTRACTORS_PER_LAYER} 条跨领域干扰）",
        f"- 管线：BM25 Top50 + Qwen embedding Top15 -> 原始查询 embedding 精排 Top5 -> 阈值 {SEMANTIC_THRESHOLD}",
        f"- Qwen 外部调用：{summary['pipeline']['external_embedding_calls']}",
        "",
        "| 分词器 | BM25 Top1 | BM25 Top5 | BM25 Top10 | BM25 Top50 | 最终Top5命中 | Precision@5 | Recall@5 | F1@5 | 负例拒绝 | 平均延迟ms | P95ms | 平均query tokens |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name in summary["requested_tokenizers"]:
        if name not in summary["modes"]:
            continue
        mode = summary["modes"][name]
        detail = summary["tokenizer_metrics"][name]
        lines.append(
            f"| {name} | {detail['bm25_expected_top1_rate']:.2%} | {detail['bm25_expected_top5_rate']:.2%} | "
            f"{detail['bm25_expected_top10_rate']:.2%} | {detail['bm25_expected_hit_at_50']:.2%} | {mode['positive_topk_rate']:.2%} | "
            f"{mode['precision_at_k']:.2%} | {mode['recall_at_k']:.2%} | {mode['f1_at_k']:.2%} | "
            f"{mode['negative_rejection_rate']:.2%} | {mode['latency_ms_mean']:.4f} | "
            f"{mode['latency_ms_p95']:.4f} | {detail['query_average_tokens']:.2f} |"
        )
    if summary.get("unavailable_tokenizers"):
        lines.extend(["", "## 未完成项", ""])
        for name, error in summary["unavailable_tokenizers"].items():
            lines.append(f"- `{name}`：`{error}`")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def evaluate(output_dir: Path, requested_tokenizers: list[str]) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    cases, corpora = build_cases()
    records_by_layer = build_records(corpora)
    embedding_cache = QwenEmbeddingCache(output_dir, seed_cache_path=SEED_EMBEDDING_CACHE)
    texts = [record["text"] for records in records_by_layer.values() for record in records]
    texts.extend(case["query"] for case in cases)
    embeddings = embedding_cache.embed_many(texts)
    semantic_rankings, semantic_precompute_ms = build_semantic_rankings(records_by_layer, cases, embeddings)

    all_results: list[dict[str, Any]] = []
    tokenizer_metrics: dict[str, Any] = {}
    unavailable: dict[str, str] = {}
    for name in requested_tokenizers:
        try:
            variant = build_tokenizer(name)
            results, metrics = evaluate_variant(variant, records_by_layer, cases, semantic_rankings)
            all_results.extend(results)
            tokenizer_metrics[name] = metrics
        except Exception as exc:
            unavailable[name] = f"{type(exc).__name__}: {exc}"

    summary = summarize(all_results) if all_results else {
        "schema_version": "1.0",
        "evaluation": "tokenizer_ablation_1000",
        "case_count": len(cases),
        "modes": {},
        "layers": {},
        "categories": {},
    }
    summary["evaluation"] = "tokenizer_ablation_1000"
    summary["requested_tokenizers"] = requested_tokenizers
    summary["tokenizer_metrics"] = tokenizer_metrics
    summary["unavailable_tokenizers"] = unavailable
    summary["pipeline"] = {
        "bm25_top_k": BM25_TOP_K,
        "embedding_top_k": EMBEDDING_TOP_K,
        "final_top_k": FINAL_TOP_K,
        "semantic_threshold": SEMANTIC_THRESHOLD,
        "final_score": "original_query_qwen_embedding_cosine_only",
        "records_per_layer": len(records_by_layer["l2"]),
        "distractors_per_layer": DISTRACTORS_PER_LAYER,
        "embedding_model": embedding_cache.model,
        "embedding_dimensions": embedding_cache.dimensions,
        "external_embedding_calls": embedding_cache.external_calls,
        "semantic_precompute_ms": round(semantic_precompute_ms, 4),
    }
    atomic_write_json(output_dir / "summary.json", summary)
    (output_dir / "results.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in all_results),
        encoding="utf-8",
    )
    write_report(output_dir / "report.md", summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True)
    parser.add_argument(
        "--tokenizers",
        default="current_ngram,jieba_domain,spacy_pkuseg_domain",
        help="Comma-separated tokenizer names",
    )
    args = parser.parse_args()
    requested = [name.strip() for name in args.tokenizers.split(",") if name.strip()]
    print(json.dumps(evaluate(Path(args.output_dir), requested), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
