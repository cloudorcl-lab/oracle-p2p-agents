"""
orchestrator.py — P2P Agent Orchestrator
=========================================
Runs the full source-to-pay chain in the recommended build order.
Each agent hands off IDs through the shared AgentStateStore.

Usage:
    python orchestrator.py --request sample_request.json
    python orchestrator.py --from pr5  # resume from a specific agent
    python orchestrator.py --monitor   # run PR7 gap scan only
"""

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path

from auth.oracle_auth import load_config, test_connection
from state.state_store import AgentStateStore
from agents.pr1_supplier       import PR1SupplierAgent
from agents.pr2_requisition    import PR2RequisitionAgent
from agents.pr3_sourcing       import PR3SourcingAgent
from agents.pr4_agreement      import PR4AgreementAgent
from agents.pr5_purchase_order import PR5PurchaseOrderAgent
from agents.pr6_receiving      import PR6ReceivingAgent
from agents.pr7_monitor        import PR7LifecycleMonitor
from run_report import AgentRunRecord, generate_report

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("orchestrator")


async def run_full_p2p(request: dict, transaction_id: str,
                       request_file: str = "—",
                       report_dir: Path | None = None) -> dict:
    """
    Runs: PR1 → PR2 → [PR3 if sourcing] → [PR4 if agreement] → PR5 → PR6
    PR3 and PR4 are optional — include "sourcing" or "agreement" keys in request.
    PR7 Monitor always runs at the end for gap detection.

    Writes a Markdown run report to report_dir (default: runs/ next to this file).
    """
    config     = load_config()
    results    = {}
    run_start  = time.monotonic()
    run_records: list[AgentRunRecord] = []

    first_error: Exception | None = None

    async def _run_agent(agent_class, key: str, inputs: dict) -> None:
        """
        Run one agent, capture timing + API call count.
        On failure, records the error and sets first_error so the report
        still generates, then subsequent agents are skipped.
        """
        nonlocal first_error
        if first_error is not None:
            return   # a prior agent failed — skip downstream agents

        rec   = AgentRunRecord(key)
        agent = agent_class(transaction_id, config)
        logger.info(f"=== Running {key}: {agent_class.__name__} ===")
        try:
            result = await agent.run(inputs)
            rec.complete(result, agent._api_calls)
            results[key] = result
        except Exception as exc:
            rec.fail(str(exc), agent._api_calls)
            logger.error(f"[{transaction_id}] {key} FAILED: {exc}")
            first_error = exc
        finally:
            run_records.append(rec)

    # ── PR1: Supplier Registration ────────────────────────────────────────
    if "supplier" in request:
        await _run_agent(PR1SupplierAgent, "PR1", request["supplier"])

    # ── PR2: Requisition ──────────────────────────────────────────────────
    if "requisition" in request:
        await _run_agent(PR2RequisitionAgent, "PR2", request["requisition"])

    # ── PR3: Sourcing / Negotiation (optional — competitive sourcing) ─────
    if "sourcing" in request:
        await _run_agent(PR3SourcingAgent, "PR3", request["sourcing"])

    # ── PR4: Agreement Management (optional — recurring spend / BPA) ──────
    if "agreement" in request:
        await _run_agent(PR4AgreementAgent, "PR4", request["agreement"])

    # ── PR5: Purchase Order ───────────────────────────────────────────────
    if "purchase_order" in request:
        await _run_agent(PR5PurchaseOrderAgent, "PR5", request["purchase_order"])

    # ── PR6: Receiving ────────────────────────────────────────────────────
    if "receiving" in request:
        await _run_agent(PR6ReceivingAgent, "PR6", request["receiving"])

    # ── PR7: Lifecycle Monitor (always runs — even after failure) ─────────
    # Reset first_error so PR7 runs regardless; restore afterward for re-raise
    saved_error = first_error
    first_error = None
    pr_number   = results.get("PR2", {}).get("RequisitionNumber")
    await _run_agent(PR7LifecycleMonitor, "PR7", {"pr_number": pr_number})
    first_error = saved_error

    # ── Write run report (always runs) ───────────────────────────────────
    out_dir = report_dir or (Path(__file__).parent.parent / "runs")
    try:
        report_path = generate_report(
            records=run_records,
            txn_id=transaction_id,
            request_file=request_file,
            run_started=run_start,
            output_dir=out_dir,
        )
        logger.info(f"[{transaction_id}] Report: {report_path}")
    except Exception as exc:
        logger.warning(f"[{transaction_id}] Report generation failed: {exc}")

    # Re-raise the first agent failure so callers know the run did not complete
    if first_error is not None:
        raise first_error

    return results


