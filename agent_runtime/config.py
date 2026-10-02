from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml
except Exception:  # pragma: no cover - fallback for minimal environments
    yaml = None


@dataclass
class Settings:
    redis_url: str = "redis://localhost:6379/0"
    redis_json_retention_days: int = 360
    redis_stream_retention_days: int = 360
    redis_vector_retention_days: int = 360
    workspace_root: str = r"D:\pythoncode\antenna_agent_lab\workspace"
    logs_root: str = r"D:\pythoncode\antenna_agent_lab\logs"
    antenna_skills_root: str = r"C:\Users\30626\.codex\skills\Antenna Skills"
    e_platform_root: str = r"E:\antenna skills"
    e_results_root: str = r"E:\antenna skills-resault"
    paperwise_root: str = r"C:\Users\30626\.codex\skills\paperwise-main"
    ieee_harvester_root: str = r"C:\Users\30626\.codex\skills\ieee-xplore-harvester"
    allowed_tool_paths: list[str] = field(default_factory=list)
    l1_recent_turns: int = 20
    l2_final_top_k: int = 5
    l3_workflow_top_k: int = 5
    l2_min_relevance_score: float = 0.51
    l3_min_relevance_score: float = 0.51
    query_expansion_enabled: bool = True
    query_expansion_max_variants: int = 5
    query_expansion_use_llm: bool = True
    agent_timeout_seconds: int = 60
    max_node_retries: int = 3
    max_graph_steps: int = 50
    max_auto_fix_rounds: int = 3
    approval_ttl_hours: int = 24
    approval_abandoned_after_timeouts: int = 3
    llm_base_url: str = "https://api-cn.smallice.xyz/v1"
    llm_enabled: bool = False
    llm_api_key_env: str = "OPENAI_API_KEY"
    llm_model_name: str = "gpt-5.5"
    embedding_model_name: str = "text-embedding-v3"
    embedding_provider: str = "local"
    embedding_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    embedding_api_key_env: str = "API_KEY"
    embedding_dimensions: int | None = 1024
    embedding_timeout_seconds: int = 30
    learning_enabled: bool = True
    learning_evolution_enabled: bool = False
    learning_domain_knowledge_enabled: bool = True
    learning_l2_project_facts: list[dict[str, Any]] = field(default_factory=list)


