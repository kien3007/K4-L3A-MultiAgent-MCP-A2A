"""Shared dataclasses for inter-agent handoff messages (A2A protocol)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class OrderItemResult:
    """Output produced by OrderItemAgent after analysing order & item evidence."""

    order_ids: list[str]
    item_ids: list[str]
    seller_ids: list[str]
    evidence_refs: list[str]
    order_status: str  # e.g. "canceled", "delivered", "pending"
    item_status: str   # e.g. "unavailable", "available"
    raw_data: dict[str, Any] = field(default_factory=dict)


@dataclass
class PaymentResult:
    """Output produced by PaymentAgent after reconciling payments."""

    payment_references: list[str]
    evidence_refs: list[str]
    verdict: str  # reconciled | capture_mismatch | duplicate_capture | refund_pending | refund_failed | refunded | insufficient_evidence
    captured_total_brl: float | None
    refunded_total_brl: float | None
    refundable_total_brl: float | None
    raw_data: dict[str, Any] = field(default_factory=dict)


@dataclass
class ShipmentResult:
    """Output produced by ShipmentAgent after tracing shipment timeline."""

    shipment_ids: list[str]
    evidence_refs: list[str]
    verdict: str  # on_time | seller_delay | logistics_delay | lost | returned | conflicting | insufficient_evidence
    late_seller_ids: list[str]
    timeline_complete: bool
    raw_data: dict[str, Any] = field(default_factory=dict)


@dataclass
class PolicyResult:
    """Output produced by PolicyAgent after applying policy rules."""

    policy_version: str
    primary_issue: str
    case_status: str       # action_required | no_action | needs_investigation
    confidence: float
    ranked_causes: list[dict[str, Any]]
    responsible_parties: list[dict[str, Any]]
    recommended_refund_brl: float
    refund_lines: list[dict[str, Any]]
    resolution_actions: list[str]
    data_conflicts: list[dict[str, Any]]
    evidence_refs: list[str]
    policy_ref: str | None = None
    seller_evidence_refs: list[str] = field(default_factory=list)
    raw_data: dict[str, Any] = field(default_factory=dict)
