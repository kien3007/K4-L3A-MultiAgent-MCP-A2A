from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .cases import load_case_set
from .config import Settings
from .contracts import Contracts
from .mcp_gateway import connect_gateway
from .submission import package_submission, validate_artifacts
from .trace import TraceWriter
from .workflow import make_coordinator, solve_case


def _root(value: str) -> Path:
    return Path(value).resolve()


async def _show_tools(root: Path) -> None:
    settings = Settings.load(root)
    contracts = Contracts(root / "contracts" / "schemas")
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        for tool in await gateway.list_tools():
            print(tool)


async def _run(root: Path) -> None:
    settings = Settings.load(root)
    case_set = load_case_set(root)
    contracts = Contracts(root / "contracts" / "schemas")
    output_root = root / "outputs"
    trace_path = root / "traces" / "trace.jsonl"
    output_root.mkdir(parents=True, exist_ok=True)
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    trace = TraceWriter(trace_path, contracts)

    # Build coordinator once (loads ScoringPolicy from repo_root).
    coordinator = make_coordinator(root)

    gateway_cm = None
    gateway = None

    async def get_gateway():
        nonlocal gateway, gateway_cm
        if gateway_cm is not None:
            try:
                await gateway_cm.__aexit__(None, None, None)
            except Exception:
                pass
        gateway_cm = connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts)
        gateway = await gateway_cm.__aenter__()
        return gateway

    try:
        gw = await get_gateway()
        discovered_tools = await gw.list_tools()
        if not discovered_tools:
            raise RuntimeError("MCP Gateway returned no tools")

        total = len(case_set.case_ids)
        for idx, case_id in enumerate(case_set.case_ids, 1):
            target = output_root / f"{case_id}.json"
            if target.exists():
                try:
                    with open(target, encoding="utf-8") as fp:
                        existing = json.load(fp)
                    contracts.validate_output(existing, f"outputs/{case_id}.json")
                    issue = existing["assessment"]["primary_issue"]
                    rf = existing["financial_resolution"]["recommended_refund_brl"]
                    print(f"[{idx:3d}/{total}] {case_id} [CACHED] -> {issue:23s} (refund={rf} BRL)")
                    continue
                except Exception:
                    target.unlink(missing_ok=True)

            case = case_set.cases[case_id]
            for attempt in range(5):
                try:
                    # Lifecycle event 1: case_received (required by scoring-policy-v2)
                    trace.emit(case_id=case_id, event_type="case_received", actor="coordinator")
                    output = await solve_case(case, gw, trace, coordinator=coordinator)
                    contracts.validate_output(output, f"outputs/{case_id}.json")
                    if output.get("case_id") != case_id:
                        raise ValueError(f"solver returned a mismatched case_id for {case_id}")
                    temporary = target.with_suffix(".json.tmp")
                    temporary.write_text(
                        json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                    )
                    temporary.replace(target)
                    # Lifecycle event 7: case_finalized (required by scoring-policy-v2)
                    trace.emit(case_id=case_id, event_type="case_finalized", actor="coordinator")
                    
                    issue = output["assessment"]["primary_issue"]
                    rf = output["financial_resolution"]["recommended_refund_brl"]
                    print(f"[{idx:3d}/{total}] {case_id} -> {issue:23s} (refund={rf} BRL)")
                    break
                except BaseException as exc:
                    if attempt < 4:
                        print(f"[{idx:3d}/{total}] {case_id} retry ({attempt+1}/4) after: {exc} — reconnecting gateway...")
                        await asyncio.sleep(3.0)
                        try:
                            gw = await get_gateway()
                        except BaseException as conn_err:
                            print(f"Reconnect failed: {conn_err}, waiting 5s...")
                            await asyncio.sleep(5.0)
                            gw = await get_gateway()
                    else:
                        raise
    finally:
        if gateway_cm is not None:
            try:
                await gateway_cm.__aexit__(None, None, None)
            except Exception:
                pass


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Day09 L3A student workflow")
    result.add_argument("--root", default=".", help="repository root (default: current directory)")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("validate-inputs", help="validate case-set.json and all 100 inputs")
    commands.add_parser("mcp-tools", help="authenticate and list discovered MCP tools")
    commands.add_parser("run", help="run the implemented workflow for all cases")
    commands.add_parser("validate", help="validate outputs and observable trace")
    package = commands.add_parser("package", help="validate and build the submission ZIP")
    package.add_argument("--output", default="dist/submission.zip")
    return result


def main() -> None:
    args = parser().parse_args()
    root = _root(args.root)
    try:
        if args.command == "validate-inputs":
            case_set = load_case_set(root)
            print(
                f"OK: {case_set.variant_id} / {case_set.version} / "
                f"{len(case_set.case_ids)} cases"
            )
        elif args.command == "mcp-tools":
            asyncio.run(_show_tools(root))
        elif args.command == "run":
            asyncio.run(_run(root))
        elif args.command == "validate":
            case_set = load_case_set(root)
            contracts = Contracts(root / "contracts" / "schemas")
            _, trace = validate_artifacts(root, case_set, contracts)
            print(f"OK: {len(case_set.case_ids)} outputs / {len(trace)} trace events")
        elif args.command == "package":
            destination = package_submission(root, root / args.output)
            print(f"OK: {destination}")
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
