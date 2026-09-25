"""Shared MCP calling utility for all specialist agents.

Rules enforced here (from competition contract):
  1. case_id is ALWAYS forwarded to gateway.call().
  2. evidence_ref is NEVER modified – taken verbatim from MCP response.
  3. tool_result_consumed is emitted for EVERY successful call, with the
     real evidence_ref returned by the server.
  4. On exhausted retries a trace event is emitted WITHOUT evidence_refs
     (there is none to cite) and None is returned so the caller can fall
     back gracefully without fabricating any reference.
"""

from __future__ import annotations

import logging
from typing import Any

from ..mcp_gateway import EvidenceGateway
from ..trace import TraceWriter

logger = logging.getLogger(__name__)

MAX_RETRIES = 2  # 3 total attempts (0, 1, 2)


async def mcp_call(
    gateway: EvidenceGateway,
    tool_name: str,
    *,
    case_id: str,
    actor: str,
    trace: TraceWriter,
    **kwargs: str,
) -> dict[str, Any] | None:
    """Call an MCP tool with retry, trace, and audit compliance.

    Returns the full evidence envelope dict on success, or None on exhausted
    retries.  The caller MUST NOT substitute a fabricated evidence_ref when
    None is returned.

    Args:
        gateway:   live EvidenceGateway session.
        tool_name: exact MCP tool identifier (e.g. "get_order").
        case_id:   competition case ID, forwarded verbatim to the gateway.
        actor:     agent name for trace attribution.
        trace:     TraceWriter for the current run.
        **kwargs:  additional tool arguments (e.g. order_id="…").
    """
    last_exc: Exception | None = None

    for attempt in range(MAX_RETRIES + 1):
        try:
            # Rule 1: case_id always forwarded.
            evidence: dict[str, Any] = await gateway.call(
                tool_name, case_id=case_id, **kwargs
            )

            # Rule 2: evidence_ref taken verbatim – never modified.
            evidence_ref: str = evidence["evidence_ref"]

            # Rule 4: emit tool_result_consumed with the real server-issued ref.
            trace.emit(
                case_id=case_id,
                event_type="tool_result_consumed",
                actor=actor,
                tool_name=tool_name,
                evidence_refs=[evidence_ref],
                attributes={"attempt": attempt},
            )
            return evidence

        except RuntimeError as exc:
            last_exc = exc
            logger.warning(
                "[%s/%s] attempt %d/%d failed: %s",
                actor, tool_name, attempt + 1, MAX_RETRIES + 1, exc,
            )

    # All retries exhausted – emit audit event WITHOUT evidence_refs (none exist).
    trace.emit(
        case_id=case_id,
        event_type="tool_result_consumed",
        actor=actor,
        tool_name=tool_name,
        decision_code="MCP_EXHAUSTED",
        attributes={
            "error": str(last_exc)[:160] if last_exc else "unknown",
            "attempts": MAX_RETRIES + 1,
        },
    )
    return None
