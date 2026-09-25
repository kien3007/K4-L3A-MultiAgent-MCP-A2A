"""ShipmentAgent – traces shipment timeline evidence via MCP.

MCP tool permissions: get_shipment_summary  (no others)

Actual MCP response shape (probed from live server):
  get_shipment_summary: data = {
    order_id, order_status,
    delivered_carrier_at,    # ISO datetime or null
    delivered_customer_at,   # ISO datetime or null
    estimated_delivery_at,   # ISO datetime or null
    shipping_limits: list[{seller_id, shipping_limit_date}],
    events: list[{status, timestamp}]
  }
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from ..mcp_gateway import EvidenceGateway
from ..trace import TraceWriter
from .messages import OrderItemResult, ShipmentResult
from .mcp_utils import mcp_call

logger = logging.getLogger(__name__)

AGENT_NAME = "shipment-agent"


def _parse_dt(value: str | None) -> datetime | None:
    """Parse ISO 8601 datetime string, return None on failure."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class ShipmentAgent:
    """Specialist responsible for shipment timeline evidence."""

    async def run(
        self,
        case: dict[str, Any],
        order_result: OrderItemResult,
        gateway: EvidenceGateway,
        trace: TraceWriter,
    ) -> ShipmentResult:
        case_id: str = case["case_id"]
        claimed_order_id = case["customer_request"]["claimed_order_id"]

        trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target=AGENT_NAME,
            attributes={"order_ids": ",".join(order_result.order_ids)},
        )

        evidence_refs: list[str] = []
        shipment_ids: list[str] = []
        late_seller_ids: list[str] = []
        verdict = "insufficient_evidence"
        timeline_complete = False
        raw_data: dict[str, Any] = {}
        verdicts_seen: list[str] = []

        orders_to_check = list(dict.fromkeys(
            [claimed_order_id] + order_result.order_ids
        ))

        for order_id in orders_to_check:
            ship_ev = await mcp_call(
                gateway, "get_shipment_summary",
                case_id=case_id, actor=AGENT_NAME, trace=trace,
                order_id=order_id,
            )
            if ship_ev is None:
                continue

            evidence_refs.append(ship_ev["evidence_ref"])
            ship_data: dict[str, Any] = ship_ev.get("data", {})
            raw_data.setdefault("shipments", {})[order_id] = ship_data

            # Shipment summary uses the order_id as the shipment identifier
            shipment_ids.append(order_id)

            delivered_carrier = _parse_dt(ship_data.get("delivered_carrier_at"))
            delivered_customer = _parse_dt(ship_data.get("delivered_customer_at"))
            estimated = _parse_dt(ship_data.get("estimated_delivery_at"))

            if delivered_carrier:
                timeline_complete = True

            # Check each seller's shipping limit against carrier handoff
            shipping_limits: list[dict[str, Any]] = ship_data.get("shipping_limits", [])
            seller_delayed = False
            for limit_entry in shipping_limits:
                limit_dt = _parse_dt(limit_entry.get("shipping_limit_date"))
                seller_id = limit_entry.get("seller_id")
                if limit_dt and delivered_carrier and delivered_carrier > limit_dt:
                    seller_delayed = True
                    if seller_id:
                        late_seller_ids.append(seller_id)

            # Determine verdict from evidence
            if seller_delayed:
                this_verdict = "seller_delay"
            elif delivered_customer and estimated and delivered_customer > estimated:
                # Delivered after estimated date but seller was on time → logistics
                this_verdict = "logistics_delay"
            elif delivered_customer:
                this_verdict = "on_time"
            elif delivered_carrier:
                # Carrier received but not yet delivered to customer
                this_verdict = "insufficient_evidence"
            else:
                this_verdict = "insufficient_evidence"

            verdicts_seen.append(this_verdict)

        # Consolidate
        if not verdicts_seen:
            verdict = "insufficient_evidence"
        elif len(set(verdicts_seen)) == 1:
            verdict = verdicts_seen[0]
        else:
            verdict = "conflicting"

        trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor=AGENT_NAME,
            target="coordinator",
            decision_code="SHIPMENT_DONE",
            evidence_refs=evidence_refs or None,
            attributes={"verdict": verdict, "timeline_complete": timeline_complete},
        )

        return ShipmentResult(
            shipment_ids=list(dict.fromkeys(shipment_ids)),
            evidence_refs=list(dict.fromkeys(evidence_refs)),
            verdict=verdict,
            late_seller_ids=list(dict.fromkeys(late_seller_ids)),
            timeline_complete=timeline_complete,
            raw_data=raw_data,
        )
