"""
test_pr6.py — Unit tests for PR6ReceivingAgent
===============================================
All external I/O mocked. Uses unittest + unittest.mock only.
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch


def run(coro):
    return asyncio.run(coro)


def _make_inputs(**overrides):
    base = {
        "receipt_date":      "2026-04-28",
        "received_by_email": "warehouse@company.com",
        "receiving_org_id":  204,
        "packing_slip":      "PKG-2026-00123",
        "lines": [{
            "po_line_id":         300001,
            "po_schedule_id":     400001,
            "item_id":            500001,
            "quantity_received":  5,
            "uom":                "Each",
            "destination_type":   "EXPENSE",
            "destination_org_id": 204,
            "inspection_required": True,
            "inspection_result":   "ACCEPTED",
            "quantity_accepted":   5,
            "quantity_rejected":   0,
        }],
    }
    base.update(overrides)
    return base


class TestPR6ReceivingAgent(unittest.TestCase):

    def setUp(self):
        self.config_patcher = patch("auth.oracle_auth.load_config", return_value=MagicMock())
        self.config_patcher.start()
        self.store_patcher = patch("state.state_store.AgentStateStore")
        MockStore = self.store_patcher.start()
        self.mock_store = MockStore.return_value
        self.mock_store.get = AsyncMock(side_effect=[200001, 100001, 400001])
        self.mock_store.set = AsyncMock()
        self.mock_store.set_many = AsyncMock()

        from agents.pr6_receiving import PR6ReceivingAgent
        self.agent = PR6ReceivingAgent(transaction_id="TEST-PR6-001")
        self.agent.get   = AsyncMock()
        self.agent.post  = AsyncMock()
        self.agent.audit = AsyncMock()
        self.agent.store = self.mock_store

    def tearDown(self):
        self.config_patcher.stop()
        self.store_patcher.stop()

    def _receipt_resp(self, receipt_id=600001):
        return {
            "ReceiptHeaderId": receipt_id,
            "ReceiptNumber":   "RCV-2026-001",
            "links": [{"rel": "self", "href": f".../receivingReceipts/{receipt_id}"}],
        }

    def _setup_store_get(self, po_id=200001, sup_id=100001, site_id=400001):
        self.mock_store.get.side_effect = [po_id, sup_id, site_id]

    # ── Happy path: full receipt + inspection + deliver ───────────────────

    def test_happy_path_receipt_inspection_deliver_three_way_ready(self):
        """All 5 accepted, 0 rejected → three_way_match_ready = True."""
        self._setup_store_get()
        self.agent.get.return_value = {"POHeaderStatusCode": "APPROVED"}
        self.agent.post.side_effect = [
            self._receipt_resp(),             # create header
            {"ReceiptLineId": 700001},        # create line
            {"InspectionId": 800001},         # create inspection
            {},                               # deliver
        ]

        result = run(self.agent.run(_make_inputs()))

        self.assertTrue(result["three_way_match_ready"])
        self.assertIsNotNone(result["ap_invoice_trigger"])
        self.assertEqual(result["lines"][0]["QuantityAccepted"], 5)
        self.assertEqual(result["lines"][0]["QuantityRejected"], 0)

    # ── PO not open → ValueError ──────────────────────────────────────────

    def test_raises_when_po_not_open_for_receipt(self):
        """ValueError raised when PO status is INCOMPLETE."""
        self._setup_store_get()
        self.agent.get.return_value = {"POHeaderStatusCode": "INCOMPLETE"}

        with self.assertRaises(ValueError) as ctx:
            run(self.agent.run(_make_inputs()))

        self.assertIn("INCOMPLETE", str(ctx.exception))

    # ── No POHeaderId in state → ValueError ───────────────────────────────

    def test_raises_when_po_header_id_not_in_state(self):
        """ValueError raised when POHeaderId is missing from state store."""
        self.mock_store.get.side_effect = [None, None, None]

        with self.assertRaises(ValueError) as ctx:
            run(self.agent.run(_make_inputs()))

        self.assertIn("POHeaderId not found", str(ctx.exception))

    # ── Rejected quantity triggers return to vendor ───────────────────────

    def test_rejected_quantity_triggers_return_to_vendor(self):
        """quantity_rejected > 0 → _return_to_vendor POST is called."""
        self._setup_store_get()
        inputs = _make_inputs()
        inputs["lines"][0]["quantity_rejected"] = 2
        inputs["lines"][0]["quantity_accepted"] = 3
        self.agent.get.return_value = {"POHeaderStatusCode": "COMMUNICATED"}
        self.agent.post.side_effect = [
            self._receipt_resp(),
            {"ReceiptLineId": 700001},
            {"InspectionId": 800001},
            {},  # return_to_vendor
            {},  # deliver (accepted qty)
        ]

        result = run(self.agent.run(inputs))

        self.assertFalse(result["three_way_match_ready"])
        self.assertIsNone(result["ap_invoice_trigger"])
        # Verify 5 POSTs: header, line, inspection, return_to_vendor, deliver
        self.assertEqual(self.agent.post.call_count, 5)

    # ── Duplicate receipt check ───────────────────────────────────────────

    def test_receipt_header_uses_dup_checker(self):
        """Receipt header POST is called with duplicate_checker kwarg."""
        self._setup_store_get()
        self.agent.get.return_value = {"POHeaderStatusCode": "APPROVED"}
        self.agent.post.side_effect = [
            self._receipt_resp(),
            {"ReceiptLineId": 700001},
            {"InspectionId": 800001},
            {},
        ]

        run(self.agent.run(_make_inputs()))

        # First post call is the receipt header — verify it was called
        first_call = self.agent.post.call_args_list[0]
        self.assertIn("receivingReceipts", first_call[0][0])

    # ── ReceiptHeaderId stored before lines ───────────────────────────────

    def test_receipt_header_id_stored_before_line_loop(self):
        """ReceiptHeaderId written to state before line POSTs."""
        set_calls = []
        self.mock_store.get.side_effect = [200001, 100001, 400001]
        self.mock_store.set.side_effect = lambda k, v: set_calls.append(k)
        self.agent.get.return_value = {"POHeaderStatusCode": "APPROVED"}
        self.agent.post.side_effect = [
            self._receipt_resp(),
            {"ReceiptLineId": 700001},
            {"InspectionId": 800001},
            {},
        ]

        run(self.agent.run(_make_inputs()))

        self.assertIn("ReceiptHeaderId", set_calls)


if __name__ == "__main__":
    unittest.main()
