"""OrderItemAgent – collects & analyses order and item evidence via MCP.

MCP tool permissions: get_order, get_order_items  (no others)

Actual MCP response shapes (probed from live server):
  get_order:       data = {order_id, customer_id, order_status, order_purchase_timestamp,
                           order_approved_at, order_delivered_carrier_date,
                           order_delivered_customer_date, order_estimated_delivery_date}
  get_order_items: data = list[{order_id, order_item_id, product_id, seller_id,
                                shipping_limit_date, price, freight_value}]
"""

from __future__ import annotations

import logging
from typing import Any

from ..mcp_gateway import EvidenceGateway
from ..trace import TraceWriter
from .messages import OrderItemResult
from .mcp_utils import mcp_call

logger = logging.getLogger(__name__)

AGENT_NAME = "order-item-agent"


class OrderItemAgent:
    """Specialist responsible for order status and item availability evidence."""

    async def run(
        self,
        case: dict[str, Any],
        gateway: EvidenceGateway,
        trace: TraceWriter,
    ) -> OrderItemResult:
        case_id: str = case["case_id"]
        claimed_order_id: str = case["customer_request"]["claimed_order_id"]

        trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target=AGENT_NAME,
            attributes={"claimed_order_id": claimed_order_id},
        )

        evidence_refs: list[str] = []
        order_ids: list[str] = []
        item_ids: list[str] = []
        seller_ids: list[str] = []
        order_status = "unknown"
        raw_data: dict[str, Any] = {}

        # ── get_order ──────────────────────────────────────────────────────
        order_ev = await mcp_call(
            gateway, "get_order",
            case_id=case_id, actor=AGENT_NAME, trace=trace,
            order_id=claimed_order_id,
        )
        if order_ev is not None:
            evidence_refs.append(order_ev["evidence_ref"])
            order_data: dict[str, Any] = order_ev.get("data", {})
            raw_data["order"] = order_data
            # Field name from MCP is "order_status" (not "status")
            order_status = order_data.get("order_status", "unknown")
            if oid := order_data.get("order_id"):
                order_ids.append(oid)

        # ── get_order_items ────────────────────────────────────────────────
        # Returns a list of items; each item has seller_id.
        items_ev = await mcp_call(
            gateway, "get_order_items",
            case_id=case_id, actor=AGENT_NAME, trace=trace,
            order_id=claimed_order_id,
        )
        if items_ev is not None:
            evidence_refs.append(items_ev["evidence_ref"])
            items_data: list[dict[str, Any]] = items_ev.get("data", [])
            raw_data["items"] = items_data
            for item in items_data:
                if iid := item.get("product_id"):
                    item_ids.append(iid)
                if sid := item.get("seller_id"):
                    seller_ids.append(sid)

        trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor=AGENT_NAME,
            target="coordinator",
            decision_code="ORDER_ITEM_DONE",
            evidence_refs=evidence_refs or None,
            attributes={"order_status": order_status},
        )

        return OrderItemResult(
            order_ids=list(dict.fromkeys(order_ids)),
            item_ids=list(dict.fromkeys(item_ids)),
            seller_ids=list(dict.fromkeys(seller_ids)),
            evidence_refs=list(dict.fromkeys(evidence_refs)),
            order_status=order_status,
            item_status="unknown",  # items tool doesn't return availability status
            raw_data=raw_data,
        )
