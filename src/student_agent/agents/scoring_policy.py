"""ScoringPolicy – loads and exposes scoring-policy-v2.json at runtime.

Used by PolicyAgent and VerifierAgent to make decisions aligned with
the actual competition scoring weights, hard gates, and required events.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class ScoringPolicy:
    """Parsed view of contracts/scoring/scoring-policy-v2.json."""

    def __init__(self, root: Path) -> None:
        path = root / "contracts" / "scoring" / "scoring-policy-v2.json"
        self._data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))

    # ── Scoring weights (L3A) ──────────────────────────────────────────────
    @property
    def weights(self) -> dict[str, float]:
        return self._data["variant_weights"]["l3a"]

    # ── Required workflow events ────────────────────────────────────────────
    @property
    def required_events(self) -> list[str]:
        return self._data["workflow_required_events"]

    # ── Hard gates (violations → score 0) ──────────────────────────────────
    @property
    def hard_gates(self) -> list[str]:
        return self._data["hard_gates"]

    # ── Consistency rules (derived from scoring description) ───────────────
    # These are used by VerifierAgent for cross-field consistency checks.
    RESPONSIBILITY_MAP: dict[str, set[str]] = {
        "canceled_order_paid":     {"platform", "seller"},
        "unavailable_order_paid":  {"seller"},
        "late_delivery_seller":    {"seller"},
        "late_delivery_logistics": {"logistics_provider"},
        "payment_mismatch":        {"payment_provider"},
        "duplicate_charge":        {"payment_provider"},
        "refund_pending":          {"payment_provider", "platform"},
        "refund_failed":           {"payment_provider", "platform"},
        "valid_split_payment":     {"customer", "platform"},
        "unsupported_claim":       {"customer"},
        "insufficient_evidence":   {"unknown", "platform"},
    }

    # Issues that REQUIRE a non-zero refund when case_status=action_required
    REFUND_REQUIRED_ISSUES: frozenset[str] = frozenset({
        "canceled_order_paid",
        "unavailable_order_paid",
        "duplicate_charge",
        "refund_pending",
        "refund_failed",
        "payment_mismatch",
    })

    # Issues where logistics_provider should NOT be the refund entity
    LOGISTICS_NOT_REFUND_PARTY: frozenset[str] = frozenset({
        "late_delivery_seller",
        "late_delivery_logistics",
    })

    # Confidence calibration thresholds based on evidence completeness
    @staticmethod
    def calibrate_confidence(
        base_confidence: float,
        evidence_refs_count: int,
        has_conflicts: bool,
        has_mcp_failures: bool,
        has_sufficient_evidence: bool,
    ) -> float:
        """Calibrate confidence score to avoid over/under-confidence.

        Scoring formula: 1 - (correctness - confidence)²
        Best calibration = confidence matches actual correctness probability.

        Args:
            base_confidence: Initial confidence from rule match.
            evidence_refs_count: Number of real evidence refs collected.
            has_conflicts: True if data_conflicts is non-empty.
            has_mcp_failures: True if any MCP call returned None.
            has_sufficient_evidence: True if all required domains covered.
        """
        conf = base_confidence

        # Penalise for insufficient evidence coverage
        if not has_sufficient_evidence:
            conf -= 0.20
        if evidence_refs_count == 0:
            conf -= 0.30
        elif evidence_refs_count < 2:
            conf -= 0.10

        # Penalise for data conflicts (contradictory signals)
        if has_conflicts:
            conf -= 0.10

        # Penalise for MCP failures (evidence gaps)
        if has_mcp_failures:
            conf -= 0.10

        # Never over-claim certainty with conflicting data
        if has_conflicts and conf > 0.80:
            conf = 0.80

        # Never under 0.10 (we at least read the case)
        return round(max(0.10, min(0.95, conf)), 4)
