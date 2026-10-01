from __future__ import annotations

from typing import Any


class SelfEvolvingEvaluationAdapter:
    """Normalize self-evolving-kb evaluation output to the local contract."""

    def normalize(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise TypeError("evaluation payload must be a dictionary")
        workspace_shape = not isinstance(payload.get("cases"), list)
        if not workspace_shape:
            scale = str(payload.get("score_scale") or "unit")
            return {
                "run_id": payload.get("run_id"),
                "cases": [self._normalize_case(item, scale) for item in payload["cases"] if isinstance(item, dict)],
                "source": str(payload.get("source") or "local"),
                "score_scale": "unit",
            }
        cases: list[dict[str, Any]] = []
        for workspace_name, workspace in (payload.get("workspaces") or {}).items():
            for case in workspace.get("cases") or []:
                if isinstance(case, dict):
                    cases.append({"workspace": workspace_name, **self._normalize_case(case, "five_point")})
        return {"run_id": payload.get("run_id"), "cases": cases, "source": "self-evolving-kb", "score_scale": "unit"}

    @staticmethod
    def _normalize_case(case: dict[str, Any], scale: str) -> dict[str, Any]:
        normalized = dict(case)
        if scale not in {"five_point", "1-5", "five"}:
            return normalized
        for field in ("judge_correctness", "judge_faithfulness"):
            value = case.get(field)
            if value is None:
                continue
            score = float(value)
            normalized[f"{field}_original"] = score
            normalized[field] = round(max(0.0, min(1.0, (score - 1.0) / 4.0)), 6)
        return normalized
