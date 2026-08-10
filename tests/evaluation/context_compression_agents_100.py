from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import os
import re
import statistics
import sys
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_runtime.config import load_settings
from agent_runtime.redis_store import RedisStore
from agent_runtime.utils import atomic_write_json, stable_hash


TASK_ROOT = ROOT / "workspace" / "tasks"
AGENT_ROLES = ("task_subagent", "module_reviewer", "global_reviewer", "central_agent")
VARIANTS = ("baseline", "compressed")
_THREAD_LOCAL = threading.local()


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return str(value)


def _compact_record(record: dict[str, Any]) -> dict[str, Any]:
    output = record.get("output") if isinstance(record.get("output"), dict) else {}
    return {
        "step_id": record.get("step_id"),
        "agent_role": record.get("agent_role"),
        "decision": record.get("decision"),
        "status": record.get("status"),
        "output_status": output.get("status"),
        "output_names": record.get("output_refs") or output.get("outputs") or [],
        "required_skills": output.get("required_skills") or [],
        "evidence_level": output.get("evidence_level"),
        "blocking_findings": output.get("blocking_findings") or [],
        "reroute_action": (output.get("reroute_suggestion") or {}).get("action"),
    }


def _compact_step(step: dict[str, Any]) -> dict[str, Any]:
    skill_context = step.get("step_skill_context") or {}
    return {
        "step_id": step.get("step_id"),
        "step_goal": step.get("step_goal"),
        "inputs": step.get("inputs") or [],
        "outputs": step.get("outputs") or [],
        "required_skills": step.get("required_skills") or [],
        "gate_condition": step.get("gate_condition"),
        "execution_condition": step.get("execution_condition"),
        "status": step.get("status"),
        "callable_skills": [
            item.get("owner_skill") for item in skill_context.get("callable_skills") or step.get("callable_skills") or []
            if isinstance(item, dict)
        ],
        "candidate_skills": [
            item.get("owner_skill") for item in skill_context.get("candidate_skills") or step.get("candidate_skills") or []
            if isinstance(item, dict)
        ],
    }


def _evaluation_scenario(index: int) -> dict[str, Any]:
    slot = index % 20
    if slot < 12:
        return {
            "name": "pass",
            "task_decision": "pass",
            "artifact_validation": "valid",
            "module_decision": "pass",
            "global_decision": "pass",
            "evidence_level": "B",
            "reroute_action": "none",
            "step_status": "passed",
            "central_action": "pass_next_step",
        }
    if slot < 15:
        return {
            "name": "revise",
            "task_decision": "pass",
            "artifact_validation": "missing_fields",
            "module_decision": "revise",
            "global_decision": "revise",
            "evidence_level": "C",
            "reroute_action": "none",
            "step_status": "revise",
            "central_action": "revise_current_step",
        }
    if slot < 17:
        return {
            "name": "block",
            "task_decision": "block",
            "artifact_validation": "unsafe",
            "module_decision": "block",
            "global_decision": "block",
            "evidence_level": "D",
            "reroute_action": "none",
            "step_status": "blocked",
            "central_action": "block_task",
        }
    if slot < 19:
        return {
            "name": "reroute",
            "task_decision": "pass",
            "artifact_validation": "valid",
            "module_decision": "pass",
            "global_decision": "reroute",
            "evidence_level": "C",
            "reroute_action": "refresh_route",
            "step_status": "reroute",
            "central_action": "refresh_skill_route",
        }
    return {
        "name": "wait_user",
        "task_decision": "pass",
        "artifact_validation": "needs_user",
        "module_decision": "wait_user",
        "global_decision": "wait_user",
        "evidence_level": "C",
        "reroute_action": "none",
        "step_status": "waiting_approval",
        "central_action": "wait_user",
    }


