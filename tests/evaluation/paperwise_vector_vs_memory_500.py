from __future__ import annotations

import argparse
import gc
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import jieba
import numpy as np
from rank_bm25 import BM25Okapi

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_runtime.config import load_settings
from agent_runtime.embedding import embed_text as local_embed_text
from agent_runtime.utils import atomic_write_json


STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "using",
    "based",
    "through",
    "from",
    "this",
    "that",
    "research",
    "paper",
    "method",
    "antenna",
}

ANTENNA_DICTIONARY_PATH = ROOT / "agent_runtime" / "dictionaries" / "antenna_terms.txt"


def now_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.remove(temp_name)


def metadata_for_embedding(conn: sqlite3.Connection, row_id: int) -> dict[str, str]:
    metadata: dict[str, str] = {}
    rows = conn.execute(
        "SELECT key, string_value FROM embedding_metadata "
        "WHERE id = ? AND key IN ('paper_id', 'arxiv_id', 'title', 'source', 'path', 'published')",
        (row_id,),
    ).fetchall()
    embedding = conn.execute("SELECT embedding_id FROM embeddings WHERE id = ?", (row_id,)).fetchone()
    for key, value in rows:
        if key and value:
            metadata[str(key)] = str(value)
    if embedding and embedding[0]:
        metadata["embedding_id"] = str(embedding[0])
    return metadata


def tokenize_query(text: str) -> list[str]:
    tokens = re.findall(r"[a-z0-9]{2,}|[\u4e00-\u9fff]{2,}", str(text or "").lower())
    return [token for token in tokens if token not in STOPWORDS]


def bm25_tokens(text: str) -> list[str]:
    normalized = str(text or "").lower().replace("_", " ").replace("-", " ")
    tokens: list[str] = []
    for part in jieba.cut_for_search(normalized, HMM=False):
        tokens.extend(re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]+", str(part).lower()))
    return [token for token in tokens if token not in STOPWORDS]


def build_query(text: str, title: str) -> str:
    text_tokens = tokenize_query(text)
    title_tokens = tokenize_query(title)
    counts = Counter(text_tokens)
    important = [
        token
        for token, _count in counts.most_common(35)
        if token not in title_tokens and not token.isdigit()
    ]
    selected = [*title_tokens[:5], *important[:9]]
    return " ".join(dict.fromkeys(selected))[:260]


def load_paperwise_rows(db_path: Path) -> list[dict[str, Any]]:
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT rowid, c0 FROM embedding_fulltext_search_content "
            "WHERE length(c0) >= 120 ORDER BY rowid"
        ).fetchall()
        records: list[dict[str, Any]] = []
        for row_id, text in rows:
            metadata = metadata_for_embedding(conn, int(row_id))
            paper_id = metadata.get("paper_id") or metadata.get("arxiv_id")
            title = metadata.get("title") or ""
            embedding_id = metadata.get("embedding_id") or f"row:{row_id}"
            if not paper_id or not title:
                continue
            clean_text = " ".join(str(text).split())
            query = build_query(clean_text, title)
            if len(query.split()) < 4:
                continue
            records.append(
                {
                    "row_id": int(row_id),
                    "memory_id": embedding_id,
                    "embedding_id": embedding_id,
                    "paper_id": str(paper_id),
                    "title": title,
                    "published": metadata.get("published") or "",
                    "source": metadata.get("source") or "",
                    "text": clean_text[:1500],
                    "query": query,
                }
            )
        return records
    finally:
        conn.close()


