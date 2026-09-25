"""LLM client wrapper for GPT-4o structured output.

Competition safety rules enforced here:
  - LLM NEVER generates evidence_ref (hard gate – must come from MCP only)
  - LLM output is constrained to JSON schema via response_format=json_object
  - Financial amounts are passed in as grounded numbers from MCP evidence
  - Temperature = 0 for determinism and calibration score
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from openai import AsyncOpenAI

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

_client: AsyncOpenAI | None = None


def get_client() -> AsyncOpenAI:
    """Lazy singleton – reads OPENAI_API_KEY from env."""
    global _client
    if _client is None:
        load_dotenv()
        api_key = os.environ.get("OPENAI_API_KEY", "")
        if not api_key or api_key.startswith("sk-..."):
            raise RuntimeError(
                "OPENAI_API_KEY not set in .env. "
                "Add: OPENAI_API_KEY=sk-your-real-key"
            )
        base_url = os.environ.get("OPENAI_BASE_URL", "").strip() or None
        _client = AsyncOpenAI(api_key=api_key, base_url=base_url)
    return _client


def get_model() -> str:
    return os.environ.get("OPENAI_MODEL", "gpt-4o")


# ---------------------------------------------------------------------------
# System prompt – instructs LLM on the competition rules and schema constraints
# ---------------------------------------------------------------------------
_SYSTEM_PROMPT = """\
You are an expert e-commerce dispute resolution agent. You will receive structured \
evidence collected from an authoritative MCP Evidence Gateway about a customer dispute.

Your task: analyse the evidence and produce a JSON decision object.

CRITICAL RULES (violations cause score = 0):
1. NEVER invent or modify evidence_ref values. Do NOT include any evidence_ref field in your output.
   Evidence refs are supplied separately from MCP and must not be fabricated.
2. All field values must exactly match the allowed enum values listed below.
3. Financial amounts must be derived from the payment evidence provided; do not guess amounts.
4. Confidence must reflect actual evidence quality: never use 1.0, never below 0.10.
5. data_conflicts: if there is a conflict, 'sources' MUST contain at least 2 distinct source names. If no conflict, data_conflicts MUST be [].

Return ONLY a valid JSON object with these exact fields:
{
  "primary_issue": "<one of the enum values below>",
  "case_status": "<action_required|no_action|needs_investigation>",
  "confidence": <float 0.10-0.95>,
  "ranked_causes": [{"cause_code": "<UPPERCASE_CODE>", "rank": 1}],
  "responsible_party_type": "<seller|platform|logistics_provider|payment_provider|customer|unknown>",
  "resolution_actions": ["<action_string_max_80_chars>", ...],
  "data_conflicts": [{"field": "...", "sources": ["...", "..."], "selected_source": "...", "resolution_code": "..."}],
  "reasoning_summary": "<one sentence, max 160 chars, no chain-of-thought>"
}

primary_issue allowed values:
  canceled_order_paid, unavailable_order_paid, late_delivery_seller,
  late_delivery_logistics, valid_split_payment, payment_mismatch,
  duplicate_charge, refund_pending, refund_failed,
  unsupported_claim, insufficient_evidence

case_status rules:
  - action_required: clear violation requiring refund or penalty
  - no_action: legitimate transaction, no intervention needed
  - needs_investigation: contradictory or missing evidence

Confidence calibration:
  - 0.85-0.95: clear unambiguous evidence
  - 0.70-0.84: mostly clear but minor gaps
  - 0.50-0.69: some conflict or missing data
  - 0.30-0.49: significant uncertainty
  - 0.10-0.29: very little usable evidence

Resolution actions (use concise English imperative phrases, max 80 chars each):
  Examples: "issue_full_refund", "notify_customer", "penalise_seller_sla_breach",
  "reverse_duplicate_capture", "retry_refund", "escalate_to_logistics_provider",
  "reconcile_payment", "escalate_to_finance", "manual_review"
"""


async def llm_decide(
    case: dict[str, Any],
    order_data: dict[str, Any],
    items_data: list[dict[str, Any]],
    payments_data: dict[str, list[dict[str, Any]]],
    shipment_data: dict[str, Any],
    sellers_data: dict[str, Any],
    policy_data: dict[str, Any],
    order_status: str,
    payment_verdict: str,
    shipment_verdict: str,
    captured_total_brl: float | None,
    refundable_total_brl: float | None,
) -> dict[str, Any]:
    """Call GPT-4o to make the dispute resolution decision.

    Passes all MCP-sourced evidence as context.
    Returns the parsed LLM JSON decision.
    """
    client = get_client()
    model = get_model()

    # Build a structured evidence summary for the LLM
    evidence_context = {
        "case_id": case["case_id"],
        "customer_request": case["customer_request"],
        "policy_version": case.get("policy_version"),
        "order": order_data,
        "order_items": items_data,
        "payments": payments_data,
        "shipment": shipment_data,
        "sellers": sellers_data,
        "policy_rules": policy_data,
        "derived_signals": {
            "order_status": order_status,
            "payment_verdict": payment_verdict,
            "shipment_verdict": shipment_verdict,
            "captured_total_brl": captured_total_brl,
            "refundable_total_brl": refundable_total_brl,
        },
    }

    user_prompt = (
        "Here is the complete evidence for this dispute case. "
        "Analyse it and return your JSON decision:\n\n"
        + json.dumps(evidence_context, ensure_ascii=False, indent=2)
    )

    logger.info("[llm] calling %s for case %s", model, case["case_id"])

    response = await client.chat.completions.create(
        model=model,
        temperature=0,          # deterministic for calibration score
        max_tokens=800,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
    )

    raw = response.choices[0].message.content or "{}"
    try:
        decision: dict[str, Any] = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.error("[llm] JSON parse error: %s — raw: %s", exc, raw[:200])
        raise RuntimeError(f"LLM returned invalid JSON: {exc}") from exc

    logger.info(
        "[llm] %s → primary_issue=%s confidence=%.2f",
        case["case_id"],
        decision.get("primary_issue"),
        decision.get("confidence", 0),
    )
    return decision