def _fixture_records(step: dict[str, Any], scenario: dict[str, Any]) -> dict[str, dict[str, Any]]:
    outputs = sorted(str(item) for item in step.get("outputs") or [])
    required_skills = sorted(str(item) for item in step.get("required_skills") or [])
    task_output = {
        "step_id": step.get("step_id"),
        "agent_role": "task",
        "decision": scenario["task_decision"],
        "status": scenario["task_decision"],
        "output_status": "produced" if scenario["task_decision"] == "pass" else "not_usable",
        "output_names": outputs,
        "required_skills": required_skills,
        "artifact_validation": scenario["artifact_validation"],
    }
    module_review = {
        "step_id": step.get("step_id"),
        "agent_role": "module_review",
        "decision": scenario["module_decision"],
        "status": scenario["module_decision"],
        "blocking_findings": [scenario["artifact_validation"]] if scenario["module_decision"] in {"revise", "block"} else [],
        "evidence_level": scenario["evidence_level"],
        "dependency_status": {
            "reroute": "missing_skill_route",
            "wait_user": "requires_user_approval",
            "block": "unsafe_dependency",
            "revise": "artifact_revision_required",
        }.get(scenario["global_decision"], "satisfied"),
        "suggested_reroute_action": scenario["reroute_action"],
    }
    global_review = {
        "step_id": step.get("step_id"),
        "agent_role": "global_review",
        "decision": scenario["global_decision"],
        "status": scenario["global_decision"],
        "evidence_level": scenario["evidence_level"],
        "reroute_action": scenario["reroute_action"],
    }
    return {"task": task_output, "module_review": module_review, "global_review": global_review}


def _fixture_gold(step: dict[str, Any], scenario: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        "task_subagent": {
            "decision": scenario["task_decision"],
            "outputs": sorted(str(item) for item in step.get("outputs") or []),
            "required_skills": sorted(str(item) for item in step.get("required_skills") or []),
        },
        "module_reviewer": {
            "decision": scenario["module_decision"],
            "has_blockers": scenario["module_decision"] in {"revise", "block"},
        },
        "global_reviewer": {
            "decision": scenario["global_decision"],
            "evidence_level": scenario["evidence_level"],
            "reroute_action": scenario["reroute_action"],
        },
        "central_agent": {"action": scenario["central_action"]},
    }


def _turns(metadata: dict[str, Any]) -> list[dict[str, str]]:
    turns: list[dict[str, str]] = []
    for item in metadata.get("central_agent_messages") or []:
        message = str(item.get("message") or "").strip()
        if message:
            turns.append({"role": "user", "content": message})
    for item in metadata.get("central_agent_replies") or []:
        message = str(item.get("reply_text") or "").strip()
        if message:
            turns.append({"role": "assistant", "content": message})
    return turns


def _memory_records(store: RedisStore, pattern: str) -> list[dict[str, Any]]:
    records = []
    for key in store.iter_keys(pattern):
        value = store.get_json(key)
        if isinstance(value, dict) and str(value.get("status") or "active") == "active":
            records.append(value)
    return records


def _memory_text(record: dict[str, Any]) -> str:
    fact = record.get("fact")
    return " ".join(str(item) for item in (record.get("namespace"), fact, record.get("goal_pattern"), record.get("domain")) if item)


def _tokens(text: str) -> set[str]:
    lowered = str(text or "").lower()
    parts = re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]", lowered)
    return set(parts)


def _relevant(records: list[dict[str, Any]], query: str, top_k: int) -> list[dict[str, Any]]:
    query_tokens = _tokens(query)
    ranked = []
    for record in records:
        text_tokens = _tokens(_memory_text(record))
        score = len(query_tokens & text_tokens) / max(1, len(query_tokens))
        ranked.append((score, stable_hash(record), record))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return [
        {
            "memory_id": record.get("memory_id") or record.get("workflow_key") or key,
            "namespace": record.get("namespace"),
            "fact": record.get("fact"),
            "domain": record.get("domain"),
            "goal_pattern": record.get("goal_pattern"),
            "workflow_quality": record.get("workflow_quality"),
            "relevance_overlap": round(score, 4),
        }
        for score, key, record in ranked[:top_k]
    ]


