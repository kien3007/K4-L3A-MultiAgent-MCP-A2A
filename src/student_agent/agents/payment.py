"""PaymentAgent – reconciles payment evidence via MCP.

MCP tool permissions: get_order_payments  (get_refund_timeline not functional)

Actual MCP response shapes (probed from live server):
  get_order_payments: data = list[{order_id, payment_sequential, payment_type,
                                   payment_installments, payment_value}]
  get_refund_timeline: currently returns server error – skipped gracefully
"""

from __future__ import annotations

import logging
from typing import Any

from ..mcp_gateway import EvidenceGateway
from ..trace import TraceWriter
from .messages import OrderItemResult, PaymentResult
from .mcp_utils import mcp_call

logger = logging.getLogger(__name__)

AGENT_NAME = "payment-agent"

# payment_type values that indicate a split/voucher payment
_SPLIT_TYPES = {"boleto", "voucher", "debit_card"}


class PaymentAgent:
    """Specialist responsible for payment reconciliation evidence."""

    async def run(
        self,
        case: dict[str, Any],
        order_result: OrderItemResult,
        gateway: EvidenceGateway,
        trace: TraceWriter,
    ) -> PaymentResult:
        case_id: str = case["case_id"]

        trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target=AGENT_NAME,
            attributes={"order_ids": ",".join(order_result.order_ids)},
        )

        evidence_refs: list[str] = []
        payment_references: list[str] = []
        captured_total: float = 0.0
        has_any_payment = False
        has_multiple_types = False
        raw_data: dict[str, Any] = {}
        claimed_order_id = case["customer_request"]["claimed_order_id"]

        # Use claimed_order_id as primary; fall back to resolved order_ids
        orders_to_check = list(dict.fromkeys(
            [claimed_order_id] + order_result.order_ids
        ))

        for order_id in orders_to_check:
            pay_ev = await mcp_call(
                gateway, "get_order_payments",
                case_id=case_id, actor=AGENT_NAME, trace=trace,
                order_id=order_id,
            )
            if pay_ev is None:
                continue

            evidence_refs.append(pay_ev["evidence_ref"])
            pay_list: list[dict[str, Any]] = pay_ev.get("data", [])
            raw_data.setdefault("payments", {})[order_id] = pay_list
            has_any_payment = True

            payment_types: set[str] = set()
            for payment in pay_list:
                # Build synthetic payment reference from sequential + type
                seq = payment.get("payment_sequential", "")
                ptype = payment.get("payment_type", "")
                ref = f"{order_id}-{ptype}-{seq}"
                payment_references.append(ref)
                captured_total += float(payment.get("payment_value", 0) or 0)
                payment_types.add(ptype)

            if len(payment_types) > 1:
                has_multiple_types = True

        # ── Determine verdict from evidence ───────────────────────────────
        # get_refund_timeline is non-functional; derive from order_status only.
        order_status = order_result.order_status
        verdict: str
        if not has_any_payment:
            verdict = "insufficient_evidence"
        elif order_status == "canceled" and captured_total > 0:
            # Order canceled but payment was captured → needs refund
            verdict = "refund_pending"
        elif has_multiple_types:
            # Multiple payment methods → valid split payment
            verdict = "reconciled"
        elif captured_total > 0:
            verdict = "reconciled"
        else:
            verdict = "insufficient_evidence"

        trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor=AGENT_NAME,
            target="coordinator",
            decision_code="PAYMENT_DONE",
            evidence_refs=evidence_refs or None,
            attributes={"verdict": verdict, "captured_total": captured_total},
        )

        refundable = captured_total if order_status == "canceled" else 0.0

        return PaymentResult(
            payment_references=list(dict.fromkeys(payment_references)),
            evidence_refs=list(dict.fromkeys(evidence_refs)),
            verdict=verdict,
            captured_total_brl=captured_total if has_any_payment else None,
            refunded_total_brl=0.0 if has_any_payment else None,
            refundable_total_brl=refundable if has_any_payment else None,
            raw_data=raw_data,
        )
