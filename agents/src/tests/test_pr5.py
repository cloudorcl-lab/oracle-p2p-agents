"""
test_pr5.py — Unit tests for PR5PurchaseOrderAgent
===================================================
All external I/O mocked. Uses unittest + unittest.mock only.
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch


def run(coro):
    return asyncio.run(coro)


def _make_inputs(**overrides):
    base = {
        "supplier_id":        100001,
        "supplier_site_id":   400001,
        "buyer_id":           700001,
        "currency":           "USD",
        "bill_to_location_id": 204,
        "ship_to_location_id": 204,
        "payment_terms_id":   1001,
        "description":        "PO for desktop computers",
        "lines": [{
            "item_id":      300001,
            "quantity":     5,
            "uom":          "Each",
            "unit_price":   1050.00,
            "need_by_date": "2026-04-30",
            "schedules": [{
                "schedule_number": 1,
                "quantity":        5,
                "need_by_date":    "2026-04-30",
                "ship_to_org_id":  204,
                "distributions": [{
                    "distribution_number": 1,
                    "quantity_ordered":    5,
                    "charge_account_id":   800001,
                }],
            }],
        }],
    }
    base.update(overrides)
    return base


class TestPR5PurchaseOrderAgent(unittest.TestCase):

    def setUp(self):
        self.config_patcher = patch("auth.oracle_auth.load_config", return_value=MagicMock())
        self.config_patcher.start()
        self.store_patcher = patch("state.state_store.AgentStateStore")
        MockStore = self.store_patcher.start()
        self.mock_store = MockStore.return_value
        self.mock_store.get = AsyncMock(return_value=None)
        self.mock_store.set = AsyncMock()
        self.mock_store.set_many = AsyncMock()

        from agents.pr5_purchase_order import PR5PurchaseOrderAgent
        self.agent = PR5PurchaseOrderAgent(transaction_id="TEST-PR5-001")
        self.agent.get              = AsyncMock()
        self.agent.post             = AsyncMock()
        self.agent.action           = AsyncMock()
        self.agent.wait_for_approval = AsyncMock()
        self.agent.audit            = AsyncMock()
        self.agent.store            = self.mock_store

    def tearDown(self):
        self.config_patcher.stop()
        self.store_patcher.stop()

    def _po_resp(self, po_id=200001, order_number="PO-2026-001"):
        return {
            "POHeaderId":  po_id,
            "OrderNumber": order_number,
            "links": [{"rel": "self", "href": f".../purchaseOrders/{po_id}"}],
        }

    # ── Happy path ────────────────────────────────────────────────────────

    def test_happy_path_po_created_approved_communicated(self):
        """Full sequence: validate → create → lines → approve → communicate."""
        # _validate_supplier
        self.agent.get.side_effect = [
            {"SupplierStatus": "ACTIVE"},   # validate supplier
            {"items": []},                  # duplicate check (no existing PO)
        ]
        self.agent.post.side_effect = [
            self._po_resp(),            # create header
            {"POLineId": 300001},       # create line
            {"POScheduleId": 400001},   # create schedule
            {},                         # create distribution
        ]
        self.agent.wait_for_approval.return_value = {"POHeaderStatusCode": "APPROVED"}
        self.agent.action.return_value = {}

        result = run(self.agent.run(_make_inputs()))

        self.assertEqual(result["POHeaderStatusCode"], "APPROVED")
        self.assertIn("POHeaderId", result)
        self.assertIn("OrderNumber", result)

    # ── Existing PO returned without creating ─────────────────────────────

    def test_returns_existing_po_when_requisition_already_converted(self):
        """If PO already exists for this RequisitionHeaderId, return it."""
        existing_po = self._po_resp(po_id=199999, order_number="PO-EXISTING")
        existing_po["POHeaderStatusCode"] = "APPROVED"
        # store.get: (1) RequisitionHeaderId→1001, (2) RequisitionLines→[]
        self.mock_store.get.side_effect = [1001, []]
        self.agent.get.side_effect = [
            {"SupplierStatus": "ACTIVE"},
            {"items": [existing_po]},  # duplicate check → existing PO found
        ]
        self.agent.post.side_effect = [
            {"POLineId": 300001},       # create_line (still runs for existing PO)
            {"POScheduleId": 400001},   # create_schedule
            {},                         # create_distribution
        ]
        self.agent.wait_for_approval.return_value = {"POHeaderStatusCode": "APPROVED"}

        result = run(self.agent.run(_make_inputs()))

        # Existing PO returned — POHeaderId should be 199999
        self.assertIn("POHeaderId", result)
        self.assertEqual(result["POHeaderId"], 199999)

    # ── Supplier not ACTIVE → ValueError ─────────────────────────────────

    def test_raises_when_supplier_not_active(self):
        """ValueError raised when supplier status is not ACTIVE."""
        self.agent.get.return_value = {"SupplierStatus": "INACTIVE"}

        with self.assertRaises(ValueError):
            run(self.agent.run(_make_inputs()))

    # ── Approval rejected → POApprovalError ──────────────────────────────

    def test_raises_po_approval_error_on_rejection(self):
        """POApprovalError raised when approval poll returns REJECTED."""
        from agents.pr5_purchase_order import POApprovalError
        self.agent.get.side_effect = [
            {"SupplierStatus": "ACTIVE"},
            {"items": []},
        ]
        self.agent.post.side_effect = [
            self._po_resp(),
            {"POLineId": 300001},
            {"POScheduleId": 400001},
            {},
        ]
        self.agent.wait_for_approval.return_value = {"POHeaderStatusCode": "REJECTED"}

        with self.assertRaises(POApprovalError):
            run(self.agent.run(_make_inputs()))

    # ── communicate failure is warning, not exception ─────────────────────

    def test_communicate_failure_does_not_raise(self):
        """Exception from communicate action is swallowed — PO still returned."""
        self.agent.get.side_effect = [
            {"SupplierStatus": "ACTIVE"},
            {"items": []},
        ]
        self.agent.post.side_effect = [
            self._po_resp(),
            {"POLineId": 300001},
            {"POScheduleId": 400001},
            {},
        ]
        self.agent.wait_for_approval.return_value = {"POHeaderStatusCode": "APPROVED"}
        # calculateTax succeeds, submitForApproval succeeds, communicate fails
        self.agent.action.side_effect = [
            {},               # calculateTax
            {},               # submitForApproval
            RuntimeError("communicate failed"),  # communicate
        ]

        result = run(self.agent.run(_make_inputs()))

        self.assertEqual(result["POHeaderStatusCode"], "APPROVED")

    # ── POHeaderId stored before line loop ────────────────────────────────

    def test_po_header_id_stored_before_line_creation(self):
        """POHeaderId written to store before lines are created."""
        set_calls = []
        self.agent.store.set.side_effect = lambda k, v: set_calls.append(k)
        self.agent.get.side_effect = [
            {"SupplierStatus": "ACTIVE"},
            {"items": []},
        ]
        self.agent.post.side_effect = [
            self._po_resp(),
            {"POLineId": 300001},
            {"POScheduleId": 400001},
            {},
        ]
        self.agent.wait_for_approval.return_value = {"POHeaderStatusCode": "APPROVED"}

        run(self.agent.run(_make_inputs()))

        self.assertIn("POHeaderId", set_calls)
        if "POLines" in set_calls:
            self.assertLess(set_calls.index("POHeaderId"), set_calls.index("POLines"))


if __name__ == "__main__":
    unittest.main()
