from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from tests.test_parent_subgraphs_and_repair import ParentSubgraphIntegrationTests, modeling_request


with tempfile.TemporaryDirectory() as root:
    scheduler = ParentSubgraphIntegrationTests.scheduler(root)
    request = modeling_request(Path(root))
    original = scheduler.modeling_preparation.prepare
    attempts = 0

    def corrupt_once(modeling_input, output_dir):
        global attempts
        attempts += 1
        result = original(modeling_input, output_dir)
        if attempts == 1 and result.get("success"):
            Path(result["artifacts"]["cst_model_spec"]).write_text("{}\n", encoding="utf-8")
        return result

    with patch.object(scheduler.modeling_preparation, "prepare", side_effect=corrupt_once):
        state = scheduler.create_task_from_request("Regenerate a malformed CST model specification.", modeling_request=request)
    metadata = state.get("task_metadata") or {}
    print(json.dumps({
        "state": state.get("state"),
        "attempts": attempts,
        "failure_reason": metadata.get("failure_reason"),
        "active_failure": metadata.get("active_failure"),
        "repair_decision": metadata.get("repair_decision"),
        "blockers": state.get("blockers"),
        "reports": metadata.get("reports"),
        "nodes": state.get("nodes"),
    }, ensure_ascii=False, indent=2, default=str))
