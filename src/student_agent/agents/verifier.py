"""VerifierAgent – validates assembled output before finalisation.

Scoring components addressed (scoring-policy-v2.json):
  - consistency (10%): cross-field checks for status/refund/action,
      seller responsibility, duplicate actions
  - calibration  (5%): confidence bounds sanity
  - schema        (5%): pre-flight structural checks (schema validator runs in CLI)
  - workflow      (5%): emits verification_completed event

Hard gates checked (score = 0 if violated):
  - case_id_mismatch
  - unscorable_schema
  - missing_required_evidence
  - invalid_evidence_refs
"""

from __future__ import annotations

import logging
from typing import Any

from ..trace import TraceWriter
from .messages import OrderItemResult, PaymentResult, PolicyResult, ShipmentResult
from .scoring_policy import ScoringPolicy

logger = logging.getLogger(__name__)

AGENT_NAME = "verifier-agent"


class VerificationError(ValueError):
    """Raised when a hard gate or critical invariant is violated."""


class VerifierAgent:
    """Final gatekeeper: validates output against all competition invariants.

    No MCP tool access – operates only on already-collected data.
    """

    def run(
        self,
        case: dict[str, Any],
        output: dict[str, Any],
        order_result: OrderItemResult,
        payment_result: PaymentResult,
        shipment_result: ShipmentResult,
        policy_result: PolicyResult,
        trace: TraceWriter,
    ) -> dict[str, Any]:
        case_id: str = case["case_id"]

        trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target=AGENT_NAME,
        )

        errors: list[str] = []
        warnings: list[str] = []

        assessment: dict[str, Any] = output.get("assessment", {})
        primary_issue: str = assessment.get("primary_issue", "")
        case_status: str = assessment.get("case_status", "")
        confidence: float = assessment.get("confidence", -1.0)
        fin: dict[str, Any] = output.get("financial_resolution", {})
        refund: float = fin.get("recommended_refund_brl", 0.0)
        refund_lines: list[Any] = fin.get("refund_lines", [])
        actions: list[str] = output.get("resolution_actions", [])
        ev_refs: list[str] = output.get("evidence_refs", [])
        entities: dict[str, list[str]] = output.get("affected_entities", {})
        rca: dict[str, Any] = output.get("root_cause_analysis", {})
        responsible_parties: list[dict[str, Any]] = rca.get("responsible_parties", [])

        # ══════════════════════════════════════════════════════════════════
        # HARD GATES (any failure → VerificationError → score = 0 for case)
        # ══════════════════════════════════════════════════════════════════

        # [HG-1] case_id_mismatch
        if output.get("case_id") != case_id:
            errors.append(
                f"[HG-1 case_id_mismatch] got {output.get('case_id')!r}, expected {case_id!r}"
            )

        # [HG-2] unscorable_schema
        if output.get("schema_version") != "day09-l3a-output-v2":
            errors.append(
                f"[HG-2 unscorable_schema] schema_version must be 'day09-l3a-output-v2'"
            )

        # [HG-3] missing_required_evidence / invalid_evidence_refs
        for ref in ev_refs:
            if not ref.startswith("ev_"):
                errors.append(f"[HG-4 invalid_evidence_refs] ref does not start with 'ev_': {ref!r}")

        # [HG-4] action_required without any evidence = missing_required_evidence
        if case_status == "action_required" and not ev_refs:
            errors.append(
                "[HG-3 missing_required_evidence] action_required output has no evidence_refs"
            )

        # ══════════════════════════════════════════════════════════════════
        # CONSISTENCY CHECKS (scoring component: 10%)
        # ══════════════════════════════════════════════════════════════════

        # [C-1] primary_issue → correct responsible_party type
        expected_parties = ScoringPolicy.RESPONSIBILITY_MAP.get(primary_issue, set())
        if isinstance(expected_parties, str):
            expected_parties = {expected_parties}
        actual_parties = {p.get("party_type") for p in responsible_parties}
        if expected_parties and actual_parties and not (expected_parties & actual_parties):
            errors.append(
                f"[C-1 consistency] primary_issue={primary_issue!r} requires "
                f"responsible_party in {expected_parties!r}, got {actual_parties}"
            )

        # [C-2] late_delivery issues: logistics_provider must NOT be refund entity
        if primary_issue in ScoringPolicy.LOGISTICS_NOT_REFUND_PARTY:
            for line in refund_lines:
                if line.get("entity_id") in shipment_result.shipment_ids:
                    errors.append(
                        f"[C-2 consistency] {primary_issue!r}: "
                        "logistics shipment_id cannot be the refund entity"
                    )

        # [C-3] seller-responsible issue → seller_id must be in responsible_parties
        if primary_issue in ("unavailable_order_paid", "late_delivery_seller"):
            seller_party_ids = [
                p.get("party_id") for p in responsible_parties
                if p.get("party_type") == "seller"
            ]
            if order_result.seller_ids and not any(
                sid in seller_party_ids for sid in order_result.seller_ids
            ):
                warnings.append(
                    f"[C-3 consistency] {primary_issue!r}: seller_id not set in responsible_parties"
                )

        # [C-4] non-zero refund requires at least one refund_line
        if refund > 0 and not refund_lines:
            errors.append(
                "[C-4 consistency] recommended_refund_brl > 0 but refund_lines is empty"
            )

        # [C-5] zero refund must have empty refund_lines (unless needs_investigation)
        if refund == 0 and refund_lines and case_status != "needs_investigation":
            errors.append(
                "[C-5 consistency] refund_lines present but recommended_refund_brl == 0"
            )

        # [C-6] action_required → resolution_actions must be non-empty
        if case_status == "action_required" and not actions:
            errors.append(
                "[C-6 consistency] case_status=action_required but resolution_actions is empty"
            )

        # [C-7] no_action → recommended_refund_brl must be 0
        if case_status == "no_action" and refund > 0:
            errors.append(
                f"[C-7 consistency] case_status=no_action but recommended_refund_brl={refund}"
            )

        # [C-8] duplicate resolution actions
        if len(actions) != len(set(actions)):
            errors.append(
                "[C-8 consistency] resolution_actions contains duplicates: "
                + str([a for a in actions if actions.count(a) > 1])
            )

        # [C-9] financial refund cannot exceed captured amount
        cap = payment_result.captured_total_brl
        if cap is not None and refund > cap * 1.01:  # 1% tolerance for float rounding
            errors.append(
                f"[C-9 consistency] recommended_refund_brl ({refund}) "
                f"exceeds captured_total ({cap})"
            )

        # [C-10] entities coverage: all order_ids from evidence must appear in output
        for oid in order_result.order_ids:
            if oid not in entities.get("order_ids", []):
                errors.append(
                    f"[C-10 consistency] order_id {oid!r} in evidence "
                    "but missing from affected_entities.order_ids"
                )

        # ══════════════════════════════════════════════════════════════════
        # CALIBRATION CHECKS (scoring component: 5%)
        # ══════════════════════════════════════════════════════════════════

        # [CAL-1] confidence in [0.0, 1.0]
        if not (0.0 <= confidence <= 1.0):
            errors.append(f"[CAL-1 calibration] confidence={confidence} not in [0, 1]")

        # [CAL-2] never claim 1.0 (perfect certainty requires perfect evidence)
        if confidence >= 1.0:
            errors.append(
                "[CAL-2 calibration] confidence=1.0 is not allowed; "
                "use ≤0.95 to reflect residual uncertainty"
            )

        # [CAL-3] insufficient_evidence should not have high confidence
        if primary_issue == "insufficient_evidence" and confidence > 0.65:
            warnings.append(
                f"[CAL-3 calibration] primary_issue=insufficient_evidence "
                f"but confidence={confidence} > 0.65"
            )

        # ══════════════════════════════════════════════════════════════════
        # EMIT & RETURN
        # ══════════════════════════════════════════════════════════════════

        for w in warnings:
            logger.warning("[verifier] %s", w)

        if errors:
            for err in errors:
                logger.error("[verifier] %s", err)
            trace.emit(
                case_id=case_id,
                event_type="verification_completed",
                actor=AGENT_NAME,
                decision_code="VERIFICATION_FAILED",
                attributes={
                    "error_count": len(errors),
                    "warning_count": len(warnings),
                    "first_error": errors[0][:160],
                },
            )
            raise VerificationError(
                f"Verification failed for {case_id} ({len(errors)} errors): "
                + errors[0]
            )

        trace.emit(
            case_id=case_id,
            event_type="verification_completed",
            actor=AGENT_NAME,
            decision_code="VERIFICATION_PASSED",
            evidence_refs=ev_refs[:20] or None,
            attributes={
                "warning_count": len(warnings),
                "evidence_count": len(ev_refs),
                "primary_issue": primary_issue,
                "confidence": confidence,
            },
        )
        return output
