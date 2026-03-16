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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("orchestrator")


async def run_full_p2p(request: dict, transaction_id: str) -> dict:
    """
    Runs: PR1 → PR2 → [PR3 if sourcing] → [PR4 if agreement] → PR5 → PR6
    PR3 and PR4 are optional — include "sourcing" or "agreement" keys in request.
    PR7 Monitor always runs at the end for gap detection.
    """
    config = load_config()
    results = {}

    # ── PR1: Supplier Registration ────────────────────────────────────────
    if "supplier" in request:
        logger.info("=== Running PR1: Supplier Registration ===")
        agent   = PR1SupplierAgent(transaction_id, config)
        results["PR1"] = await agent.run(request["supplier"])

    # ── PR2: Requisition ──────────────────────────────────────────────────
    if "requisition" in request:
        logger.info("=== Running PR2: Requisition ===")
        agent   = PR2RequisitionAgent(transaction_id, config)
        results["PR2"] = await agent.run(request["requisition"])

    # ── PR3: Sourcing / Negotiation (optional — competitive sourcing) ─────
    if "sourcing" in request:
        logger.info("=== Running PR3: Sourcing / Negotiation ===")
        agent   = PR3SourcingAgent(transaction_id, config)
        results["PR3"] = await agent.run(request["sourcing"])

    # ── PR4: Agreement Management (optional — recurring spend / BPA) ──────
    if "agreement" in request:
        logger.info("=== Running PR4: Agreement Management ===")
        agent   = PR4AgreementAgent(transaction_id, config)
        results["PR4"] = await agent.run(request["agreement"])

    # ── PR5: Purchase Order ───────────────────────────────────────────────
    if "purchase_order" in request:
        logger.info("=== Running PR5: Purchase Order ===")
        agent   = PR5PurchaseOrderAgent(transaction_id, config)
        results["PR5"] = await agent.run(request["purchase_order"])

    # ── PR6: Receiving ────────────────────────────────────────────────────
    if "receiving" in request:
        logger.info("=== Running PR6: Receiving ===")
        agent   = PR6ReceivingAgent(transaction_id, config)
        results["PR6"] = await agent.run(request["receiving"])

    # ── PR7: Lifecycle Monitor (always runs at end) ───────────────────────
    logger.info("=== Running PR7: Lifecycle Monitor ===")
    monitor    = PR7LifecycleMonitor(transaction_id, config)
    pr_number  = results.get("PR2", {}).get("RequisitionNumber")
    results["PR7"] = await monitor.run({"pr_number": pr_number})

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
    results = asyncio.run(run_full_p2p(request, args.txn_id))

    print("\n[DONE] P2P run complete\n")
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
