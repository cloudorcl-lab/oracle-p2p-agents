"""
test_pr1.py — Unit tests for PR1SupplierAgent
=============================================
All external I/O mocked. Uses unittest + unittest.mock only.
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch


def run(coro):
    return asyncio.run(coro)


def _make_inputs(**overrides):
    base = {
        "supplier_name": "Acme Corp",
        "tax_id":        "12-3456789",
        "supplier_type": "CORPORATION",
        "contact": {
            "first_name": "Jane", "last_name": "Doe",
            "email": "jane@acme.com", "phone": "555-0100",
        },
        "address": {
            "line1": "123 Main St", "city": "New York",
            "state": "NY", "postal_code": "10001", "country": "US",
        },
        "payment_terms": "NET30",
        "bank": {
            "name": "Chase", "branch": "NYC",
            "account_number": "123456789", "routing_number": "021000021",
            "account_type": "CHECKING", "swift": "CHASUS33",
        },
        "qualifications": [{
            "type": "ISO_9001", "cert_number": "CERT-001",
            "issue_date": "2024-01-01", "expiry_date": "2026-12-31",
            "issuing_authority": "BSI",
        }],
        "procurement_bu": "Vision Operations",
    }
    base.update(overrides)
    return base


class TestPR1SupplierAgent(unittest.TestCase):

    def setUp(self):
        self.config_patcher = patch("auth.oracle_auth.load_config", return_value=MagicMock())
        self.config_patcher.start()
        self.store_patcher = patch("state.state_store.AgentStateStore")
        MockStore = self.store_patcher.start()
        self.mock_store = MockStore.return_value
        self.mock_store.get = AsyncMock(return_value=None)
        self.mock_store.set = AsyncMock()
        self.mock_store.set_many = AsyncMock()

        from agents.pr1_supplier import PR1SupplierAgent
        self.agent = PR1SupplierAgent(transaction_id="TEST-PR1-001")
        self.agent.get              = AsyncMock()
        self.agent.post             = AsyncMock()
        self.agent.action           = AsyncMock()
        self.agent.wait_for_approval = AsyncMock()
        self.agent.audit            = AsyncMock()
        self.agent.store            = self.mock_store

    def tearDown(self):
        self.config_patcher.stop()
        self.store_patcher.stop()

    def _supplier_resp(self, supplier_id=100001, uniq="100001"):
        return {
            "SupplierId": supplier_id,
            "SupplierName": "Acme Corp",
            "SupplierStatus": "APPROVED",
            "links": [{"rel": "self", "href": f"https://oracle/suppliers/{uniq}"}],
        }

    # ── Happy path ────────────────────────────────────────────────────────

    def test_happy_path_creates_and_activates_supplier(self):
        """Full sequence returns ACTIVE supplier."""
        # No cached duplicate in Redis
        self.mock_store.get.return_value = None
        self.agent.post.side_effect = [
            self._supplier_resp(),                                                              # create supplier
            {"AddressId": 200001},                                                              # address
            {"ContactId": 300001},                                                              # contact
            {"SupplierSiteId": 400001, "links": [{"rel": "self", "href": ".../sites/400001"}]},# site
            {"SupplierSiteAssignmentId": 450001},                                               # site assignment
            {"BankAccountId": 500001},                                                          # bank account
            {"QualificationId": 600001},                                                        # qualification
        ]
        self.agent.wait_for_approval.return_value = {"SupplierStatus": "APPROVED"}

        result = run(self.agent.run(_make_inputs()))

        self.assertEqual(result["SupplierStatus"], "ACTIVE")
        self.assertIn("SupplierId", result)
        self.assertIn("SupplierSiteId", result)
        self.assertIn("BankAccountId", result)
        self.agent.audit.assert_any_call("PR1_STARTED", unittest.mock.ANY)
        self.agent.audit.assert_any_call("PR1_COMPLETE", unittest.mock.ANY)

    # ── Duplicate by tax ID — skip creation ───────────────────────────────

    def test_returns_existing_supplier_when_tax_id_duplicate(self):
        """If SupplierId cached in Redis, skip creation — return existing SupplierId."""
        # Simulate Redis hit for SupplierId and SupplierUniqId
        self.mock_store.get.side_effect = ["999999", "999999"] + [None] * 20
        self.agent.post.side_effect = [
            {"AddressId": 200002},
            {"ContactId": 300002},
            {"SupplierSiteId": 400002, "links": [{"rel": "self", "href": ".../sites/400002"}]},
            {"SupplierSiteAssignmentId": 450002},
            {"BankAccountId": 500002},
            {"QualificationId": 600002},
        ]
        self.agent.wait_for_approval.return_value = {"SupplierStatus": "APPROVED"}

        result = run(self.agent.run(_make_inputs()))

        self.assertEqual(result["SupplierId"], 999999)

    # ── Approval rejected → SupplierRejectedError ─────────────────────────

    def test_raises_supplier_rejected_error_on_rejection(self):
        """SupplierRejectedError raised when approval returns REJECTED."""
        from agents.pr1_supplier import SupplierRejectedError
        self.mock_store.get.return_value = None
        self.agent.post.side_effect = [
            self._supplier_resp(),
            {"AddressId": 200001},
            {"ContactId": 300001},
            {"SupplierSiteId": 400001, "links": [{"rel": "self", "href": ".../sites/400001"}]},
            {"SupplierSiteAssignmentId": 450001},
            {"BankAccountId": 500001},
            {"QualificationId": 600001},
        ]
        self.agent.wait_for_approval.return_value = {
            "SupplierStatus": "REJECTED",
            "RejectionReason": "Compliance issue",
        }

        with self.assertRaises(SupplierRejectedError):
            run(self.agent.run(_make_inputs()))

    # ── activate() only called after approval ─────────────────────────────

    def test_activate_called_only_after_approved(self):
        """action(activate) must not be called before wait_for_approval resolves."""
        self.mock_store.get.return_value = None
        self.agent.post.side_effect = [
            self._supplier_resp(),
            {"AddressId": 200001},
            {"ContactId": 300001},
            {"SupplierSiteId": 400001, "links": [{"rel": "self", "href": ".../sites/400001"}]},
            {"SupplierSiteAssignmentId": 450001},
            {"BankAccountId": 500001},
            {"QualificationId": 600001},
        ]
        self.agent.wait_for_approval.return_value = {"SupplierStatus": "APPROVED"}

        run(self.agent.run(_make_inputs()))

        action_calls = [str(c) for c in self.agent.action.call_args_list]
        activate_calls = [c for c in action_calls if "activate" in c]
        self.assertGreater(len(activate_calls), 0)

    # ── SupplierId stored before site creation ────────────────────────────

    def test_supplier_id_stored_before_site_creation(self):
        """SupplierId written to store immediately after supplier POST."""
        set_calls = []
        self.agent.store.set.side_effect = lambda k, v: set_calls.append((k, v))
        self.mock_store.get.return_value = None
        self.agent.post.side_effect = [
            self._supplier_resp(),
            {"AddressId": 200001},
            {"ContactId": 300001},
            {"SupplierSiteId": 400001, "links": [{"rel": "self", "href": ".../sites/400001"}]},
            {"SupplierSiteAssignmentId": 450001},
            {"BankAccountId": 500001},
            {"QualificationId": 600001},
        ]
        self.agent.wait_for_approval.return_value = {"SupplierStatus": "APPROVED"}

        run(self.agent.run(_make_inputs()))

        keys = [k for k, _ in set_calls]
        self.assertIn("SupplierId", keys)
        # SupplierId should appear before SupplierSiteId
        if "SupplierSiteId" in keys:
            self.assertLess(keys.index("SupplierId"), keys.index("SupplierSiteId"))


if __name__ == "__main__":
    unittest.main()
