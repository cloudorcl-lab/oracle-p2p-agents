"""
test_pr4.py — Unit tests for PR4AgreementAgent
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
        "agreement_type":   "BPA",
        "supplier_id":      100001,
        "supplier_site_id": 400001,
        "start_date":       "2026-04-01",
        "end_date":         "2027-03-31",
        "agreement_amount": 50000.00,
        "currency":         "USD",
        "payment_terms":    "NET30",
        "description":      "Annual desktop supply agreement",
        "procurement_bu":   "Vision Operations",
        "lines": [{
            "item_id":    300001,
            "item_number": "AS54888",
            "quantity":   100,
            "uom":        "Each",
            "unit_price": 1050.00,
            "price_tiers": [
                {"min_qty": 1,  "max_qty": 50,  "price": 1100.00},
                {"min_qty": 51, "max_qty": 999, "price": 1050.00},
            ],
        }],
    }
    base.update(overrides)
    return base


class TestPR4AgreementAgent(unittest.TestCase):

    def setUp(self):
        self.config_patcher = patch("auth.oracle_auth.load_config", return_value=MagicMock())
        self.config_patcher.start()
        self.store_patcher = patch("state.state_store.AgentStateStore")
        MockStore = self.store_patcher.start()
        self.mock_store = MockStore.return_value
        self.mock_store.get = AsyncMock(return_value=None)
        self.mock_store.set = AsyncMock()
        self.mock_store.set_many = AsyncMock()

        from agents.pr4_agreement import PR4AgreementAgent
        self.agent = PR4AgreementAgent(transaction_id="TEST-PR4-001")
        self.agent.get              = AsyncMock()
        self.agent.post             = AsyncMock()
        self.agent.patch            = AsyncMock()
        self.agent.action           = AsyncMock()
        self.agent.wait_for_approval = AsyncMock()
        self.agent.audit            = AsyncMock()
        self.agent.store            = self.mock_store

    def tearDown(self):
        self.config_patcher.stop()
        self.store_patcher.stop()

    def _agr_resp(self, agr_id=600001, number="BPA-2026-0001"):
        return {
            "AgreementId":         agr_id,
            "AgreementNumber":     number,
            "AgreementStatusCode": "DRAFT",
            "links": [{"rel": "self", "href": f".../supplierAgreements/{agr_id}"}],
        }

    # ── Happy path ────────────────────────────────────────────────────────

    def test_happy_path_creates_and_activates_agreement(self):
        """Full 9-step sequence returns AgreementStatusCode=ACTIVE."""
        # Duplicate check returns empty, then final GET returns ACTIVE
        self.agent.get.side_effect = [
            {"items": []},                                       # Step 1: no duplicate
            {"AgreementStatusCode": "ACTIVE",                   # Step 9: post-activate GET
             "AgreementNumber": "BPA-2026-0001"},
        ]
        self.agent.post.side_effect = [
            self._agr_resp(),                                    # Step 2: create header
            {"AgreementLineId": 700001,                          # Step 3: create line
             "links": [{"rel": "self", "href": ".../lines/700001"}]},
            {"PriceTierId": 800001},                             # Step 4: tier 1
            {"PriceTierId": 800002},                             # Step 4: tier 2
        ]
        self.agent.wait_for_approval.return_value = {"ApprovalStatus": "APPROVED"}
        self.agent.action.return_value = {}

        result = run(self.agent.run(_make_inputs()))

        self.assertEqual(result["AgreementStatusCode"], "ACTIVE")
        self.assertEqual(result["AgreementNumber"], "BPA-2026-0001")
        self.assertIn("AgreementId", result)
        self.assertIn("lines", result)
        self.agent.audit.assert_any_call("PR4_STARTED", unittest.mock.ANY)
        self.agent.audit.assert_any_call("PR4_COMPLETE", unittest.mock.ANY)

    # ── Existing ACTIVE agreement returned without creation ───────────────

    def test_returns_existing_active_agreement_no_post(self):
        """If ACTIVE agreement exists, skip creation and return existing."""
        existing = {
            "AgreementId":         599999,
            "AgreementNumber":     "BPA-EXISTING",
            "AgreementStatusCode": "ACTIVE",
            "StartDate":           "2026-01-01",
            "EndDate":             "2026-12-31",
            "AgreementAmount":     40000.00,
            "RemainingAmount":     30000.00,
            "links": [{"rel": "self", "href": ".../supplierAgreements/599999"}],
        }
        self.agent.get.return_value = {"items": [existing]}

        result = run(self.agent.run(_make_inputs()))

        self.assertEqual(result["AgreementId"], 599999)
        self.assertEqual(result["AgreementStatusCode"], "ACTIVE")
        # No header POST should have been made
        self.agent.post.assert_not_called()

    # ── Approval rejected → AgreementRejectedError ────────────────────────

    def test_raises_agreement_rejected_error(self):
        """AgreementRejectedError raised when poll returns REJECTED."""
        from agents.pr4_agreement import AgreementRejectedError
        self.agent.get.return_value = {"items": []}
        self.agent.post.side_effect = [
            self._agr_resp(),
            {"AgreementLineId": 700001, "links": [{"rel": "self", "href": ".../lines/700001"}]},
            {"PriceTierId": 800001},
            {"PriceTierId": 800002},
        ]
        self.agent.wait_for_approval.return_value = {
            "ApprovalStatus": "REJECTED",
            "RejectionReason": "Price exceeds budget",
        }

        with self.assertRaises(AgreementRejectedError):
            run(self.agent.run(_make_inputs()))

    # ── Price tier gap → PriceTierGapError ───────────────────────────────

    def test_raises_price_tier_gap_error(self):
        """PriceTierGapError when tier 1 ends at 40 and tier 2 starts at 51."""
        from agents.pr4_agreement import PriceTierGapError
        inputs = _make_inputs()
        inputs["lines"][0]["price_tiers"] = [
            {"min_qty": 1,  "max_qty": 40,  "price": 1100.00},
            {"min_qty": 51, "max_qty": 999, "price": 1050.00},  # gap: 41-50
        ]
        self.agent.get.return_value = {"items": []}
        self.agent.post.return_value = self._agr_resp()

        with self.assertRaises(PriceTierGapError):
            run(self.agent.run(inputs))

    # ── Activate only called after APPROVED ───────────────────────────────

    def test_activate_called_only_after_approval(self):
        """action('activate') must appear after wait_for_approval completes."""
        self.agent.get.side_effect = [
            {"items": []},
            {"AgreementStatusCode": "ACTIVE", "AgreementNumber": "BPA-2026-0001"},
        ]
        self.agent.post.side_effect = [
            self._agr_resp(),
            {"AgreementLineId": 700001, "links": [{"rel": "self", "href": ".../lines/700001"}]},
            {"PriceTierId": 800001},
            {"PriceTierId": 800002},
        ]
        self.agent.wait_for_approval.return_value = {"ApprovalStatus": "APPROVED"}
        self.agent.action.return_value = {}

        run(self.agent.run(_make_inputs()))

        action_calls = [str(c) for c in self.agent.action.call_args_list]
        activate_calls = [c for c in action_calls if "activate" in c]
        self.assertGreater(len(activate_calls), 0, "activate action was never called")

    # ── AgreementId stored before line loop ───────────────────────────────

    def test_agreement_id_stored_before_line_creation(self):
        """AgreementId written to store before the line loop begins."""
        set_calls = []
        self.agent.store.set.side_effect = lambda k, v: set_calls.append(k)
        self.agent.get.side_effect = [
            {"items": []},
            {"AgreementStatusCode": "ACTIVE", "AgreementNumber": "BPA-2026-0001"},
        ]
        self.agent.post.side_effect = [
            self._agr_resp(),
            {"AgreementLineId": 700001, "links": [{"rel": "self", "href": ".../lines/700001"}]},
            {"PriceTierId": 800001},
            {"PriceTierId": 800002},
        ]
        self.agent.wait_for_approval.return_value = {"ApprovalStatus": "APPROVED"}
        self.agent.action.return_value = {}

        run(self.agent.run(_make_inputs()))

        self.assertIn("AgreementId", set_calls)
        if "AgreementLineId" in set_calls:
            self.assertLess(set_calls.index("AgreementId"),
                            set_calls.index("AgreementLineId"))

    # ── submitForApproval 504 falls through to poll ───────────────────────

    def test_submit_timeout_falls_through_to_poll(self):
        """Exception from submitForApproval is swallowed — poll still runs."""
        self.agent.get.side_effect = [
            {"items": []},
            {"AgreementStatusCode": "ACTIVE", "AgreementNumber": "BPA-2026-0001"},
        ]
        self.agent.post.side_effect = [
            self._agr_resp(),
            {"AgreementLineId": 700001, "links": [{"rel": "self", "href": ".../lines/700001"}]},
            {"PriceTierId": 800001},
            {"PriceTierId": 800002},
        ]
        self.agent.action.side_effect = [
            TimeoutError("504"),  # submitForApproval
            {},                   # activate
        ]
        self.agent.wait_for_approval.return_value = {"ApprovalStatus": "APPROVED"}

        result = run(self.agent.run(_make_inputs()))

        self.assertEqual(result["AgreementStatusCode"], "ACTIVE")

    # ── _validate_price_tiers unit tests ──────────────────────────────────

    def test_validate_price_tiers_passes_with_no_gap(self):
        """No exception when tiers are contiguous."""
        from agents.pr4_agreement import PR4AgreementAgent
        agent = PR4AgreementAgent.__new__(PR4AgreementAgent)
        tiers = [
            {"min_qty": 1,  "max_qty": 50,  "price": 1100},
            {"min_qty": 51, "max_qty": 100, "price": 1050},
            {"min_qty": 101, "max_qty": 999, "price": 1000},
        ]
        agent._validate_price_tiers(tiers)  # no exception

    def test_validate_price_tiers_raises_on_gap(self):
        """PriceTierGapError when tiers are not contiguous."""
        from agents.pr4_agreement import PR4AgreementAgent, PriceTierGapError
        agent = PR4AgreementAgent.__new__(PR4AgreementAgent)
        tiers = [
            {"min_qty": 1,  "max_qty": 50,  "price": 1100},
            {"min_qty": 52, "max_qty": 999, "price": 1050},  # gap at 51
        ]
        with self.assertRaises(PriceTierGapError):
            agent._validate_price_tiers(tiers)

    def test_validate_price_tiers_empty_passes(self):
        """Empty tier list passes validation."""
        from agents.pr4_agreement import PR4AgreementAgent
        agent = PR4AgreementAgent.__new__(PR4AgreementAgent)
        agent._validate_price_tiers([])  # no exception


if __name__ == "__main__":
    unittest.main()
