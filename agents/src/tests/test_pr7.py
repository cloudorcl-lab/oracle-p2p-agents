"""
test_pr7.py — Unit tests for PR7LifecycleMonitor
=================================================
All external I/O mocked. Uses unittest + unittest.mock only.
"""

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def run(coro):
    return asyncio.run(coro)


class TestPR7LifecycleMonitor(unittest.TestCase):

    def setUp(self):
        # Patch all external dependencies before importing agent
        self.config_patcher = patch("auth.oracle_auth.load_config", return_value=MagicMock())
        self.config_patcher.start()
        self.store_patcher = patch("state.state_store.AgentStateStore")
        MockStore = self.store_patcher.start()
        self.mock_store = MockStore.return_value
        self.mock_store.get = AsyncMock(return_value=None)
        self.mock_store.set = AsyncMock()
        self.mock_store.set_many = AsyncMock()

        from agents.pr7_monitor import PR7LifecycleMonitor
        self.agent = PR7LifecycleMonitor(transaction_id="TEST-PR7-001")
        self.agent.get   = AsyncMock()
        self.agent.audit = AsyncMock()
        self.agent.store = self.mock_store

    def tearDown(self):
        self.config_patcher.stop()
        self.store_patcher.stop()

    # ── Happy path — no gaps ──────────────────────────────────────────────

    def test_happy_path_no_gaps(self):
        """run() with approved PR and PO, receipt present → 0 gaps."""
        today = _utcnow().strftime("%Y-%m-%d")

        pr = {
            "RequisitionHeaderId": 1001,
            "Requisition":         "REQ-001",
            "DocumentStatus":      "APPROVED",
            "ApprovedDate":        today,
            "LastUpdateDate":      today,
            "PreparerEmail":       "buyer@co.com",
        }
        po = {
            "POHeaderId":        2001,
            "OrderNumber":       "PO-001",
            "POHeaderStatusCode": "APPROVED",
            "NeedByDate":        (_utcnow() + timedelta(days=10)).strftime("%Y-%m-%d"),
            "LastUpdateDate":    today,
        }
        receipt = {
            "ReceiptHeaderId": 3001,
            "ReceiptNumber":   "RCV-001",
            "ReceiptDate":     today,
        }

        self.agent.get.side_effect = [
            {"items": [pr]},      # _get_pr
            {"items": [po]},      # _get_po
            {"items": [receipt]}, # _get_receipt
        ]
        self.mock_store.get.side_effect = [None, 1001, 2001]

        report = run(self.agent.run({"pr_number": "REQ-001"}))

        self.assertEqual(report["gap_count"], 0)
        self.assertEqual(report["critical_count"], 0)
        self.assertIn("generated_at", report)

    # ── Gap: PR stuck in approval beyond SLA ─────────────────────────────

    def test_pr_stuck_in_approval_gap_detected(self):
        """PR in PENDING_APPROVAL for > SLA days → MEDIUM gap."""
        stale = (_utcnow() - timedelta(days=5)).strftime("%Y-%m-%d")
        pr = {
            "Requisition":    "REQ-STUCK",
            "DocumentStatus": "PENDING_APPROVAL",
            "LastUpdateDate": stale,
            "PreparerEmail":  "buyer@co.com",
        }
        self.agent.get.side_effect = [
            {"items": [pr]},
            {"items": []},  # no PO
        ]
        self.mock_store.get.side_effect = [None, None, None]

        report = run(self.agent.run({"pr_number": "REQ-STUCK"}))

        self.assertEqual(report["gap_count"], 1)
        gap = report["gaps"][0]
        self.assertEqual(gap["gap_type"], "PR_STUCK_IN_APPROVAL")
        self.assertEqual(gap["severity"], "MEDIUM")

    # ── Gap: PO delivery overdue ──────────────────────────────────────────

    def test_po_delivery_overdue_gap_detected(self):
        """PO with NeedByDate in past → HIGH gap."""
        today = _utcnow().strftime("%Y-%m-%d")
        overdue = (_utcnow() - timedelta(days=3)).strftime("%Y-%m-%d")
        pr = {
            "Requisition":    "REQ-002",
            "DocumentStatus": "APPROVED",
            "ApprovedDate":   today,
            "LastUpdateDate": today,
        }
        po = {
            "POHeaderId":        2002,
            "OrderNumber":       "PO-002",
            "POHeaderStatusCode": "COMMUNICATED",
            "NeedByDate":        overdue,
            "LastUpdateDate":    today,
        }
        self.agent.get.side_effect = [
            {"items": [pr]},
            {"items": [po]},
            {"items": []},  # no receipt yet
        ]
        # store.get calls: (1) RequisitionHeaderId→1002, (2) POHeaderId→2002
        self.mock_store.get.side_effect = [1002, 2002]

        report = run(self.agent.run({"pr_number": "REQ-002"}))

        po_gaps = [g for g in report["gaps"] if g["gap_type"] == "PO_DELIVERY_OVERDUE"]
        self.assertEqual(len(po_gaps), 1)
        self.assertEqual(po_gaps[0]["severity"], "HIGH")

    # ── Gap: Receipt with no invoice (3-way match stalled) ───────────────

    def test_receipt_no_invoice_gap_detected(self):
        """Receipt older than RECEIPT_TO_INVOICE_SLA_DAYS → HIGH gap."""
        old_date = (_utcnow() - timedelta(days=10)).strftime("%Y-%m-%d")
        today = _utcnow().strftime("%Y-%m-%d")
        pr = {
            "Requisition": "REQ-003", "DocumentStatus": "APPROVED",
            "ApprovedDate": today, "LastUpdateDate": today,
        }
        po = {
            "POHeaderId": 2003, "OrderNumber": "PO-003",
            "POHeaderStatusCode": "PARTIALLY_RECEIVED",
            "NeedByDate": (_utcnow() + timedelta(days=5)).strftime("%Y-%m-%d"),
            "LastUpdateDate": today,
        }
        receipt = {
            "ReceiptHeaderId": 3003,
            "ReceiptNumber":   "RCV-003",
            "ReceiptDate":     old_date,
        }
        self.agent.get.side_effect = [
            {"items": [pr]},
            {"items": [po]},
            {"items": [receipt]},
        ]
        # store.get calls: (1) RequisitionHeaderId→1003, (2) POHeaderId→2003
        self.mock_store.get.side_effect = [1003, 2003]

        report = run(self.agent.run({"pr_number": "REQ-003"}))

        inv_gaps = [g for g in report["gaps"] if g["gap_type"] == "RECEIPT_NO_INVOICE"]
        self.assertEqual(len(inv_gaps), 1)
        self.assertEqual(inv_gaps[0]["severity"], "HIGH")

    # ── days_since boundary ───────────────────────────────────────────────

    def test_days_since_today_returns_zero(self):
        from agents.pr7_monitor import PR7LifecycleMonitor
        today = _utcnow().strftime("%Y-%m-%d")
        self.assertEqual(PR7LifecycleMonitor._days_since(today), 0)

    def test_days_since_empty_returns_zero(self):
        from agents.pr7_monitor import PR7LifecycleMonitor
        self.assertEqual(PR7LifecycleMonitor._days_since(""), 0)

    def test_days_overdue_future_returns_zero(self):
        from agents.pr7_monitor import PR7LifecycleMonitor
        future = (_utcnow() + timedelta(days=5)).strftime("%Y-%m-%d")
        self.assertEqual(PR7LifecycleMonitor._days_overdue(future), 0)

    # ── scan_all_gaps returns sorted list ─────────────────────────────────

    def test_scan_all_gaps_returns_list(self):
        """scan_all_gaps() aggregates all bulk scan methods."""
        today = _utcnow().strftime("%Y-%m-%d")
        self.agent.get.return_value = {"items": []}

        gaps = run(self.agent.scan_all_gaps())

        self.assertIsInstance(gaps, list)


if __name__ == "__main__":
    unittest.main()
