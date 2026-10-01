from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from adapters.paperwise_adapter import PaperWiseAdapter
from agent_runtime.config import load_settings
from agent_runtime.skill_router import SkillRouter
from agent_runtime.utils import atomic_write_json


OUTPUT_DIR = PROJECT_ROOT / "tests" / "evaluation" / "v2_3_baseline_100"
CASES_PATH = OUTPUT_DIR / "cases.jsonl"
GOLD_PATH = OUTPUT_DIR / "gold_annotations.jsonl"
RESULTS_PATH = OUTPUT_DIR / "baseline_results.jsonl"
METRICS_PATH = OUTPUT_DIR / "metrics.json"
REPORT_PATH = OUTPUT_DIR / "report.md"


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def _normalized_terms(index: dict[str, Any]) -> list[str]:
    aliases = {
        "patch": "patch antenna",
        "slot": "slot antenna",
        "array": "antenna array",
        "mimo": "MIMO antenna",
        "circular_polarization": "circular polarization",
        "dielectric_resonator": "dielectric resonator antenna",
        "monopole": "printed monopole",
        "metasurface": "metasurface antenna",
        "transmitarray": "transmitarray",
        "s11": "S11",
        "bandwidth": "bandwidth",
        "gain": "gain",
        "efficiency": "efficiency",
        "axial_ratio": "axial ratio",
        "gwo": "GWO",
        "pso": "PSO",
        "ga": "genetic algorithm",
        "ann": "ANN surrogate",
        "de": "differential evolution",
    }
    values = []
    for key in (*index.get("structures", []), *index.get("objectives", []), *index.get("algorithms", [])):
        if key in aliases and aliases[key] not in values:
            values.append(aliases[key])
    return values