async def run_monitor_only(transaction_id: str) -> None:
    """PR7 gap scan across the full Oracle environment."""
    config  = load_config()
    monitor = PR7LifecycleMonitor(transaction_id, config)
    gaps    = await monitor.scan_all_gaps()

    if not gaps:
        print("\n[OK] No gaps found across P2P lifecycle")
        return

    print(f"\n[WARN] {len(gaps)} gap(s) detected:\n")
    for g in gaps:
        icon = "[HIGH]" if g["severity"] in ("CRITICAL", "HIGH") else "[MED]"
        print(f"  {icon} [{g['severity']}] {g['gap_type']}")
        print(f"     Document: {g.get('document', 'N/A')}")
        print(f"     Message:  {g['message']}")
        print(f"     Action:   {g.get('action', 'Review required')}")
        print()


def main():
    parser = argparse.ArgumentParser(description="P2P Agent Orchestrator")
    parser.add_argument("--request",    type=str, help="JSON request file path")
    parser.add_argument("--txn-id",     type=str, default="TXN-001",
                        help="Transaction ID (used for state store key)")
    parser.add_argument("--monitor",    action="store_true",
                        help="Run PR7 gap scan only")
    parser.add_argument("--test-conn",  action="store_true",
                        help="Test Oracle connection and exit")
    args = parser.parse_args()

    # ── Connection test ───────────────────────────────────────────────────
    if args.test_conn:
        config = load_config()
        ok = asyncio.run(test_connection(config))
        print("[OK] Oracle connection OK" if ok else "[FAIL] Oracle connection FAILED")
        sys.exit(0 if ok else 1)

    # ── Monitor only ──────────────────────────────────────────────────────
    if args.monitor:
        asyncio.run(run_monitor_only(args.txn_id))
        return

    # ── Full P2P run ──────────────────────────────────────────────────────
    if not args.request:
        parser.print_help()
        sys.exit(1)

    request_path = Path(args.request)
    if not request_path.exists():
        print(f"ERROR: Request file not found: {args.request}")
        sys.exit(1)

    request = json.loads(request_path.read_text())

    print(f"\n[START] Starting P2P agents -- Transaction: {args.txn_id}\n")
    run_failed = False
    try:
        results = asyncio.run(run_full_p2p(request, args.txn_id,
                                            request_file=str(request_path.resolve())))
    except Exception as exc:
        run_failed = True
        results    = {}
        print(f"\n[FAIL] P2P run stopped: {exc}\n")

    status_label = "[DONE]" if not run_failed else "[PARTIAL]"
    print(f"\n{status_label} P2P run complete\n")
    print(f"  Report: runs/p2p_run_{args.txn_id}_*.md\n")
    for agent, output in results.items():
        if agent == "PR7":
            gaps = output.get("gap_count", 0)
            print(f"  {agent}: {gaps} gap(s) detected")
        elif agent == "PR3":
            neg_num = output.get("NegotiationNumber", "—")
            awards  = len(output.get("awards", []))
            print(f"  {agent}: {neg_num} ({awards} award(s))")
        elif agent == "PR4":
            agr_num = output.get("AgreementNumber", "—")
            status  = output.get("AgreementStatusCode", "—")
            print(f"  {agent}: {agr_num} ({status})")
        else:
            key_id = (output.get("RequisitionNumber")
                      or output.get("OrderNumber")
                      or output.get("ReceiptNumber")
                      or output.get("SupplierId", "—"))
            print(f"  {agent}: {key_id}")


if __name__ == "__main__":
    main()
