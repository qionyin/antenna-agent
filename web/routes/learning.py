from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from agent_learning import ClusterConfig
from agent_runtime.utils import stable_hash


def create_learning_router(get_scheduler: Callable[[], Any]) -> APIRouter:
    router = APIRouter()

    def domain_knowledge_disabled():
        if get_scheduler().learning.domain_knowledge_enabled:
            return None
        return JSONResponse({"error": "domain_knowledge_disabled"}, status_code=403)

    @router.get("/learning")
    def learning_snapshot():
        """Return V2.3 source, evidence, knowledge, experience, cluster, and innovation counts."""
        return get_scheduler().learning.snapshot()

    @router.post("/learning/evolution/diagnose")
    def diagnose_learning_evaluation(payload: dict):
        try:
            return get_scheduler().learning.diagnose_evaluation(dict(payload))
        except (TypeError, ValueError, RuntimeError) as exc:
            return JSONResponse({"error": "validation_error", "message": str(exc)}, status_code=400)

    @router.post("/learning/evolution/evaluate-extraction")
    def evaluate_learning_extraction(payload: dict):
        try:
            return get_scheduler().learning.evaluate_extraction(list(payload.get("cases") or []))
        except (TypeError, ValueError, RuntimeError) as exc:
            return JSONResponse({"error": "validation_error", "message": str(exc)}, status_code=400)

    @router.post("/learning/evolution/run-extraction")
    def run_learning_extraction_evolution(payload: dict):
        try:
            return get_scheduler().learning.run_extraction_evolution(
                list(payload.get("cases") or []),
                verification_cases=list(payload.get("verification_cases") or []),
                auto_apply_low_risk=bool(payload.get("auto_apply_low_risk", True)),
                use_external_llm=bool(payload.get("use_external_llm", False)),
            )
        except (TypeError, ValueError, RuntimeError) as exc:
            return JSONResponse({"error": "evolution_error", "message": str(exc)}, status_code=400)

    @router.get("/learning/evolution/proposals")
    def list_learning_evolution_proposals(limit: int = 100):
        return get_scheduler().memory.list_evolution_proposals(limit=max(1, min(limit, 1000)))

    @router.post("/learning/evolution/propose")
    def propose_learning_evolution(payload: dict):
        try:
            return get_scheduler().learning.propose_evolution(
                dict(payload.get("run") or payload),
                current_config_hash=payload.get("current_config_hash"),
            )
        except (TypeError, ValueError, RuntimeError) as exc:
            return JSONResponse({"error": "validation_error", "message": str(exc)}, status_code=400)

    @router.post("/learning/evolution/verify")
    def verify_learning_evolution(payload: dict):
        try:
            return get_scheduler().learning.verify_evolution(
                dict(payload.get("before") or {}),
                dict(payload.get("after") or {}),
            )
        except (TypeError, ValueError, RuntimeError) as exc:
            return JSONResponse({"error": "validation_error", "message": str(exc)}, status_code=400)

    @router.post("/learning/sources")
    def store_learning_source(payload: dict):
        """Store raw source text outside L2 for later evidence compilation."""
        if response := domain_knowledge_disabled():
            return response
        try:
            source_id = get_scheduler().memory.store_long_term_source(
                str(payload.get("source_id") or ""),
                str(payload.get("content") or ""),
                metadata=dict(payload.get("metadata") or {}),
            )
            return get_scheduler().memory.get_long_term_source(source_id)
        except (TypeError, ValueError) as exc:
            return JSONResponse({"error": "validation_error", "message": str(exc)}, status_code=400)

    @router.post("/learning/paperwise/ingest")
    def ingest_paperwise_report(payload: dict):
        """Copy a read-only PaperWise report into the raw source pool, optionally compile it."""
        if response := domain_knowledge_disabled():
            return response
        report_path = str(payload.get("report_path") or "").strip()
        if not report_path:
            return JSONResponse({"error": "validation_error", "message": "report_path is required"}, status_code=400)
        report = get_scheduler().paperwise.read_report(report_path, max_chars=2_000_000)
        if not report.get("available"):
            return JSONResponse({"error": "not_found", "message": report.get("error") or "PaperWise report unavailable"}, status_code=404)
        source_path = Path(str(report["path"]))
        try:
            content = source_path.read_text(encoding="utf-8", errors="ignore")
        except OSError as exc:
            return JSONResponse({"error": "source_read_error", "message": str(exc)}, status_code=400)
        source_id = str(payload.get("source_id") or f"paperwise-{stable_hash(str(source_path.resolve()))[:20]}")
        source = get_scheduler().memory.store_long_term_source(
            source_id,
            content,
            metadata={
                "source_type": "paperwise_report",
                "source_ref": str(source_path),
                "locator": "report.md",
                "paper_id": payload.get("paper_id"),
                "display_title": report.get("display_title") or report.get("title"),
                "evidence_type": "paper_fact",
            },
        )
        compiled = get_scheduler().learning.compile_source(source_id) if bool(payload.get("compile", True)) else None
        return {"source": get_scheduler().memory.get_long_term_source(source_id), "compiled": compiled}

    @router.post("/learning/sources/{source_id}/compile")
    def compile_learning_source(source_id: str):
        """Compile one raw source into traceable evidence and candidate domain knowledge."""
        if response := domain_knowledge_disabled():
            return response
        try:
            return get_scheduler().learning.compile_source(source_id)
        except KeyError as exc:
            return JSONResponse({"error": "not_found", "message": str(exc)}, status_code=404)
        except ValueError as exc:
            return JSONResponse({"error": "validation_error", "message": str(exc)}, status_code=400)

    @router.get("/learning/knowledge")
    def list_learning_knowledge(status: str | None = None, limit: int = 100):
        if response := domain_knowledge_disabled():
            return response
        statuses = {item.strip() for item in status.split(",") if item.strip()} if status else None
        return get_scheduler().memory.list_domain_knowledge(statuses=statuses, limit=max(1, min(limit, 1000)))

    @router.post("/learning/knowledge/{knowledge_id}/review")
    def review_learning_knowledge(knowledge_id: str):
        if response := domain_knowledge_disabled():
            return response
        try:
            return get_scheduler().learning.review_knowledge(knowledge_id)
        except KeyError as exc:
            return JSONResponse({"error": "not_found", "message": str(exc)}, status_code=404)

    @router.post("/learning/knowledge/{knowledge_id}/promote")
    def promote_learning_knowledge(knowledge_id: str, payload: dict):
        if response := domain_knowledge_disabled():
            return response
        try:
            return get_scheduler().learning.promote_knowledge(
                knowledge_id,
                authority=str(payload.get("authority") or ""),
                evidence_refs=list(payload.get("evidence_refs") or []),
            )
        except KeyError as exc:
            return JSONResponse({"error": "not_found", "message": str(exc)}, status_code=404)
        except ValueError as exc:
            return JSONResponse({"error": "validation_error", "message": str(exc)}, status_code=400)

    @router.post("/learning/clusters")
    def cluster_learning_candidates(payload: dict):
        if response := domain_knowledge_disabled():
            return response
        try:
            config = ClusterConfig(
                method=str(payload.get("method") or "natural"),
                merge_threshold=float(payload.get("merge_threshold", 0.82)),
                split_threshold=float(payload.get("split_threshold", 0.55)),
                temperature=float(payload.get("temperature", 0.0)),
                cooling_rate=float(payload.get("cooling_rate", 0.9)),
                iteration_limit=int(payload.get("iteration_limit", 3)),
                random_seed=int(payload.get("random_seed", 0)),
            )
            return get_scheduler().learning.cluster_candidates(config)
        except (TypeError, ValueError) as exc:
            return JSONResponse({"error": "validation_error", "message": str(exc)}, status_code=400)

    @router.get("/learning/experiences")
    def list_learning_experiences(status: str | None = None, limit: int = 100):
        statuses = {item.strip() for item in status.split(",") if item.strip()} if status else None
        return get_scheduler().memory.list_experiences(statuses=statuses, limit=max(1, min(limit, 1000)))

    @router.post("/learning/experiences/{experience_id}/feedback")
    def record_learning_feedback(experience_id: str, payload: dict):
        try:
            return get_scheduler().learning.record_experience_feedback(
                experience_id,
                task_id=str(payload.get("task_id") or ""),
                outcome=str(payload.get("outcome") or ""),
                evidence_refs=list(payload.get("evidence_refs") or []),
            )
        except KeyError as exc:
            return JSONResponse({"error": "not_found", "message": str(exc)}, status_code=404)
        except ValueError as exc:
            return JSONResponse({"error": "validation_error", "message": str(exc)}, status_code=400)

    @router.post("/learning/experiences/{experience_id}/replay")
    def replay_learning_experience(experience_id: str, payload: dict):
        try:
            return get_scheduler().learning.validate_experience_replay(
                experience_id,
                list(payload.get("cases") or []),
            )
        except KeyError as exc:
            return JSONResponse({"error": "not_found", "message": str(exc)}, status_code=404)
        except ValueError as exc:
            return JSONResponse({"error": "validation_error", "message": str(exc)}, status_code=400)

    @router.post("/learning/experiences/{experience_id}/promote")
    def promote_learning_experience(experience_id: str, payload: dict):
        try:
            return get_scheduler().learning.promote_experience(
                experience_id,
                authority=str(payload.get("authority") or ""),
                evidence_refs=list(payload.get("evidence_refs") or []),
            )
        except KeyError as exc:
            return JSONResponse({"error": "not_found", "message": str(exc)}, status_code=404)
        except ValueError as exc:
            return JSONResponse({"error": "validation_error", "message": str(exc)}, status_code=400)

    @router.post("/learning/innovations/evaluate")
    def evaluate_learning_innovation(payload: dict):
        if response := domain_knowledge_disabled():
            return response
        return get_scheduler().learning.evaluate_innovation(dict(payload))

    @router.post("/learning/wiki/rebuild")
    def rebuild_learning_wiki():
        if response := domain_knowledge_disabled():
            return response
        return get_scheduler().learning.rebuild_wiki()

    @router.get("/learning/graph")
    def get_learning_graph():
        if response := domain_knowledge_disabled():
            return response
        return get_scheduler().paperwise.graph_library_summary(query="", limit=20)

    @router.get("/learning/wiki")
    def list_learning_wiki(limit: int = 100):
        if response := domain_knowledge_disabled():
            return response
        return get_scheduler().memory.list_wiki_pages(limit=max(1, min(limit, 1000)))

    @router.get("/learning/wiki/{page_id}")
    def get_learning_wiki_page(page_id: str):
        if response := domain_knowledge_disabled():
            return response
        page = get_scheduler().memory.get_wiki_page(page_id)
        if page is None:
            return JSONResponse({"error": "not_found", "message": f"wiki page not found: {page_id}"}, status_code=404)
        return page

    @router.get("/tasks/{task_id}/learning")
    def task_learning_state(task_id: str):
        record = get_scheduler().blackboard.get(task_id)
        metadata = record.task_metadata
        episode_id = metadata.get("learning_episode_id")
        return {
            "task_id": task_id,
            "validated_learning_context": metadata.get("validated_learning_context") or {},
            "learning_episode_id": episode_id,
            "episode": get_scheduler().memory.get_episode(str(episode_id)) if episode_id else None,
            "experience_candidate_id": metadata.get("experience_candidate_id"),
        }
    return router