def _select_step(metadata: dict[str, Any], seed: str) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    plan = metadata.get("dynamic_plan") or {}
    steps = [item for item in plan.get("steps") or [] if isinstance(item, dict)]
    records_by_role: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for record in metadata.get("subagent_records") or []:
        if isinstance(record, dict) and record.get("step_id") and record.get("agent_role"):
            records_by_role[str(record["agent_role"])][str(record["step_id"])] = record
    executable = [step for step in steps if str(step.get("step_id")) in records_by_role.get("task", {})]
    candidates = executable or steps
    if not candidates:
        candidates = [{
            "step_id": "step_001_intake",
            "step_goal": "Understand and classify the user request",
            "inputs": ["user_goal"],
            "outputs": ["task_summary"],
            "required_skills": [],
            "status": "pending",
        }]
    index = int(stable_hash(seed)[:8], 16) % len(candidates)
    step = candidates[index]
    step_id = str(step.get("step_id"))
    return step, {role: items.get(step_id, {}) for role, items in records_by_role.items()}


def _central_action(module_decision: str, global_decision: str, reroute_action: str) -> str:
    if module_decision in {"block", "blocked"} or global_decision in {"block", "blocked"}:
        return "block_task"
    if reroute_action == "refresh_route":
        return "refresh_skill_route"
    if global_decision == "reroute":
        return "reroute_plan"
    if global_decision == "wait_user":
        return "wait_user"
    if module_decision == "revise" or global_decision == "revise":
        return "revise_current_step"
    return "pass_next_step"


