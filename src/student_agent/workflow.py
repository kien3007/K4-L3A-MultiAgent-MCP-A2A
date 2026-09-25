"""L3A workflow entry-point – delegates to CoordinatorAgent."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .agents.coordinator import CoordinatorAgent
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter


def make_coordinator(repo_root: Path) -> CoordinatorAgent:
    """Factory so CLI can build a coordinator with the correct repo root."""
    return CoordinatorAgent(repo_root)


async def solve_case(
    case: dict[str, Any],
    gateway: EvidenceGateway,
    trace: TraceWriter,
    *,
    coordinator: CoordinatorAgent,
) -> dict[str, Any]:
    """Coordinate all specialist agents and return a schema-valid L3A output dict."""
    return await coordinator.run(case, gateway, trace)
