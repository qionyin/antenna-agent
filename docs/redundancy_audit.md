# Redundancy Audit

## Current Verdict

The project keeps one executable chain:

```text
input -> Scheduler -> LangGraph planning/execution/repair subgraphs -> subagents -> adapters/MCP -> artifacts -> review/report
```

Rollback/recovery mechanisms were reduced to the ones that affect current real execution.

## Removed

| Item | Previous Use | Removal Reason | Runtime Impact |
|---|---|---|---|
| `Blackboard.rollback_to_checkpoint()` | Manually restore `blackboard.json` to an old version snapshot | It only restored task state, not CST files, artifacts, reports, FailureStore records, or LangGraph checkpoints. This could create inconsistent real-task state. | No impact on normal execution; manual arbitrary rollback API removed. |
| `POST /tasks/{task_id}/rollback/{version}` | Frontend/API entry for manual arbitrary state rollback | Same mismatch as above; not part of the main execution chain. | No impact on create/approve/run/report flow. |
| `workspace/tasks/*/checkpoints/vN.json` generation | Per-update full blackboard version files | Main chain only reads latest `blackboard.json`; LangGraph handles execution resume checkpoints. | Reduces durable state noise. |
| Redis blackboard mirror `checkpoint:dynamic:{task_id}` | Extra copy of the whole task payload in Redis | Duplicated latest disk `blackboard.json` and did not drive resume decisions. | LangGraph Redis checkpoint support remains. |
| `CheckpointManager.put_task_checkpoint/get_task_checkpoint()` | Write/read the duplicate Redis blackboard mirror | Removed with the mirror. | `CheckpointManager` now only reports backend availability. |

## Kept

| Item | Current Use | Reason |
|---|---|---|
| LangGraph checkpoint/resume | Resume paused approval nodes and durable graph state when a checkpointer is available | Required for approval interruption and restart support. |
| `Scheduler.recover_tasks()` | Recover interrupted real CST tasks from `running` to `waiting_approval` and clear the run guard | Prevents accidental duplicate CST solver execution after interruption. |
| `FailureStore` | Store classified step failures, repair rounds, repairability, and terminal status | Required by the repair subgraph. |
| LangGraph repair subgraph | Triage, repair, verify, and replay failed planning/execution steps | Current automatic repair loop depends on it. |
| `repair_archive` | Archive invalid generated artifacts before regeneration | Prevents overwrite loss during artifact repair. |
| Audit and stream replay | Frontend logs and durable traceability | Required for visibility and debugging. |
| `CheckpointManager.health()` | Reports whether Redis is available for LangGraph checkpoint selection | Small health facade; no duplicate task payload storage. |

## Notes

- No CircuitBreaker or dead-letter production implementation currently exists; old references were documentation residue.
- `Blackboard` remains the latest task-state store.
- LangGraph remains the execution checkpoint owner.
- Failure repair remains the only automatic rollback-like path in the active chain.