def select_cases(records: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    by_paper: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        by_paper[record["paper_id"]].append(record)
    selected: list[dict[str, Any]] = []
    while len(selected) < limit:
        progressed = False
        for paper_id in sorted(by_paper):
            bucket = by_paper[paper_id]
            if not bucket:
                continue
            selected.append(bucket.pop(0))
            progressed = True
            if len(selected) >= limit:
                break
        if not progressed:
            break
    return [{**record, "case_id": f"PWV{index:04d}"} for index, record in enumerate(selected, start=1)]


@dataclass
class QwenBatchEmbedder:
    base_url: str
    api_key_env: str
    model_name: str
    dimensions: int | None = None
    timeout_seconds: int = 60
    batch_size: int = 10

    def __post_init__(self) -> None:
        api_key = os.getenv(self.api_key_env)
        if not api_key:
            raise RuntimeError(f"missing embedding API key: set {self.api_key_env}")
        from openai import OpenAI

        self._client = OpenAI(api_key=api_key, base_url=self.base_url, timeout=self.timeout_seconds)
        self.total_inputs = 0
        self.total_tokens = 0
        self.external_calls = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for batch in batched(texts, self.batch_size):
            payload: dict[str, Any] = {
                "model": self.model_name,
                "input": list(batch),
                "encoding_format": "float",
            }
            if self.dimensions is not None:
                payload["dimensions"] = self.dimensions
            response = self._client.embeddings.create(**payload)
            vectors.extend([list(map(float, item.embedding)) for item in response.data])
            self.total_inputs += len(batch)
            self.external_calls += 1
            usage = getattr(response, "usage", None)
            if usage is not None:
                self.total_tokens += int(getattr(usage, "total_tokens", 0) or 0)
        return vectors


def batched(items: list[Any], size: int) -> Iterable[list[Any]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]


class EmbeddingCache:
    def __init__(self, path: Path, embedder: QwenBatchEmbedder | None):
        self.path = path
        self.embedder = embedder
        self.data: dict[str, list[float]] = {}
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                self.data[str(row["id"])] = [float(value) for value in row["embedding"]]

    def get_many(self, ids: list[str], texts: list[str]) -> dict[str, list[float]]:
        missing_ids: list[str] = []
        missing_texts: list[str] = []
        for item_id, text in zip(ids, texts):
            if item_id not in self.data:
                missing_ids.append(item_id)
                missing_texts.append(text)
        if missing_ids:
            if self.embedder is None:
                vectors = [local_embed_text(text) for text in missing_texts]
            else:
                vectors = self.embedder.embed(missing_texts)
            for item_id, vector in zip(missing_ids, vectors):
                self.data[item_id] = vector
            self._save()
        return {item_id: self.data[item_id] for item_id in ids}

    def _save(self) -> None:
        rows = [
            json.dumps({"id": key, "embedding": vector}, ensure_ascii=False, separators=(",", ":"))
            for key, vector in sorted(self.data.items())
        ]
        atomic_write_text(self.path, "\n".join(rows) + "\n")


def normalize_matrix(vectors: list[list[float]]) -> np.ndarray:
    matrix = np.array(vectors, dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0.0] = 1.0
    return matrix / norms


class MemoryPipelineEvaluator:
    def __init__(
        self,
        records: list[dict[str, Any]],
        embedding_cache: EmbeddingCache,
        threshold: float,
        bm25_top_k: int = 50,
        embedding_top_k: int = 15,
    ):
        self.records = records
        self.threshold = threshold
        self.bm25_top_k = bm25_top_k
        self.embedding_top_k = embedding_top_k
        self.texts = [f"{record['title']} {record['text']}" for record in records]
        self.ids = [str(record["memory_id"]) for record in records]
        self.bm25 = BM25Okapi([bm25_tokens(text) for text in self.texts])
        vectors = embedding_cache.get_many(self.ids, self.texts)
        self.matrix = normalize_matrix([vectors[item_id] for item_id in self.ids])
        self.embedding_cache = embedding_cache

    def retrieve(self, query: str, top_k: int) -> list[dict[str, Any]]:
        started = time.perf_counter()
        qid = f"query:{query}"
        qvec = normalize_matrix([self.embedding_cache.get_many([qid], [query])[qid]])[0]
        semantic_scores = self.matrix @ qvec
        bm25_scores = self.bm25.get_scores(bm25_tokens(query))

        bm25_indices = np.argsort(-bm25_scores)[: self.bm25_top_k].tolist()
        embedding_indices = np.argsort(-semantic_scores)[: self.embedding_top_k].tolist()
        candidate_indices = sorted(set([*bm25_indices, *embedding_indices]))

        ranked: list[dict[str, Any]] = []
        for index in candidate_indices:
            score = float(semantic_scores[index])
            if score < self.threshold:
                continue
            record = self.records[index]
            coarse_sources = []
            if index in bm25_indices:
                coarse_sources.append("bm25_top50")
            if index in embedding_indices:
                coarse_sources.append("embedding_top15")
            ranked.append(
                {
                    "paper_id": record["paper_id"],
                    "title": record["title"],
                    "path": f"memory:{record['memory_id']}",
                    "score": score,
                    "bm25_score": float(bm25_scores[index]),
                    "coarse_sources": coarse_sources,
                    "latency_ms_internal": (time.perf_counter() - started) * 1000.0,
                }
            )
        ranked.sort(key=lambda item: (-float(item["score"]), str(item["paper_id"]), str(item["path"])))
        return ranked[:top_k]


class PaperWiseStoreEvaluator:
    def __init__(self, paperwise_root: Path, model_name: str, base_url: str, api_key_env: str):
        self.paperwise_root = paperwise_root
        self.model_name = model_name
        self.base_url = base_url
        self.api_key_env = api_key_env
        self._temp_dir: tempfile.TemporaryDirectory[str] | None = None
        self._previous_outputs: str | None = None
        self._inserted_sys_path = False
        self._store = None

    def __enter__(self) -> "PaperWiseStoreEvaluator":
        source_kb = self.paperwise_root / "outputs" / ".kb"
        if not source_kb.is_dir():
            raise RuntimeError(f"PaperWise KB not found: {source_kb}")
        self._temp_dir = tempfile.TemporaryDirectory(prefix="paperwise-eval-kb-")
        temp_outputs = Path(self._temp_dir.name) / "outputs"
        temp_kb = temp_outputs / ".kb"
        shutil.copytree(source_kb, temp_kb)
        bridge_qwen_env(self.api_key_env)
        self._previous_outputs = os.environ.get("PAPERWISE_OUTPUTS_DIR")
        os.environ["PAPERWISE_OUTPUTS_DIR"] = str(temp_outputs)
        os.environ["EMBEDDING_PROVIDER"] = "qwen"
        os.environ["EMBEDDING_MODEL"] = self.model_name
        root_text = str(self.paperwise_root)
        if root_text not in sys.path:
            sys.path.insert(0, root_text)
            self._inserted_sys_path = True
        for module_name in [name for name in sys.modules if name == "research_helper" or name.startswith("research_helper.")]:
            sys.modules.pop(module_name, None)
        from research_helper.kb import store
        from research_helper.llm import client as llm_client

        llm_client._BASE_URLS["qwen"] = self.base_url

        self._store = store
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        store_module = sys.modules.get("research_helper.kb.store")
        if store_module is not None:
            client = getattr(store_module, "_client", None)
            system = getattr(client, "_system", None)
            if system is not None:
                try:
                    system.stop()
                except Exception:
                    pass
            setattr(store_module, "_collection", None)
            setattr(store_module, "_client", None)
            try:
                from chromadb.api.client import SharedSystemClient

                SharedSystemClient.clear_system_cache()
            except Exception:
                pass
        if self._previous_outputs is None:
            os.environ.pop("PAPERWISE_OUTPUTS_DIR", None)
        else:
            os.environ["PAPERWISE_OUTPUTS_DIR"] = self._previous_outputs
        if self._inserted_sys_path:
            try:
                sys.path.remove(str(self.paperwise_root))
            except ValueError:
                pass
        for module_name in [name for name in sys.modules if name == "research_helper" or name.startswith("research_helper.")]:
            sys.modules.pop(module_name, None)
        gc.collect()
        if self._temp_dir is not None:
            self._temp_dir.cleanup()

    def retrieve(self, query: str, top_k: int) -> list[dict[str, Any]]:
        if self._store is None:
            raise RuntimeError("PaperWiseStoreEvaluator must be used as a context manager")
        entries = self._store.query(query, top_k=top_k, mode="paper")
        return [
            {
                "paper_id": str(entry.arxiv_id or ""),
                "title": str(entry.title or ""),
                "path": f"chroma:{entry.doc_id}",
                "score": max(0.0, 1.0 - float(entry.distance or 0.0)),
                "source": entry.source,
            }
            for entry in entries
        ]


def bridge_qwen_env(preferred_key_env: str | None = None) -> None:
    if preferred_key_env and os.getenv(preferred_key_env):
        os.environ["QWEN_API_KEY"] = os.getenv(preferred_key_env, "")
        return
    if not os.getenv("QWEN_API_KEY"):
        for key in ("API_KEY", "DASHSCOPE_API_KEY"):
            value = os.getenv(key)
            if value:
                os.environ["QWEN_API_KEY"] = value
                break


def evaluate_system(
    *,
    name: str,
    backend: str,
    queries: list[dict[str, Any]],
    retrieve,
    top_k: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    started = time.perf_counter()
    for case in queries:
        t0 = time.perf_counter()
        hits = retrieve(case["query"], top_k)
        latency_ms = (time.perf_counter() - t0) * 1000.0
        returned_papers = [str(hit.get("paper_id") or hit.get("arxiv_id") or "") for hit in hits]
        relevant = [paper_id for paper_id in returned_papers if paper_id == case["paper_id"]]
        returned_count = len(returned_papers)
        hit_at_k = bool(relevant)
        rows.append(
            {
                "case_id": case["case_id"],
                "system": name,
                "query": case["query"],
                "expected_paper_id": case["paper_id"],
                "returned_paper_ids": returned_papers,
                "returned_count": returned_count,
                "relevant_returned": len(relevant),
                "precision_at_k": (len(relevant) / returned_count) if returned_count else 0.0,
                "recall_at_k": 1.0 if hit_at_k else 0.0,
                "hit_at_k": hit_at_k,
                "latency_ms": round(latency_ms, 4),
                "top_hit_titles": [str(hit.get("title") or "")[:120] for hit in hits],
            }
        )
    total_ms = (time.perf_counter() - started) * 1000.0
    n = len(rows) or 1
    summary = {
        "system": name,
        "backend": backend,
        "cases": len(rows),
        "top_k": top_k,
        "precision_at_k": round(sum(row["precision_at_k"] for row in rows) / n, 4),
        "recall_at_k": round(sum(row["recall_at_k"] for row in rows) / n, 4),
        "hit_rate_at_k": round(sum(1 for row in rows if row["hit_at_k"]) / n, 4),
        "avg_latency_ms": round(sum(row["latency_ms"] for row in rows) / n, 4),
        "total_latency_ms": round(total_ms, 4),
        "avg_returned_count": round(sum(row["returned_count"] for row in rows) / n, 4),
    }
    return summary, rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--paperwise-root", default="")
    parser.add_argument("--cases", type=int, default=500)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--threshold", type=float, default=0.51)
    parser.add_argument("--external-embedding", action="store_true")
    parser.add_argument("--embedding-model", default="")
    parser.add_argument("--embedding-base-url", default="")
    parser.add_argument("--embedding-api-key-env", default="")
    parser.add_argument("--embedding-dimensions", default="")
    parser.add_argument("--output-dir", default="")
    args = parser.parse_args()

    settings = load_settings(ROOT / "config.yaml")
    paperwise_root = Path(args.paperwise_root or settings.paperwise_root)
    db_path = paperwise_root / "outputs" / ".kb" / "chroma.sqlite3"
    if not db_path.is_file():
        raise SystemExit(f"PaperWise Chroma DB not found: {db_path}")
    if not 0.0 <= args.threshold <= 1.0:
        raise SystemExit("--threshold must be between 0 and 1")

    if ANTENNA_DICTIONARY_PATH.exists():
        jieba.load_userdict(str(ANTENNA_DICTIONARY_PATH))

    output_dir = (
        Path(args.output_dir)
        if args.output_dir
        else ROOT / "docs" / "evaluations" / f"paperwise_vector_vs_memory_500_{now_id()}"
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    all_records = load_paperwise_rows(db_path)
    queries = select_cases(all_records, args.cases)
    if len(queries) < args.cases:
        raise SystemExit(f"Only built {len(queries)} cases from PaperWise KB")

    model_name = args.embedding_model or "text-embedding-v3"
    base_url = args.embedding_base_url or settings.embedding_base_url
    api_key_env = args.embedding_api_key_env or settings.embedding_api_key_env or "API_KEY"
    dimensions = (
        None
        if str(args.embedding_dimensions).lower() in {"", "none", "null"}
        else int(args.embedding_dimensions)
    )
    if args.embedding_dimensions == "":
        dimensions = settings.embedding_dimensions
    embedder: QwenBatchEmbedder | None = None
    if args.external_embedding:
        bridge_qwen_env(api_key_env)
        if not os.getenv(api_key_env):
            for fallback_key in ("API_KEY", "DASHSCOPE_API_KEY", "QWEN_API_KEY"):
                if os.getenv(fallback_key):
                    api_key_env = fallback_key
                    break
        embedder = QwenBatchEmbedder(
            base_url=base_url,
            api_key_env=api_key_env,
            model_name=model_name,
            dimensions=dimensions,
            timeout_seconds=max(settings.embedding_timeout_seconds, 60),
        )

    cache_path = output_dir / f"embedding_cache_{model_name if args.external_embedding else 'local'}.jsonl"
    embedding_cache = EmbeddingCache(cache_path, embedder)
    # Prewarm query embeddings in batches; otherwise the memory evaluator calls
    # the external embedding API once per query during scoring.
    if args.external_embedding:
        query_ids = [f"query:{case['query']}" for case in queries]
        query_texts = [case["query"] for case in queries]
        embedding_cache.get_many(query_ids, query_texts)
    memory_evaluator = MemoryPipelineEvaluator(
        records=all_records,
        embedding_cache=embedding_cache,
        threshold=args.threshold,
    )

    if args.external_embedding:
        with PaperWiseStoreEvaluator(
            paperwise_root,
            model_name=model_name,
            base_url=base_url,
            api_key_env=api_key_env,
        ) as paperwise:
            paperwise_summary, paperwise_rows = evaluate_system(
                name="paperwise_official_chroma_qwen",
                backend=f"PaperWise Chroma store.query + Qwen {model_name}",
                queries=queries,
                retrieve=paperwise.retrieve,
                top_k=args.top_k,
            )
    else:
        raise SystemExit("Use --external-embedding for real PaperWise semantic retrieval")

    memory_summary, memory_rows = evaluate_system(
        name="memory_bm2550_qwen15_semantic5_threshold",
        backend=f"BM25 top50 + Qwen top15 + original-query semantic top5, threshold={args.threshold}",
        queries=queries,
        retrieve=memory_evaluator.retrieve,
        top_k=args.top_k,
    )

    dataset_path = output_dir / "cases.jsonl"
    results_path = output_dir / "results.jsonl"
    summary_path = output_dir / "summary.json"
    atomic_write_text(
        dataset_path,
        "\n".join(json.dumps(case, ensure_ascii=False) for case in queries) + "\n",
    )
    atomic_write_text(
        results_path,
        "\n".join(json.dumps(row, ensure_ascii=False) for row in [*paperwise_rows, *memory_rows]) + "\n",
    )
    summary = {
        "schema_version": "1.0",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "paperwise_root": str(paperwise_root),
        "db_path": str(db_path),
        "cases": len(queries),
        "corpus_records": len(all_records),
        "top_k": args.top_k,
        "external_embedding": bool(args.external_embedding),
        "embedding_provider": "qwen" if args.external_embedding else "local",
        "embedding_model": model_name if args.external_embedding else "local_hash_embedding",
        "embedding_base_url": base_url if args.external_embedding else None,
        "embedding_api_key_env": api_key_env if args.external_embedding else None,
        "semantic_threshold": args.threshold,
        "metric_definitions": {
            "precision_at_k": "returned items with expected paper_id / returned items",
            "recall_at_k": "paper-level recall: 1 when expected paper_id appears in top_k else 0, averaged over cases",
            "hit_rate_at_k": "same binary event as paper-level recall, reported explicitly for search UX",
        },
        "systems": [paperwise_summary, memory_summary],
        "delta_memory_minus_paperwise": {
            key: round(memory_summary[key] - paperwise_summary[key], 4)
            for key in ("precision_at_k", "recall_at_k", "hit_rate_at_k", "avg_latency_ms")
        },
        "external_call_summary": {
            "embedding_inputs": embedder.total_inputs if embedder is not None else 0,
            "embedding_external_calls": embedder.external_calls if embedder is not None else 0,
            "embedding_tokens": embedder.total_tokens if embedder is not None else 0,
            "note": "PaperWise store.query also calls Qwen embeddings internally; its token counter is stored by PaperWise if enabled.",
        },
        "artifacts": {
            "cases": str(dataset_path),
            "results": str(results_path),
            "summary": str(summary_path),
            "embedding_cache": str(cache_path),
        },
    }
    atomic_write_json(summary_path, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
