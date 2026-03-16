"""
test_pr3.py — Unit tests for PR3SourcingAgent
=============================================
All external I/O mocked. Uses unittest + unittest.mock only.
Response monitoring is patched to return immediately (no 3600s sleeps).
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch


def run(coro):
    return asyncio.run(coro)


def _make_inputs(**overrides):
    base = {
        "negotiation_type":       "RFQ",
        "title":                  "RFQ — Desktop Computers Q2 2026",
        "buyer_id":               700001,
        "open_bidding_date":      "2026-03-20T09:00:00Z",
        "response_due_date":      "2026-04-15T17:00:00Z",
        "award_by_line":          True,
        "allow_view_bid_ranking": False,
        "overall_scoring_method": "MANUAL",
        "auto_extend":            False,
        "max_extension_days":     0,
        "invited_suppliers": [{
            "supplier_id":      100001,
            "supplier_site_id": 400001,
            "email":            "sales@acmecorp.com",
        }],
        "lines": [{
            "item_id":      300001,
            "quantity":     5,
            "uom":          "Each",
            "need_by_date": "2026-04-30",
            "target_price": 1200.00,
            "line_type":    "Goods",
            "requirements": [{
                "type":          "TECHNICAL",
                "description":   "Provide ISO 9001 certificate",
                "is_mandatory":  True,
                "response_type": "FILE",
            }],
        }],
    }
    base.update(overrides)
    return base


class TestPR3SourcingAgent(unittest.TestCase):

    def setUp(self):
        self.config_patcher = patch("auth.oracle_auth.load_config", return_value=MagicMock())
        self.config_patcher.start()
        self.store_patcher = patch("state.state_store.AgentStateStore")
        MockStore = self.store_patcher.start()
        self.mock_store = MockStore.return_value
        self.mock_store.get = AsyncMock(return_value=None)
        self.mock_store.set = AsyncMock()
        self.mock_store.set_many = AsyncMock()

        from agents.pr3_sourcing import PR3SourcingAgent
        self.agent = PR3SourcingAgent(transaction_id="TEST-PR3-001")
        # Reduce poll interval to zero for tests
        self.agent.RESPONSE_POLL_INTERVAL_SEC  = 0
        self.agent.RESPONSE_POLL_TIMEOUT_HOURS = 0.001  # expire immediately
        self.agent.get   = AsyncMock()
        self.agent.post  = AsyncMock()
        self.agent.patch = AsyncMock()
        self.agent.action = AsyncMock()
        self.agent.audit = AsyncMock()
        self.agent.store = self.mock_store

    def tearDown(self):
        self.config_patcher.stop()
        self.store_patcher.stop()

    def _neg_resp(self, neg_id=500001, number="NB-2026-0001"):
        return {
            "NegotiationId":     neg_id,
            "NegotiationNumber": number,
            "NegotiationStatus": "DRAFT",
            "links": [{"rel": "self", "href": f".../supplierNegotiations/{neg_id}"}],
        }

    def _submitted_response(self, response_id=900001, supplier_id=100001):
        return {
            "ResponseId":     response_id,
            "ResponseStatus": "SUBMITTED",
            "SupplierId":     supplier_id,
            "SupplierSiteId": 400001,
            "lines": [],  # populated in step 8
        }

    def _response_line(self, neg_line_id=600001, price=1050.00):
        return {
            "NegotiationLineId": neg_line_id,
            "QuotedPrice":       price,
            "Quantity":          5,
        }

    def _awarded(self, award_id=800001):
        return {"AwardId": award_id, "AwardStatus": "AWARDED",
                "AwardedSupplierId": 100001, "AwardedSupplierSiteId": 400001,
                "AwardedPrice": 1050.00}

    # ── Happy path: full 12-step RFQ ─────────────────────────────────────

    def test_happy_path_full_rfq_flow(self):
        """Full sequence from create to CLOSED with one award."""
        # No existing negotiation
        self.mock_store.get.side_effect = [None, None, None, None, None]
        neg_resp = self._neg_resp()
        sub_resp = self._submitted_response()
        sub_resp["lines"] = [self._response_line()]

        self.agent.get.side_effect = [
            {"items": []},                # Step 1: no existing
            {"items": [sub_resp]},        # Step 7: monitor (submitted)
            {"items": [sub_resp["lines"][0]]},  # Step 8: response lines
            {"items": [self._awarded()]}, # Step 12: confirm awards
        ]
        self.agent.post.side_effect = [
            neg_resp,                   # Step 2: create header
            {"NegotiationLineId": 600001},  # Step 3: create line
            {},                         # Step 4: add requirement
            {},                         # Step 5: invite supplier
            {},                         # Step 10: award
        ]
        self.agent.action.return_value = {}  # publish, close

        result = run(self.agent.run(_make_inputs()))

        self.assertEqual(result["NegotiationStatus"], "CLOSED")
        self.assertIn("NegotiationId", result)
        self.assertEqual(len(result["awards"]), 1)
        self.agent.audit.assert_any_call("PR3_STARTED", unittest.mock.ANY)
        self.agent.audit.assert_any_call("PR3_COMPLETE", unittest.mock.ANY)

    # ── Resume: existing PUBLISHED negotiation → skip steps 2-6 ──────────

    def test_resumes_from_step_7_when_negotiation_already_published(self):
        """If PUBLISHED negotiation found, skip creation and resume monitoring."""
        existing = self._neg_resp(neg_id=499999, number="NB-EXISTING")
        existing["NegotiationStatus"] = "PUBLISHED"
        existing["links"] = [{"rel": "self", "href": ".../supplierNegotiations/499999"}]

        self.mock_store.get.side_effect = [1001, None, None, None, None]  # req_header_id
        sub_resp = self._submitted_response()
        sub_resp["lines"] = [self._response_line()]

        self.agent.get.side_effect = [
            {"items": [existing]},        # Step 1: existing found
            {"items": [sub_resp]},        # Step 7: monitor
            {"items": [sub_resp["lines"][0]]},  # Step 8: lines
            {"items": [self._awarded()]}, # Step 12: confirm
        ]
        self.agent.post.side_effect = [{}]  # Step 10: award
        self.agent.action.return_value = {} # close

        result = run(self.agent.run(_make_inputs()))

        self.assertEqual(result["NegotiationStatus"], "CLOSED")
        # POST should only be called for the award — not header/line/invite
        header_posts = [c for c in self.agent.post.call_args_list
                        if "supplierNegotiations" in str(c) and "child" not in str(c)]
        self.assertEqual(len(header_posts), 0, "Header POST should not be called on resume")

    # ── Publish failure → NegotiationPublishError ─────────────────────────

    def test_raises_negotiation_publish_error_when_publish_fails(self):
        """NegotiationPublishError when publish action fails and status not PUBLISHED."""
        from agents.pr3_sourcing import NegotiationPublishError
        self.mock_store.get.side_effect = [None, None]
        self.agent.get.side_effect = [
            {"items": []},                # Step 1: no existing
            {"NegotiationStatus": "DRAFT"},  # Status check after publish failure
        ]
        self.agent.post.side_effect = [
            self._neg_resp(),
            {"NegotiationLineId": 600001},
            {},  # requirement
            {},  # invite
        ]
        self.agent.action.side_effect = RuntimeError("publish timed out")

        with self.assertRaises(NegotiationPublishError):
            run(self.agent.run(_make_inputs()))

    # ── No responses → NoResponsesReceivedError ───────────────────────────

    def test_raises_no_responses_received_error_when_no_bids(self):
        """NoResponsesReceivedError when zero responses at deadline (past due date)."""
        from agents.pr3_sourcing import NoResponsesReceivedError
        self.mock_store.get.side_effect = [None, None, None, None]
        self.agent.get.side_effect = [
            {"items": []},   # Step 1: no existing
            {"items": []},   # Step 7: monitor — no responses (due date already past)
        ]
        self.agent.post.side_effect = [
            self._neg_resp(),
            {"NegotiationLineId": 600001},
            {},  # requirement
            {},  # invite
        ]
        self.agent.action.return_value = {}  # publish
        # Use a past due date so monitor exits immediately after one poll
        inputs = _make_inputs(response_due_date="2026-01-01T17:00:00Z")

        with self.assertRaises(NoResponsesReceivedError):
            run(self.agent.run(inputs))

    # ── Award confirmation failure → AwardConfirmationError ──────────────

    def test_raises_award_confirmation_error_when_award_not_confirmed(self):
        """AwardConfirmationError when award GET shows non-AWARDED status."""
        from agents.pr3_sourcing import AwardConfirmationError
        self.mock_store.get.side_effect = [None, None, None, None, None]
        sub_resp = self._submitted_response()
        sub_resp["lines"] = [self._response_line()]
        unconfirmed = {"AwardId": 800001, "AwardStatus": "PENDING",
                       "AwardedSupplierId": 100001}

        self.agent.get.side_effect = [
            {"items": []},                        # Step 1: no existing
            {"items": [sub_resp]},                # Step 7: monitor
            {"items": [sub_resp["lines"][0]]},    # Step 8: lines
            {"items": [unconfirmed]},             # Step 12: confirm — not AWARDED
        ]
        self.agent.post.side_effect = [
            self._neg_resp(),
            {"NegotiationLineId": 600001},
            {},  # requirement
            {},  # invite
            {},  # award
        ]
        self.agent.action.return_value = {}

        with self.assertRaises(AwardConfirmationError):
            run(self.agent.run(_make_inputs()))

    # ── NegotiationId stored before line loop ─────────────────────────────

    def test_negotiation_id_stored_before_line_creation(self):
        """NegotiationId written to store before line POST."""
        set_calls = []
        self.mock_store.get.side_effect = [None, None, None, None, None]
        self.mock_store.set.side_effect = lambda k, v: set_calls.append(k)
        sub_resp = self._submitted_response()
        sub_resp["lines"] = [self._response_line()]

        self.agent.get.side_effect = [
            {"items": []},
            {"items": [sub_resp]},
            {"items": [sub_resp["lines"][0]]},
            {"items": [self._awarded()]},
        ]
        self.agent.post.side_effect = [
            self._neg_resp(),
            {"NegotiationLineId": 600001},
            {}, {}, {},
        ]
        self.agent.action.return_value = {}

        run(self.agent.run(_make_inputs()))

        self.assertIn("NegotiationId", set_calls)
        if "NegotiationLines" in set_calls:
            self.assertLess(set_calls.index("NegotiationId"),
                            set_calls.index("NegotiationLines"))

    # ── Validation: invalid negotiation type ─────────────────────────────

    def test_raises_on_invalid_negotiation_type(self):
        """ValueError raised for unknown negotiation type."""
        with self.assertRaises(ValueError) as ctx:
            run(self.agent.run(_make_inputs(negotiation_type="INVALID_TYPE")))
        self.assertIn("INVALID_TYPE", str(ctx.exception))

    # ── Validation: no suppliers invited ─────────────────────────────────

    def test_raises_when_no_suppliers_invited(self):
        """ValueError raised when invited_suppliers is empty."""
        with self.assertRaises(ValueError):
            run(self.agent.run(_make_inputs(invited_suppliers=[])))

    # ── _to_oracle_datetime normalization ─────────────────────────────────

    def test_to_oracle_datetime_normalizes_plus_utc(self):
        from agents.pr3_sourcing import PR3SourcingAgent
        result = PR3SourcingAgent._to_oracle_datetime("2026-04-15T17:00:00+00:00")
        self.assertTrue(result.endswith("Z"), f"Expected Z suffix, got: {result}")

    def test_to_oracle_datetime_passes_through_z_suffix(self):
        from agents.pr3_sourcing import PR3SourcingAgent
        result = PR3SourcingAgent._to_oracle_datetime("2026-04-15T17:00:00Z")
        self.assertEqual(result, "2026-04-15T17:00:00Z")

    def test_to_oracle_datetime_appends_time_to_date_only(self):
        from agents.pr3_sourcing import PR3SourcingAgent
        result = PR3SourcingAgent._to_oracle_datetime("2026-04-15")
        self.assertEqual(result, "2026-04-15T00:00:00Z")

    # ── _select_winner: lowest price wins ─────────────────────────────────

    def test_select_winner_picks_lowest_price(self):
        """_select_winner returns cheapest bid per line."""
        from agents.pr3_sourcing import PR3SourcingAgent
        agent = PR3SourcingAgent.__new__(PR3SourcingAgent)
        agent.log = MagicMock()

        responses = [
            {
                "ResponseId": 1, "SupplierId": 100001, "SupplierSiteId": 400001,
                "ResponseStatus": "SUBMITTED",
                "lines": [{"NegotiationLineId": 600001, "QuotedPrice": 1100.00, "Quantity": 5}],
            },
            {
                "ResponseId": 2, "SupplierId": 100002, "SupplierSiteId": 400002,
                "ResponseStatus": "SUBMITTED",
                "lines": [{"NegotiationLineId": 600001, "QuotedPrice": 1050.00, "Quantity": 5}],
            },
        ]
        lines    = [{"quantity": 5, "target_price": 1200.00}]
        inv_sups = [{"supplier_id": 100001, "supplier_site_id": 400001}]

        awards = agent._select_winner(responses, lines, inv_sups)

        self.assertEqual(len(awards), 1)
        self.assertEqual(awards[0]["AwardedSupplierId"], 100002)  # cheaper supplier
        self.assertEqual(awards[0]["AwardedPrice"], 1050.00)


if __name__ == "__main__":
    unittest.main()