def _gold(metadata: dict[str, Any], step: dict[str, Any], records: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    task = records.get("task") or {}
    module = records.get("module_review") or {}
    global_review = records.get("global_review") or {}
    module_output = module.get("output") if isinstance(module.get("output"), dict) else {}
    global_output = global_review.get("output") if isinstance(global_review.get("output"), dict) else {}
    module_decision = str(module.get("decision") or module_output.get("decision") or "pass")
    global_decision = str(global_review.get("decision") or global_output.get("decision") or module_decision)
    reroute_action = str((global_output.get("reroute_suggestion") or {}).get("action") or "none")
    step_id = str(step.get("step_id"))
    central = next(
        (item for item in metadata.get("central_decisions") or [] if str(item.get("step_id")) == step_id),
        {},
    )
    return {
        "task_subagent": {
            "decision": str(task.get("decision") or "pass"),
            "outputs": sorted(str(item) for item in step.get("outputs") or []),
            "required_skills": sorted(str(item) for item in step.get("required_skills") or []),
        },
        "module_reviewer": {
            "decision": module_decision,
            "has_blockers": bool(module_output.get("blocking_findings")),
        },
        "global_reviewer": {
            "decision": global_decision,
            "evidence_level": str(global_output.get("evidence_level") or ("B" if global_decision == "pass" else "C")),
            "reroute_action": reroute_action,
        },
        "central_agent": {
            "action": str(central.get("decision") or _central_action(module_decision, global_decision, reroute_action)),
        },
    }


def _complexity(metadata: dict[str, Any], user_input: str) -> float:
    plan = metadata.get("dynamic_plan") or {}
    return (
        len(user_input) / 40
        + len(plan.get("steps") or []) * 1.5
        + len(metadata.get("subagent_records") or []) * 0.25
        + len(metadata.get("artifacts") or []) * 0.35
        + len(metadata.get("dynamic_plan_history") or []) * 0.5
        + len(_turns(metadata)) * 0.4
    )


def _generated_boundary_cases(count: int) -> list[dict[str, Any]]:
    templates = [
        "解释当前步骤，不修改计划：{topic}",
        "检查{topic}输出是否足以进入下一步，并说明证据缺口",
        "已有论文证据但缺少{artifact}，中枢下一步应该怎么裁决",
        "真实CST前已完成{topic}，但端口边界未确认，是否应等待用户",
        "不要运行CST，只审查{topic}与当前claim是否一致",
    ]
    topics = ["贴片天线S11", "MIMO隔离度", "圆极化ARBW", "U槽结构参数", "GWO优化范围"]
    artifacts = ["geometry_sketch", "gain export", "parameter_resolution", "port definition", "paper evidence"]
    cases = []
    for index in range(count):
        text = templates[index % len(templates)].format(topic=topics[index % len(topics)], artifact=artifacts[index % len(artifacts)])
        step = {
            "step_id": f"generated_step_{index + 1:03d}",
            "step_goal": "Review the current antenna task and produce the next safe action",
            "inputs": ["user_goal", "current_artifact"],
            "outputs": ["reviewed_next_action"],
            "required_skills": ["antenna-research-reviewer"],
            "status": "pending",
        }
        metadata = {
            "user_input": text,
            "mode": "mock",
            "dynamic_plan": {"plan_version": 1, "steps": [step]},
            "subagent_records": [],
            "artifacts": [],
            "central_agent_messages": [],
            "central_agent_replies": [],
        }
        cases.append({"task_id": f"generated-{index + 1:03d}", "source": "generated_boundary", "metadata": metadata})
    return cases


def build_dataset(store: RedisStore, sample_size: int = 100) -> list[dict[str, Any]]:
    best_by_input: dict[str, dict[str, Any]] = {}
    for path in TASK_ROOT.glob("*/blackboard.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        metadata = data.get("task_metadata") or {}
        user_input = str(metadata.get("user_input") or "").strip()
        if not user_input or "????" in user_input:
            continue
        score = len(metadata.get("subagent_records") or []) + len((metadata.get("dynamic_plan") or {}).get("steps") or [])
        current = best_by_input.get(user_input)
        if current is None or score > current["score"]:
            best_by_input[user_input] = {
                "task_id": str(data.get("task_id") or path.parent.name),
                "source": "runtime_log",
                "metadata": metadata,
                "score": score,
            }
    raw = list(best_by_input.values())
    raw.sort(key=lambda item: (_complexity(item["metadata"], item["metadata"]["user_input"]), stable_hash(item["metadata"]["user_input"])))
    if len(raw) < sample_size:
        raw.extend(_generated_boundary_cases(sample_size - len(raw)))
        raw.sort(key=lambda item: (_complexity(item["metadata"], item["metadata"]["user_input"]), stable_hash(item["metadata"]["user_input"])))
    # Evenly cover the whole observed complexity range instead of taking only one end.
    selected = []
    for index in range(sample_size):
        source_index = round(index * (len(raw) - 1) / max(1, sample_size - 1))
        selected.append(raw[source_index])
    l2_all = _memory_records(store, "mem:l2:*") if store.available else []
    l3_all = _memory_records(store, "mem:l3:workflow:*") if store.available else []
    result = []
    for index, item in enumerate(selected):
        metadata = item["metadata"]
        user_input = str(metadata.get("user_input") or "")
        source_step, _records = _select_step(metadata, user_input)
        scenario = _evaluation_scenario(index)
        step = _compact_step(source_step)
        step["execution_condition"] = scenario["task_decision"]
        step["status"] = scenario["step_status"]
        records = _fixture_records(step, scenario)
        l2 = _relevant(l2_all, user_input, 5)
        l3 = _relevant(l3_all, user_input, 5)
        group = "simple" if index < 34 else "medium" if index < 67 else "complex"
        plan_steps = [_compact_step(value) for value in (metadata.get("dynamic_plan") or {}).get("steps") or []]
        replaced = False
        for plan_index, plan_step in enumerate(plan_steps):
            if str(plan_step.get("step_id")) == str(step.get("step_id")):
                plan_steps[plan_index] = step
                replaced = True
        if not replaced:
            plan_steps.append(step)
        current_plan = {
            "plan_version": (metadata.get("dynamic_plan") or {}).get("plan_version") or 1,
            "active_step_id": step.get("step_id"),
            "steps": plan_steps,
        }
        result.append({
            "case_id": f"context-{index + 1:03d}",
            "source": item["source"],
            "source_task_id": item["task_id"],
            "complexity": group,
            "complexity_score": round(_complexity(metadata, user_input), 4),
            "evaluation_scenario": scenario["name"],
            "context_origin": "human_defined_fixture_over_runtime_question",
            "user_input": user_input,
            "step": step,
            "gold": _fixture_gold(step, scenario),
            "baseline_context": {
                "user_input": user_input,
                "mode": metadata.get("mode"),
                "current_stage": metadata.get("current_stage"),
                "current_plan": current_plan,
                "plan_history": [
                    {"reason": value.get("reason"), "plan_version": ((value.get("plan") or {}).get("plan_version"))}
                    for value in metadata.get("dynamic_plan_history") or [] if isinstance(value, dict)
                ],
                "current_step": step,
                "task_output": records["task"],
                "module_review": records["module_review"],
                "global_review": records["global_review"],
                "all_subagent_history": [_compact_record(value) for value in metadata.get("subagent_records") or []],
                "all_central_decisions": metadata.get("central_decisions") or [],
                "recent_turns": _turns(metadata)[-20:],
                "l2_memory": l2,
                "l3_memory": l3,
            },
            "compressed_parts": {
                "recent_turns_6": _turns(metadata)[-6:],
                "l2_top2": l2[:2],
                "l2_top1": l2[:1],
                "l3_top1": l3[:1],
                "task_output": records["task"],
                "latest_module_review": records["module_review"],
                "current_plan": current_plan,
            },
        })
    return result


def context_for(case: dict[str, Any], agent_role: str, variant: str) -> dict[str, Any]:
    if variant == "baseline":
        return case["baseline_context"]
    parts = case["compressed_parts"]
    step = case["step"]
    if agent_role == "task_subagent":
        return {"user_input": case["user_input"], "current_step": step, "l2_memory": parts["l2_top2"]}
    if agent_role == "module_reviewer":
        return {"current_step": step, "current_artifact": parts["task_output"]}
    if agent_role == "global_reviewer":
        return {
            "current_step": step,
            "current_artifact": parts["task_output"],
            "latest_review": parts["latest_module_review"],
            "l2_memory": parts["l2_top1"],
        }
    return {
        "user_input": case["user_input"],
        "current_plan": parts["current_plan"],
        "recent_turns": parts["recent_turns_6"],
        "l2_memory": parts["l2_top2"],
        "l3_memory": parts["l3_top1"],
    }


def _schema_instruction(agent_role: str) -> str:
    schemas = {
        "task_subagent": '{"decision":"pass|revise|block","outputs":["..."],"required_skills":["..."]}',
        "module_reviewer": '{"decision":"pass|revise|block|reroute|wait_user","has_blockers":true}',
        "global_reviewer": '{"decision":"pass|revise|block|reroute|wait_user","evidence_level":"A|B|C|D","reroute_action":"none|refresh_route|add_step|remove_step|replace_step|rebuild_plan"}',
        "central_agent": '{"action":"pass_next_step|revise_current_step|refresh_skill_route|reroute_plan|block_task|wait_user"}',
    }
    return schemas[agent_role]


def _agent_policy(agent_role: str) -> str:
    policies = {
        "task_subagent": (
            "Read current_step.execution_condition. Copy current_step.outputs as artifact identifier strings, not generated content. "
            "Copy current_step.required_skills exactly. decision equals execution_condition."
        ),
        "module_reviewer": (
            "Review only current_artifact for current_step. artifact_validation valid -> pass/no blockers; "
            "missing_fields -> revise/blockers; unsafe -> block/blockers; needs_user -> wait_user/no blockers."
        ),
        "global_reviewer": (
            "Use latest_review. Copy its evidence_level. dependency_status satisfied -> pass; artifact_revision_required -> revise; "
            "unsafe_dependency -> block; missing_skill_route -> reroute with suggested_reroute_action; "
            "requires_user_approval -> wait_user."
        ),
        "central_agent": (
            "Find current_plan.active_step_id, then inspect that step status. passed -> pass_next_step; revise -> revise_current_step; "
            "blocked -> block_task; reroute -> refresh_skill_route; waiting_approval -> wait_user. Current plan overrides history. "
            "This decision covers only the current step. Never return complete_task."
        ),
    }
    return policies[agent_role]


def _parse_json(raw: str) -> dict[str, Any]:
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I | re.S)
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end >= start:
        text = text[start:end + 1]
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("model response is not a JSON object")
    return value


def _normalize_answer(agent_role: str, answer: dict[str, Any]) -> dict[str, Any]:
    if agent_role == "task_subagent":
        return {
            "decision": str(answer.get("decision") or ""),
            "outputs": sorted(str(item) for item in answer.get("outputs") or []),
            "required_skills": sorted(str(item) for item in answer.get("required_skills") or []),
        }
    if agent_role == "module_reviewer":
        return {"decision": str(answer.get("decision") or ""), "has_blockers": bool(answer.get("has_blockers"))}
    if agent_role == "global_reviewer":
        return {
            "decision": str(answer.get("decision") or ""),
            "evidence_level": str(answer.get("evidence_level") or ""),
            "reroute_action": str(answer.get("reroute_action") or "none"),
        }
    return {"action": str(answer.get("action") or "")}


def _thread_client(api_key: str, base_url: str) -> Any:
    client = getattr(_THREAD_LOCAL, "client", None)
    if client is None:
        from openai import OpenAI

        client = OpenAI(api_key=api_key, base_url=base_url, timeout=60)
        _THREAD_LOCAL.client = client
    return client


def run_call(api_key: str, base_url: str, model: str, case: dict[str, Any], agent_role: str, variant: str) -> dict[str, Any]:
    context = context_for(case, agent_role, variant)
    prompt = {
        "agent_role": agent_role,
        "instruction": "Use only the supplied context. Return exactly the requested JSON schema without explanation.",
        "decision_policy": _agent_policy(agent_role),
        "output_schema": _schema_instruction(agent_role),
        "context": context,
    }
    started = time.perf_counter()
    last_error = None
    for attempt in range(1, 4):
        try:
            client = _thread_client(api_key, base_url)
            response = client.chat.completions.create(
                model=model,
                temperature=0,
                messages=[
                    {"role": "system", "content": "You are a conservative multi-agent runtime component. Return strict JSON only."},
                    {"role": "user", "content": json.dumps(prompt, ensure_ascii=False, separators=(",", ":"))},
                ],
            )
            latency_ms = (time.perf_counter() - started) * 1000
            raw = response.choices[0].message.content or "{}"
            parsed = _normalize_answer(agent_role, _parse_json(raw))
            gold = _normalize_answer(agent_role, case["gold"][agent_role])
            usage = getattr(response, "usage", None)
            prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
            prompt_details = getattr(usage, "prompt_tokens_details", None)
            cached_prompt_tokens = int(getattr(prompt_details, "cached_tokens", 0) or 0)
            return {
                "case_id": case["case_id"],
                "source": case["source"],
                "complexity": case["complexity"],
                "agent_role": agent_role,
                "variant": variant,
                "success": True,
                "exact_match": parsed == gold,
                "prediction": parsed,
                "gold": gold,
                "prompt_tokens": prompt_tokens,
                "cached_prompt_tokens": cached_prompt_tokens,
                "uncached_prompt_tokens": max(0, prompt_tokens - cached_prompt_tokens),
                "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
                "total_tokens": int(getattr(usage, "total_tokens", 0) or 0),
                "latency_ms": round(latency_ms, 3),
                "context_chars": len(json.dumps(context, ensure_ascii=False, separators=(",", ":"))),
                "attempts": attempt,
                "error": None,
            }
        except Exception as exc:
            last_error = str(exc)
            if attempt < 3:
                time.sleep(1.5 * attempt)
    return {
        "case_id": case["case_id"], "source": case["source"], "complexity": case["complexity"],
        "agent_role": agent_role, "variant": variant, "success": False, "exact_match": False,
        "prediction": {}, "gold": case["gold"][agent_role], "prompt_tokens": 0, "completion_tokens": 0,
        "cached_prompt_tokens": 0, "uncached_prompt_tokens": 0, "total_tokens": 0,
        "latency_ms": round((time.perf_counter() - started) * 1000, 3),
        "context_chars": len(json.dumps(context, ensure_ascii=False, separators=(",", ":"))),
        "attempts": 3, "error": last_error,
    }


def _summary_rows(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in results:
        grouped[(row["agent_role"], row["variant"])].append(row)
    summary = []
    for agent_role in AGENT_ROLES:
        for variant in VARIANTS:
            rows = grouped[(agent_role, variant)]
            completed = [row for row in rows if row["success"]]
            summary.append({
                "agent_role": agent_role,
                "variant": variant,
                "cases": len(rows),
                "completed": len(completed),
                "failed": len(rows) - len(completed),
                "accuracy": round(sum(row["exact_match"] for row in rows) / max(1, len(rows)), 4),
                "total_prompt_tokens": sum(row["prompt_tokens"] for row in rows),
                "total_cached_prompt_tokens": sum(row.get("cached_prompt_tokens", 0) for row in rows),
                "total_uncached_prompt_tokens": sum(row.get("uncached_prompt_tokens", row["prompt_tokens"]) for row in rows),
                "total_tokens": sum(row["total_tokens"] for row in rows),
                "mean_prompt_tokens": round(statistics.mean([row["prompt_tokens"] for row in completed]) if completed else 0, 3),
                "mean_uncached_prompt_tokens": round(statistics.mean([row.get("uncached_prompt_tokens", row["prompt_tokens"]) for row in completed]) if completed else 0, 3),
                "mean_total_tokens": round(statistics.mean([row["total_tokens"] for row in completed]) if completed else 0, 3),
                "mean_latency_ms": round(statistics.mean([row["latency_ms"] for row in completed]) if completed else 0, 3),
                "p95_latency_ms": round(sorted([row["latency_ms"] for row in completed])[max(0, math.ceil(len(completed) * 0.95) - 1)] if completed else 0, 3),
                "mean_context_chars": round(statistics.mean([row["context_chars"] for row in rows]) if rows else 0, 3),
            })
    return summary


def _comparison(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_agent = defaultdict(dict)
    for row in rows:
        by_agent[row["agent_role"]][row["variant"]] = row
    output = []
    for agent_role in AGENT_ROLES:
        baseline, compressed = by_agent[agent_role]["baseline"], by_agent[agent_role]["compressed"]
        prompt_saved = baseline["total_prompt_tokens"] - compressed["total_prompt_tokens"]
        uncached_saved = baseline["total_uncached_prompt_tokens"] - compressed["total_uncached_prompt_tokens"]
        output.append({
            "agent_role": agent_role,
            "accuracy_baseline": baseline["accuracy"],
            "accuracy_compressed": compressed["accuracy"],
            "accuracy_delta_points": round((compressed["accuracy"] - baseline["accuracy"]) * 100, 2),
            "prompt_tokens_baseline": baseline["total_prompt_tokens"],
            "prompt_tokens_compressed": compressed["total_prompt_tokens"],
            "prompt_tokens_saved": prompt_saved,
            "prompt_token_saving_rate": round(prompt_saved / max(1, baseline["total_prompt_tokens"]), 4),
            "uncached_prompt_tokens_baseline": baseline["total_uncached_prompt_tokens"],
            "uncached_prompt_tokens_compressed": compressed["total_uncached_prompt_tokens"],
            "uncached_prompt_tokens_saved": uncached_saved,
            "uncached_prompt_token_saving_rate": round(uncached_saved / max(1, baseline["total_uncached_prompt_tokens"]), 4),
            "mean_latency_baseline_ms": baseline["mean_latency_ms"],
            "mean_latency_compressed_ms": compressed["mean_latency_ms"],
            "mean_latency_delta_ms": round(compressed["mean_latency_ms"] - baseline["mean_latency_ms"], 3),
        })
    return output


def _write_report(path: Path, dataset: list[dict[str, Any]], summary: dict[str, Any]) -> None:
    lines = [
        "# Context Compression A/B Evaluation",
        "",
        f"- Model: `{summary['model']}`",
        f"- Cases: {summary['dataset']['total']}",
        f"- Runtime-log cases: {summary['dataset']['runtime_log']}",
        f"- Generated boundary cases: {summary['dataset']['generated_boundary']}",
        f"- External calls: {summary['external_calls']}",
        f"- Wall time: {summary['wall_time_ms'] / 1000:.2f}s",
        "",
        "| Agent | Baseline Acc. | Compressed Acc. | Delta | Total Prompt Saving | Context Token Saving | Baseline Latency | Compressed Latency |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in summary["comparison"]:
        lines.append(
            f"| {row['agent_role']} | {row['accuracy_baseline']:.2%} | {row['accuracy_compressed']:.2%} | "
            f"{row['accuracy_delta_points']:+.2f} pp | {row['prompt_token_saving_rate']:.2%} | {row['uncached_prompt_token_saving_rate']:.2%} | "
            f"{row['mean_latency_baseline_ms']:.1f} ms | {row['mean_latency_compressed_ms']:.1f} ms |"
        )
    lines.extend([
        "",
        "## Context Policy",
        "",
        "- task_subagent: current step + Top2 relevant L2 facts",
        "- module_reviewer: current artifact + current step",
        "- global_reviewer: current artifact + latest module review + Top1 relevant L2 fact",
        "- central_agent: current plan + last 6 turns + Top2 L2 facts + Top1 L3 workflow",
        "",
        "## Accuracy Definition",
        "",
        "Exact match against structured labels derived from the stored task/review/central-decision artifacts. "
        "This measures decision preservation under context reduction, not free-form answer quality.",
    ])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def evaluate(output_dir: Path, sample_size: int, limit: int | None, workers: int) -> dict[str, Any]:
    settings = load_settings(ROOT / "config.yaml")
    api_key = os.getenv(settings.llm_api_key_env)
    if not api_key:
        raise RuntimeError(f"missing LLM API key in environment variable {settings.llm_api_key_env}")
    store = RedisStore(
        settings.redis_url,
        json_retention_days=settings.redis_json_retention_days,
        stream_retention_days=settings.redis_stream_retention_days,
        vector_retention_days=settings.redis_vector_retention_days,
    )
    dataset = build_dataset(store, sample_size=sample_size)
    if limit is not None:
        dataset = dataset[:limit]
    output_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(output_dir / "dataset.json", {
        "schema_version": "1.0",
        "definition": "runtime-log and generated-boundary cases for per-agent context compression evaluation",
        "cases": [{key: value for key, value in case.items() if key not in {"baseline_context", "compressed_parts"}} for case in dataset],
    })
    jobs = [(case, role, variant) for case in dataset for role in AGENT_ROLES for variant in VARIANTS]
    result_path = output_dir / "results.jsonl"
    completed_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    if result_path.exists():
        for line in result_path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except Exception:
                continue
            if row.get("success"):
                completed_by_key[(str(row.get("case_id")), str(row.get("agent_role")), str(row.get("variant")))] = row
    pending_jobs = [
        job for job in jobs
        if (job[0]["case_id"], job[1], job[2]) not in completed_by_key
    ]
    started = time.perf_counter()
    new_results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = [
            executor.submit(run_call, api_key, settings.llm_base_url, settings.llm_model_name, case, role, variant)
            for case, role, variant in pending_jobs
        ]
        result_path.parent.mkdir(parents=True, exist_ok=True)
        for index, future in enumerate(concurrent.futures.as_completed(futures), 1):
            row = future.result()
            new_results.append(row)
            with result_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
                handle.flush()
            if index % 10 == 0 or index == len(futures):
                print(json.dumps({"completed_new_calls": index, "pending_calls": len(futures), "resumed_calls": len(completed_by_key)}, ensure_ascii=False), flush=True)
    results = [*completed_by_key.values(), *new_results]
    results.sort(key=lambda row: (row["case_id"], row["agent_role"], row["variant"]))
    result_path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in results), encoding="utf-8")
    rows = _summary_rows(results)
    summary = {
        "schema_version": "1.0",
        "model": settings.llm_model_name,
        "base_url": settings.llm_base_url,
        "data_egress": True,
        "egress_payload": "task text and sanitized structured agent context; no API keys or paper full text",
        "dataset": {
            "total": len(dataset),
            "runtime_log": sum(case["source"] == "runtime_log" for case in dataset),
            "generated_boundary": sum(case["source"] == "generated_boundary" for case in dataset),
            "simple": sum(case["complexity"] == "simple" for case in dataset),
            "medium": sum(case["complexity"] == "medium" for case in dataset),
            "complex": sum(case["complexity"] == "complex" for case in dataset),
        },
        "memory_source": {"redis_available": store.available, "l2_records": store.count_keys("mem:l2:*") if store.available else 0, "l3_records": store.count_keys("mem:l3:workflow:*") if store.available else 0},
        "external_calls": len(results),
        "new_external_calls": len(new_results),
        "resumed_calls": len(completed_by_key),
        "successful_calls": sum(row["success"] for row in results),
        "failed_calls": sum(not row["success"] for row in results),
        "wall_time_ms": round((time.perf_counter() - started) * 1000, 3),
        "per_agent_variant": rows,
        "comparison": _comparison(rows),
    }
    atomic_write_json(output_dir / "summary.json", summary)
    _write_report(output_dir / "report.md", dataset, summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--sample-size", type=int, default=100)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()
    print(json.dumps(evaluate(Path(args.output_dir), args.sample_size, args.limit, args.workers), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
