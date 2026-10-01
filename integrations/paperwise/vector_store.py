from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openai import OpenAI

from agent_runtime.config import load_settings
from agent_runtime.embedding import normalize_vector
from agent_runtime.utils import atomic_write_json
from .common import BM25_RECALL_TOP_K, EMBEDDING_RECALL_TOP_K, PAPERWISE_HYBRID_MIN_SCORE, PROJECT_ROOT, BM25Okapi, _bm25_tokens, _cache_safe_model_name, _cosine, _embedding_cache_key


class PaperWiseVectorStoreMixin:
    def _vector_library_summary(
        self,
        query: str,
        limit: int,
        profile: dict[str, Any],
        allow_external_embedding: bool = False,
    ) -> dict[str, Any]:
        db_path = self.root / "outputs" / ".kb" / "chroma.sqlite3"
        result: dict[str, Any] = {
            "status": "missing",
            "path": str(db_path),
            "roles": ["reproduction", "evidence"],
            "description": "PaperWise vector library stores deep-read paper chunks for reproduction and evidence lookup.",
            "support_level": "insufficient_evidence",
            "items": [],
            "chunk_count": 0,
            "paper_count": 0,
            "read_only": True,
            "retrieval_backend": "not_loaded",
            "vector_query_error": None,
        }
        if not db_path.is_file():
            return result
        vector_items: list[dict[str, Any]] = []
        try:
            conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
            try:
                result["chunk_count"] = self._sqlite_count(conn, "embeddings")
                result["metadata_count"] = self._sqlite_count(conn, "embedding_metadata")
                result["paper_count"] = self._vector_paper_count(conn)
                try:
                    vector_items = self._project_hybrid_vector_query_items(
                        conn,
                        query,
                        limit,
                        profile,
                        allow_external_embedding=allow_external_embedding,
                    )
                    result["retrieval_backend"] = (
                        "project_hybrid_bm25_50_qwen_15_semantic_rerank"
                        if allow_external_embedding
                        else "project_bm25_sqlite_fallback"
                    )
                except Exception as exc:
                    result["vector_query_error"] = self._vector_query_error(exc)
                    vector_items = self._vector_query_items(conn, query, limit, profile)
                    result["retrieval_backend"] = "project_sqlite_like_fallback_after_hybrid_error"
                result["items"] = vector_items
                result["status"] = "available" if result["items"] else "insufficient_evidence"
                result["support_level"] = "candidate_evidence" if result["status"] == "available" else "insufficient_evidence"
                if not result["items"] and result["chunk_count"]:
                    result["reason"] = "vector library exists but no query-related deep-reading chunk was found"
            finally:
                conn.close()
        except Exception as exc:
            result["status"] = "unreadable"
            result["error"] = str(exc)
        return result

    def _project_hybrid_vector_query_items(
        self,
        conn: sqlite3.Connection,
        query: str,
        limit: int,
        profile: dict[str, Any],
        allow_external_embedding: bool,
    ) -> list[dict[str, Any]]:
        if not query.strip():
            return []
        records = self._paperwise_vector_records(conn)
        if not records:
            return []
        corpus_tokens = [_bm25_tokens(record["search_text"]) for record in records]
        bm25 = BM25Okapi(corpus_tokens)
        query_tokens = _bm25_tokens(query)
        bm25_scores = [float(score) for score in bm25.get_scores(query_tokens)]
        bm25_indices = sorted(
            range(len(records)),
            key=lambda index: (-bm25_scores[index], records[index]["embedding_id"]),
        )[:BM25_RECALL_TOP_K]

        embedding_scores: dict[int, float] = {}
        embedding_indices: list[int] = []
        query_vector: list[float] | None = None
        if allow_external_embedding:
            settings = load_settings(PROJECT_ROOT / "config.yaml")
            model_name = os.getenv("PAPERWISE_HYBRID_EMBEDDING_MODEL") or "text-embedding-v3"
            base_url = os.getenv("PAPERWISE_HYBRID_EMBEDDING_BASE_URL") or settings.embedding_base_url
            api_key_env = os.getenv("PAPERWISE_HYBRID_EMBEDDING_API_KEY_ENV") or settings.embedding_api_key_env
            query_vector = self._embed_external_texts(
                [query],
                model_name=model_name,
                base_url=base_url,
                api_key_env=api_key_env,
                dimensions=settings.embedding_dimensions,
            )[0]
            record_vectors = self._paperwise_record_embeddings(
                records,
                model_name=model_name,
                base_url=base_url,
                api_key_env=api_key_env,
                dimensions=settings.embedding_dimensions,
            )
            embedding_scores = {
                index: _cosine(query_vector, record_vectors.get(records[index]["cache_key"], []))
                for index in range(len(records))
            }
            embedding_indices = sorted(
                range(len(records)),
                key=lambda index: (-embedding_scores.get(index, 0.0), records[index]["embedding_id"]),
            )[:EMBEDDING_RECALL_TOP_K]

        candidate_indices = sorted(set([*bm25_indices, *embedding_indices]))
        candidates: list[dict[str, Any]] = []
        for index in candidate_indices:
            record = records[index]
            clean = record["text"]
            relevance = self._relevance(clean, profile)
            semantic_score = embedding_scores.get(index)
            if semantic_score is not None and semantic_score < PAPERWISE_HYBRID_MIN_SCORE:
                continue
            score = semantic_score if semantic_score is not None else self._normalize_bm25_score(bm25_scores[index], bm25_scores)
            item = self._paperwise_vector_item_from_record(
                record,
                score=score,
                relevance=relevance,
                retrieval_backend=(
                    "project_hybrid_bm25_50_qwen_15_semantic_rerank"
                    if allow_external_embedding
                    else "project_bm25_sqlite_fallback"
                ),
                bm25_score=bm25_scores[index],
                coarse_sources=[
                    *("bm25_top50" for _ in [0] if index in bm25_indices),
                    *("embedding_top15" for _ in [0] if index in embedding_indices),
                ],
            )
            candidates.append(item)
        candidates.sort(key=lambda item: (-float(item.get("score", 0.0)), str(item.get("paper_id") or ""), str(item.get("path") or "")))
        return self._dedupe_papers(candidates, limit)

    def _paperwise_vector_records(self, conn: sqlite3.Connection) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT rowid, c0 FROM embedding_fulltext_search_content "
            "WHERE length(c0) >= 120 ORDER BY rowid"
        ).fetchall()
        records: list[dict[str, Any]] = []
        for rowid, text in rows:
            clean = " ".join(str(text).split())
            metadata = self._vector_metadata_for_embedding(conn, int(rowid))
            embedding_id = metadata.get("embedding_id") or f"row:{rowid}"
            paper_id = metadata.get("paper_id") or metadata.get("arxiv_id") or embedding_id.split("::", 1)[0]
            paper_title = metadata.get("title") or clean.lstrip("# ").split("**", 1)[0][:120] or f"PaperWise chunk {rowid}"
            search_text = f"{paper_title} {metadata.get('source', '')} {clean[:1600]}"
            records.append(
                {
                    "rowid": int(rowid),
                    "text": clean,
                    "search_text": search_text,
                    "paper_id": paper_id,
                    "paper_title": paper_title,
                    "source": metadata.get("source"),
                    "embedding_id": embedding_id,
                    "cache_key": _embedding_cache_key(embedding_id, search_text),
                }
            )
        return records

    def _paperwise_record_embeddings(
        self,
        records: list[dict[str, Any]],
        model_name: str,
        base_url: str,
        api_key_env: str,
        dimensions: int | None,
    ) -> dict[str, list[float]]:
        cache_path = self._paperwise_embedding_cache_path(model_name)
        cached = self._read_embedding_cache(cache_path)
        missing = [record for record in records if record["cache_key"] not in cached]
        if missing:
            vectors = self._embed_external_texts(
                [record["search_text"] for record in missing],
                model_name=model_name,
                base_url=base_url,
                api_key_env=api_key_env,
                dimensions=dimensions,
            )
            for record, vector in zip(missing, vectors):
                cached[record["cache_key"]] = vector
            self._write_embedding_cache(cache_path, cached)
        return cached

    def _paperwise_embedding_cache_path(self, model_name: str) -> Path:
        return PROJECT_ROOT / "workspace" / "cache" / f"paperwise_hybrid_embeddings_{_cache_safe_model_name(model_name)}.json"

    def _read_embedding_cache(self, path: Path) -> dict[str, list[float]]:
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        if not isinstance(data, dict) or data.get("schema_version") != "1.0":
            return {}
        vectors = data.get("embeddings")
        if not isinstance(vectors, dict):
            return {}
        return {
            str(key): [float(value) for value in vector]
            for key, vector in vectors.items()
            if isinstance(vector, list)
        }

    def _write_embedding_cache(self, path: Path, embeddings: dict[str, list[float]]) -> None:
        atomic_write_json(
            path,
            {
                "schema_version": "1.0",
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "source": "project_paperwise_hybrid_retrieval",
                "embedding_count": len(embeddings),
                "embeddings": embeddings,
            },
        )

    def _embed_external_texts(
        self,
        texts: list[str],
        model_name: str,
        base_url: str,
        api_key_env: str,
        dimensions: int | None,
    ) -> list[list[float]]:
        api_key = os.getenv(api_key_env)
        if not api_key and api_key_env != "QWEN_API_KEY":
            api_key = os.getenv("QWEN_API_KEY")
        if not api_key:
            raise RuntimeError(f"missing embedding API key: set {api_key_env}")
        from openai import OpenAI

        client = OpenAI(api_key=api_key, base_url=base_url, timeout=60)
        vectors: list[list[float]] = []
        for index in range(0, len(texts), 10):
            batch = texts[index : index + 10]
            payload: dict[str, Any] = {
                "model": model_name,
                "input": batch,
                "encoding_format": "float",
            }
            if dimensions is not None:
                payload["dimensions"] = dimensions
            response = client.embeddings.create(**payload)
            vectors.extend(normalize_vector([float(value) for value in item.embedding]) for item in response.data)
        return vectors

    def _normalize_bm25_score(self, score: float, all_scores: list[float]) -> float:
        max_score = max(all_scores) if all_scores else 0.0
        if max_score <= 0:
            return 0.0
        return float(score) / float(max_score)

    def _paperwise_vector_item_from_record(
        self,
        record: dict[str, Any],
        score: float,
        relevance: dict[str, Any],
        retrieval_backend: str,
        bm25_score: float,
        coarse_sources: list[str],
    ) -> dict[str, Any]:
        paper_id = str(record.get("paper_id") or "")
        paper_title = str(record.get("paper_title") or paper_id or "PaperWise paper")
        text = str(record.get("text") or "")
        return {
            "source": "paperwise_vector_library_project_hybrid",
            "title": paper_title,
            "display_title": self._display_title_from_text(text, fallback=paper_title),
            "original_title": paper_title,
            "path": f"embedding_fulltext_search_content:{record.get('rowid')}",
            "paper_id": paper_id or None,
            "paper_title": paper_title,
            "paper_path": self._report_path_for_kb_entry(paper_id, paper_title),
            "chunk_source": record.get("source"),
            "embedding_id": record.get("embedding_id"),
            "snippet": text[:500],
            "level": "semantic_evidence",
            "status": "available",
            "roles": ["reproduction", "evidence"],
            "score": round(float(score), 6),
            "bm25_score": round(float(bm25_score), 6),
            "coarse_sources": coarse_sources,
            "retrieval_backend": retrieval_backend,
            "relevance": relevance,
        }

    def _dedupe_papers(self, candidates: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
        selected: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in candidates:
            key = str(item.get("paper_id") or item.get("paper_path") or item.get("path"))
            if key in seen:
                continue
            seen.add(key)
            selected.append(item)
            if len(selected) >= limit:
                break
        return selected

    def _vector_query_error(self, exc: Exception) -> str:
        message = str(exc).strip()
        lowered = message.lower()
        if "api key" in lowered or "apikey" in lowered or "unauthorized" in lowered:
            return f"qwen_embedding_api_key_error:{message}"
        if "collection" in lowered:
            return f"paperwise_chroma_query_error:{message}"
        return f"paperwise_kb_store_query_failed:{message}"

    def _report_path_for_kb_entry(self, paper_id: str, title: str) -> str:
        outputs = self.root / "outputs"
        if paper_id:
            matches = sorted(outputs.glob(f"*{paper_id}*/report.md"))
            if matches:
                return str(matches[0])
        title_terms = [term for term in re.findall(r"[A-Za-z0-9]+", str(title or "").lower()) if len(term) >= 4][:4]
        if title_terms:
            for path in outputs.glob("*/report.md"):
                lower = str(path.parent.name).lower()
                if all(term in lower for term in title_terms[:2]):
                    return str(path)
        return "paperwise_kb"

    def _sqlite_count(self, conn: sqlite3.Connection, table: str) -> int:
        try:
            return int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
        except Exception:
            return 0

    def _vector_paper_count(self, conn: sqlite3.Connection) -> int:
        try:
            rows = conn.execute(
                "SELECT DISTINCT embedding_id FROM embeddings WHERE embedding_id IS NOT NULL"
            ).fetchall()
        except Exception:
            return 0
        values = {str(row[0]).split("::", 1)[0] for row in rows if row and row[0]}
        return len(values)

    def _vector_query_items(self, conn: sqlite3.Connection, query: str, limit: int, profile: dict[str, Any]) -> list[dict[str, Any]]:
        terms = self._query_terms(query)
        search_terms = list(dict.fromkeys([*profile.get("search_terms", []), *terms]))
        if not search_terms:
            return []
        clauses = " OR ".join("c0 LIKE ?" for _ in search_terms[:10])
        params = [f"%{term}%" for term in search_terms[:10]]
        try:
            rows = conn.execute(
                f"SELECT rowid, c0 FROM embedding_fulltext_search_content WHERE {clauses} LIMIT ?",
                [*params, max(limit * 8, 80)],
            ).fetchall()
        except Exception:
            return []
        items = []
        for rowid, text in rows:
            clean = " ".join(str(text).split())
            relevance = self._relevance(clean, profile)
            if not relevance["accepted"]:
                continue
            title = clean.lstrip("# ").split("**", 1)[0][:120] or f"PaperWise chunk {rowid}"
            display_title = self._display_title_from_text(clean, fallback=title)
            metadata = self._vector_metadata_for_embedding(conn, int(rowid))
            paper_id = metadata.get("paper_id") or metadata.get("arxiv_id") or metadata.get("embedding_id", "").split("::", 1)[0]
            paper_title = metadata.get("title") or self._display_title_from_text(clean, fallback=title)
            paper_path = self._report_path_for_kb_entry(paper_id, paper_title)
            items.append(
                {
                    "source": "vector_library",
                    "title": title,
                    "display_title": display_title,
                    "original_title": title,
                    "path": f"embedding_fulltext_search_content:{rowid}",
                    "paper_id": paper_id or None,
                    "paper_title": paper_title,
                    "paper_path": paper_path,
                    "chunk_source": metadata.get("source"),
                    "embedding_id": metadata.get("embedding_id"),
                    "snippet": clean[:500],
                    "level": "semantic_evidence",
                    "status": "available",
                    "roles": ["reproduction", "evidence"],
                    "score": relevance["score"],
                    "relevance": relevance,
                }
            )
        return self._tiered_select(items, limit)

    def _vector_metadata_for_embedding(self, conn: sqlite3.Connection, embedding_row_id: int) -> dict[str, str]:
        metadata: dict[str, str] = {}
        try:
            rows = conn.execute(
                "SELECT key, string_value FROM embedding_metadata "
                "WHERE id = ? AND key IN ('paper_id', 'arxiv_id', 'title', 'source', 'path')",
                (embedding_row_id,),
            ).fetchall()
            embedding = conn.execute(
                "SELECT embedding_id FROM embeddings WHERE id = ?",
                (embedding_row_id,),
            ).fetchone()
        except Exception:
            return metadata
        for key, value in rows:
            if key and value:
                metadata[str(key)] = str(value)
        if embedding and embedding[0]:
            metadata["embedding_id"] = str(embedding[0])
        return metadata
