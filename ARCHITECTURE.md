# L3A Architecture Record

## 1. System Overview

```text
inputs/<case_id>.json
        │
        ▼
┌──────────────────────────┐
│   CoordinatorAgent       │  (workflow.py → agents/coordinator.py)
└─────────────┬────────────┘
              │
   Phase 1 (serial – output feeds phases 2 & 3)
              ▼
┌──────────────────────────┐
│   OrderItemAgent         │  MCP: get_orders, get_items
└─────────────┬────────────┘
              │ OrderItemResult (handoff)
   Phase 2 (parallel via asyncio.gather)
    ┌─────────┴─────────┐
    ▼                   ▼
┌──────────────┐ ┌──────────────────────┐
│ PaymentAgent │ │  ShipmentAgent       │
│ MCP:         │ │  MCP:                │
│  get_order_  │ │   get_shipment_      │
│   payments   │ │   summary            │
└──────┬───────┘ └────────┬─────────────┘
       │ PaymentResult    │ ShipmentResult
       └────────┬─────────┘
                │
   Phase 3 (serial – needs all upstream results)
                ▼
┌──────────────────────────┐
│   PolicyAgent            │  MCP: get_policy, get_sellers
└─────────────┬────────────┘
              │ PolicyResult
   Phase 4 (assemble + verify)
              ▼
┌──────────────────────────┐
│   VerifierAgent          │  No MCP – validates invariants
└─────────────┬────────────┘
              │ Validated output dict
              ▼
      outputs/<case_id>.json
      traces/trace.jsonl
```

## 2. Agent Ownership

| Actor | Input | Trách nhiệm | Output / Handoff |
|---|---|---|---|
| CoordinatorAgent | `case` dict | Orchestrate phases, aggregate results, build final output | Calls all specialists; returns `dict[str,Any]` |
| OrderItemAgent | `case` | Collect order status, item availability via MCP | `OrderItemResult` → Coordinator |
| PaymentAgent | `OrderItemResult` | Reconcile payments, detect duplicate/failed refunds | `PaymentResult` → Coordinator |
| ShipmentAgent | `OrderItemResult` | Trace shipment timeline, identify delay cause | `ShipmentResult` → Coordinator |
| PolicyAgent | All upstream results | Apply policy rules, decide primary_issue, compute refund | `PolicyResult` → Coordinator |
| VerifierAgent | Assembled output + all upstream results | Check all schema & business invariants | Pass-through or raise `VerificationError` |

### MCP Tool Permissions (principle of least privilege)

| Agent | Allowed Tools |
|---|---|
| OrderItemAgent | `get_orders`, `get_items` |
| PaymentAgent | `get_order_payments` |
| ShipmentAgent | `get_shipment_summary` |
| PolicyAgent | `get_policy`, `get_sellers` |
| VerifierAgent | *(none)* |
| CoordinatorAgent | *(none – delegates only)* |

## 3. A2A Protocol

- **Message envelope**: typed Python dataclasses in `agents/messages.py` (`OrderItemResult`, `PaymentResult`, `ShipmentResult`, `PolicyResult`).
- **Correlation**: every MCP call and trace event carries `case_id`; evidence refs are per-case and never reused across cases.
- **Handoff conditions**: each specialist must complete successfully before the coordinator invokes the next phase. Payment + Shipment run concurrently (Phase 2) because they are independent given the order_ids.
- **Timeout**: inherited from `httpx2.Timeout(300s)` in `mcp_gateway.py`. No agent-level timeout is added on top.
- **Loop prevention**: the Coordinator calls each specialist exactly once per case; there is no feedback loop back to a specialist from a downstream agent.
- **Observable events only**: trace logs contain `event_type`, `actor`, `decision_code`, and `evidence_refs`. No prompts or chain-of-thought are written.

## 4. Evidence Lifecycle

1. MCP Gateway returns a response; `contracts.validate_evidence()` checks `mcp-evidence-response-v1.schema.json`.
2. The `evidence_ref` (pattern `ev_…`) is extracted and stored in the specialist's result dataclass.
3. At trace time, the consuming agent emits `tool_result_consumed` with the `evidence_ref`.
4. PolicyAgent aggregates all refs from all upstream agents into `PolicyResult.evidence_refs`.
5. The Coordinator writes `evidence_refs` (max 30) into the final output dict.
6. Evidence refs are **not** carried over between cases; `TraceWriter` opens a fresh file per run.

## 5. Failure Policy

| Failure | Retry? | Fallback | Trace event / code |
|---|---|---|---|
| MCP timeout / `RuntimeError` | Yes – up to 2 retries (total 3 attempts) | If exhausted: field stays `None` / `"insufficient_evidence"` | `tool_result_consumed` with `decision_code="MCP_EXHAUSTED"` |
| Tool `isError=True` | Yes – same as timeout | Same fallback | Same code |
| Entity not found (empty data) | No | Agent returns defaults; PolicyAgent maps to `"insufficient_evidence"` | `handoff` with `decision_code="*_DONE"` |
| Source conflict (captured ≠ refundable) | No | PolicyAgent records a `data_conflict` entry | logged in `data_conflicts` array |
| Invalid specialist result | No | VerifierAgent raises `VerificationError`; CLI catches & aborts case | `verification_completed` with `decision_code="VERIFICATION_FAILED"` |

> Retry is idempotent: MCP calls are read-only GET operations.  
> Missing evidence is **never** fabricated; `confidence` is reduced to reflect uncertainty.

## 6. Verification Invariants (VerifierAgent)

Checked before every `case_finalized` trace event:

1. `case_id` in output matches input case.
2. `schema_version == "day09-l3a-output-v2"`.
3. `evidence_refs` non-empty when `case_status == "action_required"`.
4. All `evidence_refs` match pattern `^ev_…`.
5. `recommended_refund_brl >= 0`; non-zero refund implies at least one `refund_line`.
6. `confidence ∈ [0.0, 1.0]`.
7. `resolution_actions` non-empty when `case_status == "action_required"`.
8. All `order_ids` returned by OrderItemAgent appear in `affected_entities.order_ids`.
9. (Schema-level) `additionalProperties: false` enforced by `contracts.validate_output()` in CLI before disk write.

## 7. Reproducibility

- **Python**: 3.12 (see `pyproject.toml` `requires-python = ">=3.11"`)
- **Dependencies**: pinned via `pip install -e ".[dev]"` from `pyproject.toml`.
- **Concurrency**: Payment + Shipment agents run concurrently via `asyncio.gather`; no thread pools.
- **Random seed**: none – all logic is deterministic rule-based; no LLM sampling used.
- **Run command**: `day09 run` (from repo root with `.env` configured).
- **Resource limits**: MCP timeout 300 s total / 30 s connect; max 2 retries per tool call.
- **No API keys in this file.**
