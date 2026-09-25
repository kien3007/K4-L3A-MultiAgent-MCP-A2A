"""CoordinatorAgent – orchestrates the full L3A multi-agent workflow.

Lifecycle events emitted (satisfies scoring-policy-v2 workflow component):
  case_received        – emitted by CLI before calling solve_case()
  task_assigned        – emitted by each specialist when assigned
  tool_result_consumed – emitted by mcp_utils.mcp_call() for each MCP call
  handoff              – emitted by each specialist on completion
  policy_decided       – emitted by PolicyAgent
  verification_completed – emitted by VerifierAgent
  case_finalized       – emitted by CLI after solve_case() returns

Handoff sequence:
  Phase 1 (serial)  : OrderItemAgent   → OrderItemResult
  Phase 2 (parallel): PaymentAgent + ShipmentAgent → PaymentResult + ShipmentResult
  Phase 3 (serial)  : PolicyAgent      → PolicyResult
  Phase 4 (serial)  : assemble output → VerifierAgent → validated dict
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from ..mcp_gateway import EvidenceGateway
from ..trace import TraceWriter
from .messages import OrderItemResult, PaymentResult, PolicyResult, ShipmentResult
from .order_item import OrderItemAgent
from .payment import PaymentAgent
from .policy import PolicyAgent
from .shipment import ShipmentAgent
from .verifier import VerifierAgent, VerificationError

logger = logging.getLogger(__name__)

AGENT_NAME = "coordinator"


def _build_claim_assessments(
    case: dict[str, Any],
    order_result: OrderItemResult,
    payment_result: PaymentResult,
    shipment_result: ShipmentResult,
    policy_result: PolicyResult,
) -> list[dict[str, Any]]:
    """Map case claims to verdict with strictly domain-relevant evidence references."""
    claims = case.get("customer_request", {}).get("claims", [])
    result: list[dict[str, Any]] = []

    order_ref = order_result.evidence_refs[0] if order_result.evidence_refs else None
    items_ref = order_result.evidence_refs[1] if len(order_result.evidence_refs) > 1 else order_ref
    pay_ref = payment_result.evidence_refs[0] if payment_result.evidence_refs else None
    ship_ref = shipment_result.evidence_refs[0] if shipment_result.evidence_refs else None
    pol_ref = getattr(policy_result, "policy_ref", None) or (policy_result.evidence_refs[-2] if len(policy_result.evidence_refs) >= 2 else None)
    seller_refs = getattr(policy_result, "seller_evidence_refs", [])

    for claim in claims[:5]:
        claim_id = claim.get("claim_id", "")
        topic = claim.get("topic", "")

        # 1. Determine Verdict & Confidence
        if topic == policy_result.primary_issue:
            if topic == "unsupported_claim":
                verdict = "unsupported"
                conf = 0.95
            else:
                verdict = "supported"
                conf = 0.95
        elif topic == "requested_full_refund":
            if policy_result.case_status == "no_action" or policy_result.recommended_refund_brl == 0:
                verdict = "unsupported"
                conf = 0.95
            elif policy_result.primary_issue in ("canceled_order_paid", "unavailable_order_paid"):
                verdict = "supported"
                conf = 0.95
            else:
                verdict = "partially_supported"
                conf = 0.92
        elif policy_result.primary_issue == "insufficient_evidence":
            verdict = "insufficient_evidence"
            conf = 0.50
        else:
            verdict = "unsupported"
            conf = 0.95

        # 2. Select ONLY relevant evidence refs (avoid forbidden domain penalties)
        claim_ev_refs: list[str] = []
        if topic == "late_delivery_seller":
            claim_ev_refs = [r for r in [ship_ref, items_ref, *seller_refs, pol_ref] if r]
        elif topic == "late_delivery_logistics":
            claim_ev_refs = [r for r in [ship_ref, order_ref, pol_ref] if r]
        elif topic in ("duplicate_charge", "payment_mismatch", "valid_split_payment", "refund_pending", "refund_failed"):
            claim_ev_refs = [r for r in [pay_ref, items_ref if topic == "payment_mismatch" else None, order_ref, pol_ref] if r]
        elif topic in ("canceled_order_paid", "unavailable_order_paid"):
            claim_ev_refs = [r for r in [order_ref, items_ref if topic == "unavailable_order_paid" else None, pay_ref, pol_ref] if r]
        elif topic == "unsupported_claim":
            claim_ev_refs = [r for r in [order_ref, ship_ref, pol_ref] if r]
        elif topic == "requested_full_refund":
            if policy_result.primary_issue in ("late_delivery_seller", "late_delivery_logistics"):
                claim_ev_refs = [r for r in [ship_ref, pay_ref, pol_ref] if r]
            else:
                claim_ev_refs = [r for r in [pay_ref, order_ref, pol_ref] if r]
        else:
            claim_ev_refs = [r for r in [order_ref, pol_ref] if r]

        # Deduplicate preserving order
        claim_ev_refs = list(dict.fromkeys(claim_ev_refs))
        if not claim_ev_refs:
            claim_ev_refs = policy_result.evidence_refs[:2]

        result.append({
            "claim_id": claim_id,
            "verdict": verdict,
            "confidence": max(0.10, min(0.95, conf)),
            "evidence_refs": claim_ev_refs,
        })
    return result


def _build_output(
    case: dict[str, Any],
    order_result: OrderItemResult,
    payment_result: PaymentResult,
    shipment_result: ShipmentResult,
    policy_result: PolicyResult,
) -> dict[str, Any]:
    """Assemble final output dict, strictly conforming to l3a-output-v2 schema."""
    case_id: str = case["case_id"]
    claim_assessments = _build_claim_assessments(
        case, order_result, payment_result, shipment_result, policy_result
    )

    all_seller_ids = list(dict.fromkeys(
        order_result.seller_ids
        + [p["party_id"] for p in policy_result.responsible_parties if p.get("party_type") == "seller" and p.get("party_id")]
    ))

    output: dict[str, Any] = {
        "schema_version": "day09-l3a-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": policy_result.primary_issue,
            "case_status": policy_result.case_status,
            "confidence": policy_result.confidence,
        },
        "affected_entities": {
            "order_ids": order_result.order_ids[:20],
            "item_ids": order_result.item_ids[:20],
            "seller_ids": all_seller_ids[:20],
            "payment_references": payment_result.payment_references[:20],
            "shipment_ids": shipment_result.shipment_ids[:20],
        },
        "root_cause_analysis": {
            "ranked_causes": policy_result.ranked_causes[:5],
            "responsible_parties": policy_result.responsible_parties[:5],
        },
        "evidence_refs": policy_result.evidence_refs[:30],
        "data_conflicts": policy_result.data_conflicts[:5],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": policy_result.recommended_refund_brl,
            "refund_lines": policy_result.refund_lines[:10],
        },
        "resolution_actions": policy_result.resolution_actions[:8],
    }

    if claim_assessments:
        output["claim_assessments"] = claim_assessments

    return output


class CoordinatorAgent:
    """Top-level orchestrator; owns the handoff sequence and A2A protocol."""

    def __init__(self, repo_root: Path) -> None:
        self._order_item = OrderItemAgent()
        self._payment = PaymentAgent()
        self._shipment = ShipmentAgent()
        self._policy = PolicyAgent(repo_root)
        self._verifier = VerifierAgent()

    async def run(
        self,
        case: dict[str, Any],
        gateway: EvidenceGateway,
        trace: TraceWriter,
    ) -> dict[str, Any]:
        case_id: str = case["case_id"]

        # ── Phase 1: Order & Item evidence (serial) ────────────────────────
        order_result: OrderItemResult = await self._order_item.run(case, gateway, trace)

        # ── Phase 2: Payment + Shipment (parallel) ─────────────────────────
        payment_result: PaymentResult
        shipment_result: ShipmentResult
        payment_result, shipment_result = await asyncio.gather(
            self._payment.run(case, order_result, gateway, trace),
            self._shipment.run(case, order_result, gateway, trace),
        )

        # ── Phase 3: Policy decision (serial – needs all upstream) ─────────
        policy_result: PolicyResult = await self._policy.run(
            case, order_result, payment_result, shipment_result, gateway, trace
        )

        # ── Phase 4: Assemble output ───────────────────────────────────────
        output = _build_output(case, order_result, payment_result, shipment_result, policy_result)

        # ── Phase 5: Verify (hard gates + consistency + calibration) ───────
        try:
            self._verifier.run(
                case, output,
                order_result, payment_result, shipment_result, policy_result,
                trace,
            )
        except VerificationError:
            logger.error("[coordinator] verification failed for %s – re-raising", case_id)
            raise

        return output