def _load_yaml(path: Path) -> dict[str, Any]:
    """读取 YAML 配置文件并返回字典。"""
    if not path.exists() or yaml is None:
        return {}
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def load_settings(config_path: str | Path = "config.yaml") -> Settings:
    """按默认值和配置文件加载运行设置。"""
    data = _load_yaml(Path(config_path))
    redis_cfg = data.get("redis", {})
    paths = data.get("paths", {})
    runtime = data.get("runtime", {})
    retrieval = data.get("retrieval", {})
    llm = data.get("llm", {})
    embedding = data.get("embedding", {})
    learning = data.get("learning", {})
    dimensions = os.getenv("EMBEDDING_DIMENSIONS", embedding.get("dimensions", Settings.embedding_dimensions))

    settings = Settings(
        redis_url=os.getenv("REDIS_URL", redis_cfg.get("url", Settings.redis_url)),
        redis_json_retention_days=int(os.getenv("REDIS_JSON_RETENTION_DAYS", redis_cfg.get("json_retention_days", Settings.redis_json_retention_days))),
        redis_stream_retention_days=int(os.getenv("REDIS_STREAM_RETENTION_DAYS", redis_cfg.get("stream_retention_days", Settings.redis_stream_retention_days))),
        redis_vector_retention_days=int(os.getenv("REDIS_VECTOR_RETENTION_DAYS", redis_cfg.get("vector_retention_days", Settings.redis_vector_retention_days))),
        workspace_root=os.getenv("WORKSPACE_ROOT", paths.get("workspace_root", Settings.workspace_root)),
        logs_root=os.getenv("LOGS_ROOT", paths.get("logs_root", Settings.logs_root)),
        antenna_skills_root=os.getenv("ANTENNA_SKILLS_ROOT", paths.get("antenna_skills_root", Settings.antenna_skills_root)),
        e_platform_root=os.getenv("E_PLATFORM_ROOT", paths.get("e_platform_root", Settings.e_platform_root)),
        e_results_root=os.getenv("E_RESULTS_ROOT", paths.get("e_results_root", Settings.e_results_root)),
        paperwise_root=os.getenv("PAPERWISE_ROOT", paths.get("paperwise_root", Settings.paperwise_root)),
        ieee_harvester_root=os.getenv("IEEE_HARVESTER_ROOT", paths.get("ieee_harvester_root", Settings.ieee_harvester_root)),
        allowed_tool_paths=paths.get("allowed_tool_paths", []),
        l1_recent_turns=int(os.getenv("L1_RECENT_TURNS", redis_cfg.get("l1_recent_turns", 20))),
        l2_final_top_k=int(retrieval.get("l2_final_top_k", 5)),
        l3_workflow_top_k=int(retrieval.get("l3_workflow_top_k", 3)),
        l2_min_relevance_score=float(os.getenv("L2_MIN_RELEVANCE_SCORE", retrieval.get("l2_min_relevance_score", Settings.l2_min_relevance_score))),
        l3_min_relevance_score=float(os.getenv("L3_MIN_RELEVANCE_SCORE", retrieval.get("l3_min_relevance_score", Settings.l3_min_relevance_score))),
        query_expansion_enabled=str(os.getenv("QUERY_EXPANSION_ENABLED", (retrieval.get("query_expansion") or {}).get("enabled", Settings.query_expansion_enabled))).lower() in {"1", "true", "yes", "on"},
        query_expansion_max_variants=int(os.getenv("QUERY_EXPANSION_MAX_VARIANTS", (retrieval.get("query_expansion") or {}).get("max_variants", Settings.query_expansion_max_variants))),
        query_expansion_use_llm=str(os.getenv("QUERY_EXPANSION_USE_LLM", (retrieval.get("query_expansion") or {}).get("use_llm", Settings.query_expansion_use_llm))).lower() in {"1", "true", "yes", "on"},
        agent_timeout_seconds=int(runtime.get("agent_timeout_seconds", 60)),
        max_node_retries=int(runtime.get("max_node_retries", 3)),
        max_graph_steps=int(runtime.get("max_graph_steps", 50)),
        max_auto_fix_rounds=int(runtime.get("max_auto_fix_rounds", 3)),
        approval_ttl_hours=int(runtime.get("approval_ttl_hours", 24)),
        approval_abandoned_after_timeouts=int(runtime.get("approval_abandoned_after_timeouts", 3)),
        llm_base_url=os.getenv("LLM_BASE_URL", llm.get("base_url", Settings.llm_base_url)),
        llm_enabled=str(os.getenv("LLM_ENABLED", llm.get("enabled", False))).lower() in {"1", "true", "yes", "on"},
        llm_api_key_env=llm.get("api_key_env", "OPENAI_API_KEY"),
        llm_model_name=os.getenv("LLM_MODEL_NAME", llm.get("model_name", Settings.llm_model_name)),
        embedding_model_name=os.getenv("EMBEDDING_MODEL_NAME", embedding.get("model_name", llm.get("embedding_model_name", Settings.embedding_model_name))),
        embedding_provider=os.getenv("EMBEDDING_PROVIDER", embedding.get("provider", Settings.embedding_provider)),
        embedding_base_url=os.getenv("EMBEDDING_BASE_URL", embedding.get("base_url", Settings.embedding_base_url)),
        embedding_api_key_env=os.getenv("EMBEDDING_API_KEY_ENV", embedding.get("api_key_env", Settings.embedding_api_key_env)),
        embedding_dimensions=None if dimensions in {None, "", "null", "none"} else int(dimensions),
    embedding_timeout_seconds=int(os.getenv("EMBEDDING_TIMEOUT_SECONDS", embedding.get("timeout_seconds", Settings.embedding_timeout_seconds))),
        learning_enabled=str(os.getenv("LEARNING_MODULE_ENABLED", learning.get("enabled", Settings.learning_enabled))).lower() in {"1", "true", "yes", "on"},
        learning_evolution_enabled=str(os.getenv("LEARNING_EVOLUTION_ENABLED", learning.get("evolution_enabled", Settings.learning_evolution_enabled))).lower() in {"1", "true", "yes", "on"},
        learning_domain_knowledge_enabled=str(os.getenv("LEARNING_DOMAIN_KNOWLEDGE_ENABLED", learning.get("domain_knowledge_enabled", Settings.learning_domain_knowledge_enabled))).lower() in {"1", "true", "yes", "on"},
        learning_l2_project_facts=list(learning.get("l2_project_facts") or []),
    )
    if not settings.allowed_tool_paths:
        settings.allowed_tool_paths = [
            settings.antenna_skills_root,
            settings.e_platform_root,
            settings.e_results_root,
            settings.paperwise_root,
            settings.ieee_harvester_root,
        ]
    return settings