def _sample_reports(adapter: PaperWiseAdapter, count: int = 20) -> list[dict[str, Any]]:
    usable = []
    for item in adapter.list_reports(limit=1000):
        report = adapter.read_report(str(item["path"]), max_chars=2000)
        if not report.get("available"):
            continue
        index = adapter._build_report_index(report)
        terms = _normalized_terms(index)
        if terms:
            usable.append({"report": report, "index": index, "terms": terms})
    if not usable:
        raise RuntimeError("PaperWise has no report with recognized antenna/metric terms")
    step = max(1, len(usable) // count)
    selected = [usable[min(i * step, len(usable) - 1)] for i in range(count)]
    unique = []
    seen = set()
    for item in selected + usable:
        path = str(item["report"].get("path"))
        if path not in seen:
            seen.add(path)
            unique.append(item)
        if len(unique) >= count:
            break
    return unique[:count]


def _case(case_id: str, category: str, query: str, *, expected_domain: str, gold: list[str] | None = None, expected_action: str = "answer", expected_relation: str = "", forbidden_claim: str = "", boundary_reason: str = "") -> dict[str, Any]:
    return {
        "case_id": case_id,
        "category": category,
        "query": query,
        "expected_domain": expected_domain,
        "gold_papers": gold or [],
        "expected_action": expected_action,
        "expected_relation": expected_relation,
        "forbidden_claim": forbidden_claim,
        "boundary_reason": boundary_reason,
    }


def build_cases(adapter: PaperWiseAdapter) -> list[dict[str, Any]]:
    reports = _sample_reports(adapter, 20)
    cases: list[dict[str, Any]] = []
    for i, item in enumerate(reports):
        report = item["report"]
        terms = item["terms"]
        query = " ".join(terms[:4])
        cases.append(_case(f"case_{len(cases)+1:03d}", "direct_match", query, expected_domain="antenna", gold=[str(report["path"])], expected_relation="paper topic and reported antenna metrics"))
    synonym_map = {"patch": "microstrip patch antenna", "s11": "return loss", "gwo": "Grey Wolf Optimizer", "pso": "particle swarm optimization", "ga": "genetic algorithm", "circular_polarization": "圆极化", "bandwidth": "宽带", "gain": "realized gain", "monopole": "printed monopole"}
    for i, item in enumerate(reports[:15]):
        names = item["index"].get("structures", []) + item["index"].get("objectives", []) + item["index"].get("algorithms", [])
        translated = [synonym_map.get(name, name) for name in names[:3]]
        cases.append(_case(f"case_{len(cases)+1:03d}", "synonym", " ".join(translated), expected_domain="antenna", gold=[str(item["report"]["path"])], expected_relation="synonym-normalized paper topic"))
    for i, item in enumerate(reports[:10]):
        terms = item["terms"]
        query = f"{' '.join(terms[:3])} parameter count {3 + i % 5} target frequency and optimization result"
        cases.append(_case(f"case_{len(cases)+1:03d}", "multi_constraint", query, expected_domain="antenna", gold=[str(item["report"]["path"])], expected_relation="structure/objective/algorithm/parameter constraint"))
    for i, item in enumerate(reports[:15]):
        terms = item["terms"]
        relation = f"在{' '.join(terms[:2])}中，参数变化如何影响{terms[1] if len(terms) > 1 else '性能指标'}？"
        cases.append(_case(f"case_{len(cases)+1:03d}", "domain_relation", relation, expected_domain="antenna", gold=[str(item["report"]["path"])], expected_relation="parameter affects electromagnetic metric", forbidden_claim="不能推出所有结构和频段都成立"))
    for i, item in enumerate(reports[:10]):
        terms = item["terms"]
        query = f"{' '.join(terms[:3])} 论文缺少馈电位置和端口尺寸，能否直接证明增益和带宽都提升？"
        cases.append(_case(f"case_{len(cases)+1:03d}", "missing_evidence", query, expected_domain="antenna", gold=[str(item["report"]["path"])], expected_action="insufficient", forbidden_claim="不能直接证明增益和带宽都提升"))
    for i in range(10):
        left = reports[i]["report"]
        right = reports[(i + 1) % len(reports)]["report"]
        query = f"比较论文 {left['title']} 和 {right['title']} 对带宽或增益的结论，是否存在冲突？"
        cases.append(_case(f"case_{len(cases)+1:03d}", "conflict", query, expected_domain="antenna", gold=[str(left["path"]), str(right["path"])], expected_action="compare"))
    negatives = [
        "金融模型的 return loss 和 drawdown 如何优化？",
        "优化这个 React 组件的性能",
        "CNN 准确率太低，如何训练？",
        "Create a CST-style dashboard card layout",
        "检查 VSCode 端口占用",
        "论文摘要如何写得更好？",
        "矩形的面积如何最大化？",
        "邮件系统的带宽如何提升？",
        "医学影像的 MRI 信号如何重建？",
        "股票收益率和 gain 的关系是什么？",
    ]
    for query in negatives:
        cases.append(_case(f"case_{len(cases)+1:03d}", "negative_cross_domain", query, expected_domain="non_antenna", expected_action="reject"))
    for i, item in enumerate(reports[:5]):
        terms = item["terms"]
        query = f"已有{' '.join(terms[:3])}工作基础上，把馈电鲁棒性加入优化目标是否构成创新？"
        cases.append(_case(f"case_{len(cases)+1:03d}", "innovation", query, expected_domain="antenna", gold=[str(item["report"]["path"])], expected_action="candidate_not_proven", forbidden_claim="不能仅凭算法和结构组合宣布创新成立"))
    boundaries = [
        ("", "empty input"),
        ("   ", "whitespace only"),
        ("S11 " * 1200, "overlong repeated input"),
        ("2.45G 矩形贴片 S1l 带宽", "typo and unit variant"),
        ("参数数量: 0，频段: -2 GHz，S11 > 0 dB", "invalid numeric constraints"),
    ]
    for query, reason in boundaries:
        cases.append(_case(f"case_{len(cases)+1:03d}", "boundary", query, expected_domain="ambiguous", expected_action="clarify_or_reject", boundary_reason=reason))
    if len(cases) != 100:
        raise AssertionError(f"expected 100 cases, got {len(cases)}")
    return cases


def _all_items(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    sources = evidence.get("sources") or {}
    for source in (sources.get("reports") or {}, sources.get("deep_read_papers") or {}, sources.get("graph_library") or {}):
        items.extend(item for item in (source.get("items") or []) if isinstance(item, dict))
    return items


def _retrieved_paths(evidence: dict[str, Any]) -> set[str]:
    paths = set()
    for item in _all_items(evidence):
        for key in ("path", "paper_path"):
            value = item.get(key)
            if value and str(value).endswith("report.md"):
                paths.add(str(Path(str(value)).resolve()))
    return paths


def _route_observation(router: SkillRouter, query: str) -> dict[str, Any]:
    plan = router.build_plan(query, mode="mock", require_paperwise=False, task_id="v2_3_baseline")
    owners = [str(item.get("owner_skill")) for item in plan.get("routes") or []]
    return {
        "domain": (plan.get("domain_decision") or {}).get("primary_domain"),
        "decision": plan.get("decision"),
        "owners": owners,
        "paperwise_routed": "paperwise" in owners,
        "antenna_routed": any(owner.startswith("antenna-") or owner in {"paperwise", "cst-control", "e-platform-cst"} for owner in owners),
        "route_plan": plan,
    }


def _evaluate_case(case: dict[str, Any], adapter: PaperWiseAdapter, router: SkillRouter) -> dict[str, Any]:
    started = time.perf_counter()
    route_error = None
    evidence_error = None
    try:
        route = _route_observation(router, case["query"])
    except Exception as exc:
        route = {}
        route_error = f"{exc.__class__.__name__}: {exc}"
    try:
        evidence = adapter.evidence_pool_summary(case["query"], limit=10, allow_external_embedding=False)
    except Exception as exc:
        evidence = {"status": "error", "sources": {}, "error": str(exc)}
        evidence_error = f"{exc.__class__.__name__}: {exc}"
    retrieved = _retrieved_paths(evidence)
    gold = {str(Path(path).resolve()) for path in case.get("gold_papers") or []}
    gold_hit = bool(gold.intersection(retrieved)) if gold else False
    expected_domain = case["expected_domain"]
    route_pass = (
        (expected_domain == "antenna" and bool(route.get("antenna_routed")))
        or (expected_domain == "non_antenna" and not bool(route.get("antenna_routed")))
        or (expected_domain == "ambiguous" and True)
    )
    result = {
        "case": case,
        "route": route,
        "evidence": evidence,
        "retrieved_report_paths": sorted(retrieved),
        "gold_hit": gold_hit,
        "route_pass": route_pass,
        "route_error": route_error,
        "evidence_error": evidence_error,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
        "tested_at": datetime.now(timezone.utc).isoformat(),
    }
    return result


def _metrics(results: list[dict[str, Any]], adapter: PaperWiseAdapter) -> dict[str, Any]:
    by_category: dict[str, list[dict[str, Any]]] = {}
    for result in results:
        by_category.setdefault(result["case"]["category"], []).append(result)
    def rate(rows: list[dict[str, Any]], key: str) -> float | None:
        return round(sum(bool(row.get(key)) for row in rows) / len(rows), 4) if rows else None
    route_rows = [row for row in results if row["case"]["expected_domain"] in {"antenna", "non_antenna"}]
    positive_rows = [row for row in results if row["case"]["expected_domain"] == "antenna" and row["case"]["category"] not in {"missing_evidence", "innovation"}]
    negative_rows = [row for row in results if row["case"]["expected_domain"] == "non_antenna"]
    return {
        "schema_version": "1.0",
        "evaluation": "v2_3_current_system_baseline",
        "system_scope": "current router + current PaperWise local report/vector/graph pipeline; no CST solver; no external embedding",
        "paperwise_report_count": len(adapter.list_reports(limit=1000)),
        "case_count": len(results),
        "category_counts": dict(Counter(row["case"]["category"] for row in results)),
        "route": {
            "antenna_and_non_antenna_accuracy": rate(route_rows, "route_pass"),
            "antenna_positive_route_rate": rate([row for row in route_rows if row["case"]["expected_domain"] == "antenna"], "route_pass"),
            "non_antenna_rejection_rate": rate(negative_rows, "route_pass"),
        },
        "retrieval": {
            "positive_gold_report_hit_rate": rate(positive_rows, "gold_hit"),
            "all_gold_report_hit_rate": rate([row for row in results if row["case"].get("gold_papers")], "gold_hit"),
            "missing_evidence_gold_report_recall": rate(by_category.get("missing_evidence", []), "gold_hit"),
            "conflict_case_any_source_recall": round(sum(bool(row["gold_hit"]) for row in by_category.get("conflict", [])) / len(by_category.get("conflict", [])), 4) if by_category.get("conflict") else None,
        },
        "operational": {
            "mean_latency_ms": round(sum(row["elapsed_ms"] for row in results) / len(results), 2),
            "p95_latency_ms": sorted(row["elapsed_ms"] for row in results)[max(0, int(len(results) * 0.95) - 1)],
            "route_errors": sum(bool(row["route_error"]) for row in results),
            "evidence_errors": sum(bool(row["evidence_error"]) for row in results),
        },
    }


def _write_report(metrics: dict[str, Any], results: list[dict[str, Any]]) -> None:
    failures = [row for row in results if not row["route_pass"] or (row["case"].get("gold_papers") and not row["gold_hit"])]
    lines = [
        "# V2.3 当前系统 100 条基线测试",
        "",
        f"- 测试时间：{datetime.now(timezone.utc).isoformat()}",
        "- 测试范围：当前 Skill Router + PaperWise 本地报告/向量/图谱流程。",
        "- 外部 embedding：未调用。真实 CST：未调用。",
        "- 测试案例和原始结果与本报告同目录保存。",
        "",
        "## 指标",
        "",
        "```json",
        json.dumps(metrics, ensure_ascii=False, indent=2),
        "```",
        "",
        f"## 失败或未命中案例（{len(failures)} 条）",
        "",
    ]
    for row in failures:
        case = row["case"]
        lines.append(f"- `{case['case_id']}` [{case['category']}] route_pass={row['route_pass']} gold_hit={row['gold_hit']}：{case['query'][:180]}")
    REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    global OUTPUT_DIR, CASES_PATH, GOLD_PATH, RESULTS_PATH, METRICS_PATH, REPORT_PATH
    parser = argparse.ArgumentParser(description="Run the current system baseline on 100 real PaperWise-grounded cases.")
    parser.add_argument("--output-dir", default=str(OUTPUT_DIR))
    parser.add_argument("--resume", action="store_true", help="Reuse existing cases.jsonl and continue missing cases")
    args = parser.parse_args()
    OUTPUT_DIR = Path(args.output_dir).resolve()
    CASES_PATH = OUTPUT_DIR / "cases.jsonl"
    GOLD_PATH = OUTPUT_DIR / "gold_annotations.jsonl"
    RESULTS_PATH = OUTPUT_DIR / "baseline_results.jsonl"
    METRICS_PATH = OUTPUT_DIR / "metrics.json"
    REPORT_PATH = OUTPUT_DIR / "report.md"
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    settings = load_settings(PROJECT_ROOT / "config.yaml")
    adapter = PaperWiseAdapter(settings.paperwise_root)
    router = SkillRouter()
    if args.resume and CASES_PATH.is_file():
        cases = [json.loads(line) for line in CASES_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
    else:
        cases = build_cases(adapter)
        _write_jsonl(CASES_PATH, cases)
        _write_jsonl(GOLD_PATH, cases)
    prior = {}
    if args.resume and RESULTS_PATH.is_file():
        prior = {row["case"]["case_id"]: row for row in (json.loads(line) for line in RESULTS_PATH.read_text(encoding="utf-8").splitlines() if line.strip())}
    results = []
    for index, case in enumerate(cases, 1):
        result = prior.get(case["case_id"]) or _evaluate_case(case, adapter, router)
        results.append(result)
        _write_jsonl(RESULTS_PATH, results)
        print(json.dumps({"completed": index, "total": len(cases), "case_id": case["case_id"], "elapsed_ms": result["elapsed_ms"], "gold_hit": result["gold_hit"], "route_pass": result["route_pass"]}, ensure_ascii=False), flush=True)
    metrics = _metrics(results, adapter)
    atomic_write_json(METRICS_PATH, metrics)
    _write_report(metrics, results)
    print(json.dumps({"ok": True, "output_dir": str(OUTPUT_DIR), "metrics": metrics}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
