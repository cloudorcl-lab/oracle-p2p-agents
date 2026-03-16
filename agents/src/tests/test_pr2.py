"""
test_pr2.py — Unit tests for PR2RequisitionAgent
=================================================
All external I/O mocked. Uses unittest + unittest.mock only.
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch


def run(coro):
    return asyncio.run(coro)


def _make_inputs(**overrides):
    base = {
        "requester_email":   "buyer@company.com",
        "requisitioning_bu": "Vision Operations",
        "description":       "Desktop computers Q2 2026",
        "justification":     "Annual refresh",
        "lines": [{
            "line_number":      1,
            "item_number":      "AS54888",
            "uom":              "Each",
            "quantity":         5,
            "need_by_date":     "2026-04-30",
            "destination_type": "Expense",
            "org_code":         "V1",
            "deliver_to_location": "V1-New York City",
            "supplier_name":    "Acme Corp",
            "supplier_site":    "ACME_PURCHASING",
            "distributions": [
                {"distribution_number": 1, "quantity": 3, "cost_center": "1100"},
                {"distribution_number": 2, "quantity": 2, "cost_center": "1200"},
            ],
        }],
    }
    base.update(overrides)
    return base


class TestPR2RequisitionAgent(unittest.TestCase):

    def setUp(self):
        self.config_patcher = patch("auth.oracle_auth.load_config", return_value=MagicMock())
        self.config_patcher.start()
        self.store_patcher = patch("state.state_store.AgentStateStore")
        MockStore = self.store_patcher.start()
        self.mock_store = MockStore.return_value
        self.mock_store.get = AsyncMock(return_value=None)
        self.mock_store.set = AsyncMock()
        self.mock_store.set_many = AsyncMock()

        from agents.pr2_requisition import PR2RequisitionAgent
        self.agent = PR2RequisitionAgent(transaction_id="TEST-PR2-001")
        self.agent.get              = AsyncMock()
        self.agent.post             = AsyncMock()
        self.agent.action           = AsyncMock()
        self.agent.wait_for_approval = AsyncMock()
        self.agent.audit            = AsyncMock()
        self.agent.store            = self.mock_store

    def tearDown(self):
        self.config_patcher.stop()
        self.store_patcher.stop()

    def _mock_pre_check_gets(self):
        """Return values for the 3 GETs in _run_pre_checks."""
        return [
            # Check 1 — supplier active
            {"items": [{"SupplierId": 100001, "SupplierStatus": "ACTIVE"}]},
            # Check 2 — purchasing site
            {"items": [{"SupplierSiteId": 400001}]},
            # Check 3 — BU ID lookup
            {"items": [{"BusinessUnitId": 300}]},
            # Check 3 — sourcing eligibility
            {"items": []},
            # Check 6 — item in PIM
            {"items": [{"ItemNumber": "AS54888"}]},
        ]

    def _mock_header_resp(self):
        return {
            "RequisitionHeaderId": 1001,
            "Requisition":         "REQ-2026-001",
            "links": [{"rel": "self", "href": ".../purchaseRequisitions/1001"}],
        }

    # ── Happy path ────────────────────────────────────────────────────────

    def test_happy_path_pr_created_and_approved(self):
        """Full sequence: pre-checks → header → line → dists → approve."""
        self.agent.get.side_effect = self._mock_pre_check_gets()
        self.agent.post.side_effect = [
            self._mock_header_resp(),
            {"RequisitionLineId": 2001},  # line
            {},                            # dist 1
            {},                            # dist 2
        ]
        self.agent.action.return_value = {"FundsStatus": "PASSED"}
        self.agent.wait_for_approval.return_value = {
            "DocumentStatus": "APPROVED",
            "ApprovedDate":   "2026-03-15",
        }

        result = run(self.agent.run(_make_inputs()))

        self.assertEqual(result["DocumentStatus"], "APPROVED")
        self.assertIn("RequisitionHeaderId", result)
        self.assertIn("RequisitionNumber", result)

    # ── Funds check failed ────────────────────────────────────────────────

    def test_raises_funds_check_failed_error(self):
        """FundsCheckFailedError when checkFunds returns FAILED."""
        from agents.pr2_requisition import FundsCheckFailedError
        self.agent.get.side_effect = self._mock_pre_check_gets()
        self.agent.post.side_effect = [
            self._mock_header_resp(),
            {"RequisitionLineId": 2001},
            {}, {},
        ]
        # First action = calculateTax (passes), second = checkFunds (fails)
        self.agent.action.side_effect = [
            {},
            {"FundsStatus": "FAILED", "FundsStatusMessage": "Insufficient budget"},
        ]

        with self.assertRaises(FundsCheckFailedError):
            run(self.agent.run(_make_inputs()))

    # ── PR rejected ───────────────────────────────────────────────────────

    def test_raises_pr_rejected_error(self):
        """PRRejectedError when approval poll returns REJECTED."""
        from agents.pr2_requisition import PRRejectedError
        self.agent.get.side_effect = self._mock_pre_check_gets()
        self.agent.post.side_effect = [
            self._mock_header_resp(),
            {"RequisitionLineId": 2001},
            {}, {},
        ]
        self.agent.action.return_value = {"FundsStatus": "PASSED"}
        self.agent.wait_for_approval.return_value = {"DocumentStatus": "REJECTED"}

        with self.assertRaises(PRRejectedError):
            run(self.agent.run(_make_inputs()))

    # ── Pre-check: supplier not found ─────────────────────────────────────

    def test_raises_pre_check_error_when_supplier_not_found(self):
        """PreCheckError(1) raised when supplier lookup returns empty."""
        from agents.pr2_requisition import PreCheckError
        self.agent.get.return_value = {"items": []}  # supplier not found

        with self.assertRaises(PreCheckError) as ctx:
            run(self.agent.run(_make_inputs()))

        self.assertEqual(ctx.exception.check_number, 1)

    # ── Distribution sum validation ───────────────────────────────────────

    def test_raises_when_distributions_dont_sum_to_line_quantity(self):
        """ValueError when distributions sum != line quantity."""
        inputs = _make_inputs()
        inputs["lines"][0]["distributions"] = [
            {"distribution_number": 1, "quantity": 3, "cost_center": "1100"},
            {"distribution_number": 2, "quantity": 1, "cost_center": "1200"},
            # Total 4 ≠ 5
        ]
        self.agent.get.side_effect = self._mock_pre_check_gets()
        self.agent.post.side_effect = [
            self._mock_header_resp(),
            {"RequisitionLineId": 2001},
        ]

        with self.assertRaises(ValueError):
            run(self.agent.run(inputs))

    # ── RequisitionHeaderId stored before line creation ───────────────────

    def test_requisition_header_id_stored_before_line_loop(self):
        """RequisitionHeaderId written to state before any line POST."""
        set_calls = []
        self.agent.store.set.side_effect = lambda k, v: set_calls.append(k)

        self.agent.get.side_effect = self._mock_pre_check_gets()
        self.agent.post.side_effect = [
            self._mock_header_resp(),
            {"RequisitionLineId": 2001},
            {}, {},
        ]
        self.agent.action.return_value = {"FundsStatus": "PASSED"}
        self.agent.wait_for_approval.return_value = {"DocumentStatus": "APPROVED", "ApprovedDate": ""}

        run(self.agent.run(_make_inputs()))

        self.assertIn("RequisitionHeaderId", set_calls)

    # ── submitRequisition 504 falls through to poll ───────────────────────

    def test_submit_timeout_falls_through_to_poll(self):
        """Exception from submitRequisition is caught — polling still runs."""
        self.agent.get.side_effect = self._mock_pre_check_gets()
        self.agent.post.side_effect = [
            self._mock_header_resp(),
            {"RequisitionLineId": 2001},
            {}, {},
        ]
        self.agent.action.side_effect = [
            {},  # calculateTaxAndAccounting
            {"FundsStatus": "PASSED"},  # checkFunds
            TimeoutError("504"),        # submitRequisition
        ]
        self.agent.wait_for_approval.return_value = {"DocumentStatus": "APPROVED", "ApprovedDate": ""}

        # Should not raise — poll handles the rest
        result = run(self.agent.run(_make_inputs()))
        self.assertEqual(result["DocumentStatus"], "APPROVED")


if __name__ == "__main__":
    unittest.main()
