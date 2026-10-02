"""离线跑一轮「用户问题 -> query 拓展 -> 向量化 -> 检索」链路。

不连 Redis、不连数据库：MemoryManager(store=None) 全内存，Blackboard/AuditLog 落临时目录。
用法：
    python work/query_expansion_pipeline_check.py
    python work/query_expansion_pipeline_check.py --question "怎么优化贴片天线的S11？" --variants 5 --no-llm
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_runtime.audit import AuditLog  # noqa: E402
from agent_runtime.blackboard import Blackboard  # noqa: E402
from agent_runtime.capability_registry import CapabilityRegistry  # noqa: E402
from agent_runtime.config import Settings  # noqa: E402
from agent_runtime.memory import MemoryManager  # noqa: E402
from agent_runtime.query_expansion import QueryExpander  # noqa: E402
from agent_runtime.runtime_llm_config import runtime_llm_config  # noqa: E402
from agent_runtime.scheduler import Scheduler  # noqa: E402
from agent_runtime.streaming import StreamPublisher  # noqa: E402


class FakeExpansionLLM:
    """模拟外部 LLM：契约与主 agent 的 LLMClient.generate_json 一致，返回字典。"""

    enabled = True

    def __init__(self, rewrites: list[str]):
        self.rewrites = rewrites
        self.calls: list[dict] = []

    def generate_json(self, *, system_prompt: str, payload: dict, runtime_config=None) -> dict:
        self.calls.append(payload)
        query = str(payload.get("query") or "")
        wanted = int(payload.get("max_variants") or len(self.rewrites))
        return {"queries": self.rewrites[:wanted], "source_query": query}


DEFAULT_REWRITES = [
    "贴片天线回波损耗优化方法",
    "微带天线 S11 反射系数如何降低",
    "用灰狼优化 GWO 优化贴片天线参数",
    "patch antenna return loss optimization",
    "贴片天线 S11 参数优化仿真流程",
]


def seed_memory(memory: MemoryManager) -> None:
    """写入内存态 L2/L3 记忆，作为检索目标。"""
    memory.store_l2(
        "project",
        {"statement": "贴片天线 S11 优化使用 GWO 灰狼优化算法", "confidence": 0.9},
        provenance={"source_ref": "seed/project"},
    )
    memory.store_l2(
        "user",
        {"statement": "关注回波损耗与轴比带宽指标", "confidence": 0.8},
        provenance={"source_ref": "seed/user"},
    )
    stored = memory.maybe_store_l3_workflow(
        {
            "task_status": "completed",
            "unresolved_failures": 0,
            "workflow_quality": 0.9,
            "graph_step": 3,
            "domain": "antenna",
            "goal_pattern": "s11-optimize",
            "task_goal": "回波损耗 优化 贴片天线",
            "steps": ["几何建模", "仿真预检", "GWO 参数优化", "结果审查"],
        }
    )
    if not stored:
        raise RuntimeError("L3 种子记忆未写入，检查 maybe_store_l3_workflow 门槛")


def build_scheduler(root: str, memory: MemoryManager, use_llm: bool, max_variants: int, rewrites: list[str]) -> tuple[Scheduler, FakeExpansionLLM]:
    runtime_llm_config.enabled = False
    runtime_llm_config.base_url = ""
    runtime_llm_config.api_key = ""
    runtime_llm_config.model_name = ""
    scheduler = Scheduler(
        Settings(
            workspace_root=root,
            logs_root=root,
            llm_enabled=False,
            l2_min_relevance_score=0.0,
            l3_min_relevance_score=0.0,
            query_expansion_enabled=True,
            query_expansion_max_variants=max_variants,
            query_expansion_use_llm=use_llm,
        ),
        Blackboard(root),
        memory,
        CapabilityRegistry(),
        AuditLog(root),
        StreamPublisher(),
    )
    fake_llm = FakeExpansionLLM(rewrites)
    scheduler.query_expander = QueryExpander(
        enabled=True,
        max_variants=max_variants,
        use_llm=use_llm,
        llm_client=fake_llm,
        runtime_llm_config=runtime_llm_config,
    )
    return scheduler, fake_llm


def print_hits(title: str, hits: list[dict]) -> None:
    print(f"  {title}: {len(hits)} 条")
    for hit in hits:
        pipeline = hit.get("retrieval_pipeline") or {}
        print(
            f"    - {hit.get('memory_id')} score={hit.get('retrieval_score')} "
            f"bm25={round(float(hit.get('bm25_score') or 0.0), 4)} "
            f"variants={pipeline.get('query_variant_count')} sources={hit.get('coarse_sources')}"
        )


def run_round(label: str, question: str, use_llm: bool, max_variants: int, rewrites: list[str]) -> None:
    print(f"\n===== {label} =====")
    with tempfile.TemporaryDirectory() as root:
        memory = MemoryManager()
        seed_memory(memory)
        scheduler, fake_llm = build_scheduler(root, memory, use_llm, max_variants, rewrites)

        state = scheduler.create_task_from_request(question)
        metadata = state["task_metadata"]

        expansion = metadata.get("query_expansion") or {}
        print(f"strategy        : {expansion.get('strategy')}")
        print(f"request/actual  : {expansion.get('requested_variants')} / {expansion.get('variant_count')}")
        print(f"llm 调用次数    : {len(fake_llm.calls)}")
        for index, variant in enumerate(expansion.get("variants") or []):
            print(f"  [{index}] {variant}")

        context = metadata.get("retrieval_context") or {}
        print(f"query_hash      : {context.get('query_hash')}")
        print_hits("L2 命中", context.get("l2") or [])
        print_hits("L3 命中", context.get("l3") or [])
        learning = metadata.get("validated_learning_context") or {}
        print(f"  学习召回       : policy={learning.get('policy')} knowledge={len(learning.get('knowledge') or [])}")

        baseline = scheduler.retrieval.retrieve_l2(question)
        print(f"  单变体基线     : {len(baseline)} 条, top1 score={baseline[0]['retrieval_score'] if baseline else None}")
        print("  各变体单独检索 :")
        for variant in expansion.get("variants") or []:
            hits = scheduler.retrieval.retrieve_l2(variant)
            top = hits[0]["retrieval_score"] if hits else None
            print(f"    {round(float(top), 6) if top is not None else None} <- {variant}")
        print(f"  任务状态       : {state['state']}")


def main() -> int:
    parser = argparse.ArgumentParser(description="离线验证 query 拓展检索链路")
    parser.add_argument("--question", default="怎么优化贴片天线的S11？")
    parser.add_argument("--variants", type=int, default=5, help="向 LLM 索取的相似 query 数量")
    parser.add_argument("--no-llm", action="store_true", help="关闭 LLM，只看本地兜底拓展")
    parser.add_argument("--rewrites", default="", help="逗号分隔，覆盖模拟 LLM 返回的相似 query")
    args = parser.parse_args()

    rewrites = [item.strip() for item in args.rewrites.split(",") if item.strip()] or DEFAULT_REWRITES
    print(json.dumps({"question": args.question, "variants": args.variants}, ensure_ascii=False))

    run_round("轮次 1：外部 LLM 拓展", args.question, use_llm=not args.no_llm, max_variants=args.variants, rewrites=rewrites)
    if not args.no_llm:
        run_round("轮次 2：LLM 不可用（本地同义词兜底）", args.question, use_llm=False, max_variants=args.variants, rewrites=rewrites)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
