"""
agents/pr1_supplier.py — PR1: Supplier Registration Agent
=========================================================
Onboards new suppliers. Root dependency for all downstream agents.
Outputs: SupplierId, SupplierSiteId, BankAccountId, QualificationIds.

RESUME-FROM-CHECKPOINT: Every child step checks Redis before POSTing.
If a step's output ID is already cached (from a prior run that failed
mid-sequence), that step is skipped and the cached ID is reused.
This prevents orphaned partial supplier records on re-run.
"""

import logging
from agents.base_agent import BaseAgent
from oracle_retry import OracleNonRetryableError

logger = logging.getLogger("p2p.PR1")


class PR1SupplierAgent(BaseAgent):

    agent_id       = "PR1"
    endpoint_group = "suppliers"

    async def run(self, inputs: dict) -> dict:
        """
        Full supplier onboarding sequence.

        inputs = {
            "supplier_name":  "Acme Corp",
            "tax_id":         "12-3456789",
            "duns_number":    "123456789",       # optional
            "supplier_type":  "CORPORATION",
            "contact": { "first_name", "last_name", "email", "phone" },
            "address": { "line1", "city", "state", "postal_code", "country" },
            "payment_terms":  "NET30",
            "bank": { "name", "branch", "account_number", "routing_number",
                      "account_type", "swift" },
            "qualifications": [{ "type", "cert_number", "issue_date",
                                  "expiry_date", "issuing_authority" }],
            "procurement_bu": "US1 Business Unit",
        }
        """
        self.log.info(f"[{self.txn_id}] PR1 starting — {inputs['supplier_name']}")
        await self.audit("PR1_STARTED", {"supplier_name": inputs["supplier_name"]})

        # ── Step 1 & 2: Supplier header (create or resume) ───────────────
        supplier_id, uniq_id = await self._get_or_create_supplier(inputs)

        # ── Step 3: Address ───────────────────────────────────────────────
        addr_id = await self._get_or_create_address(uniq_id, inputs["address"])

        # ── Step 4: Contact ───────────────────────────────────────────────
        await self._get_or_create_contact(uniq_id, inputs["contact"])

        # ── Step 5: Purchasing site ───────────────────────────────────────
        site_id, site_uniq = await self._get_or_create_site(
            uniq_id, addr_id, inputs["procurement_bu"], inputs["supplier_name"]
        )

        # ── Step 6: Site BU assignment (required for PR creation) ─────────
        await self._get_or_create_site_assignment(
            supplier_id, site_id, inputs["procurement_bu"]
        )

        # ── Step 7: Bank account ──────────────────────────────────────────
        bank_id = await self._get_or_create_bank_account(uniq_id, inputs["bank"])

        # ── Step 8: Qualifications ────────────────────────────────────────
        qual_ids = await self._get_or_create_qualifications(
            uniq_id, inputs.get("qualifications", [])
        )

        # ── Step 9: Submit for approval ───────────────────────────────────
        self.log.info(f"[{self.txn_id}] Submitting supplier for approval...")
        await self.action(f"suppliers/{uniq_id}/action/submitForApproval")

        # ── Step 10: Poll for approval ────────────────────────────────────
        result = await self.wait_for_approval(
            path=f"suppliers/{uniq_id}",
            status_field="SupplierStatus",
            terminal={"APPROVED", "REJECTED", "INACTIVE"},
            poll_interval=30,
            timeout_hours=1,
        )

        if result["SupplierStatus"] != "APPROVED":
            raise SupplierRejectedError(
                f"Supplier {inputs['supplier_name']} was "
                f"{result['SupplierStatus']}: {result.get('RejectionReason', '')}"
            )

        # ── Step 11: Activate ─────────────────────────────────────────────
        await self.action(f"suppliers/{uniq_id}/action/activate")
        self.log.info(f"[{self.txn_id}] Supplier ACTIVE — ID {supplier_id}")

        output = {
            "SupplierId":       supplier_id,
            "SupplierName":     inputs["supplier_name"],
            "SupplierStatus":   "ACTIVE",
            "SupplierSiteId":   site_id,
            "SupplierSiteName": f"{inputs['supplier_name']}_PURCHASING",
            "ProcurementBU":    inputs["procurement_bu"],
            "BankAccountId":    bank_id,
            "QualificationIds": qual_ids,
            "PaymentTermsCode": inputs.get("payment_terms", "NET30"),
        }
        await self.store.set_many(output)
        await self.audit("PR1_COMPLETE", output)
        return output

    # ── Resume-from-checkpoint helpers ────────────────────────────────────
    # Each method follows the same pattern:
    #   1. Check Redis for the cached ID (prior run may have succeeded here)
    #   2. If found: log and return cached ID — skip the POST entirely
    #   3. If not found: POST, store ID to Redis immediately, return ID
    # This ensures a failed run never re-creates objects already written to Oracle.

    async def _get_or_create_supplier(self, inputs: dict) -> tuple[int, str]:
        cached_id  = await self.store.get("SupplierId")
        cached_uid = await self.store.get("SupplierUniqId")
        if cached_id and cached_uid:
            self.log.info(f"[{self.txn_id}] RESUME step 1 — supplier cached: {cached_id}")
            return int(cached_id), cached_uid

        async def dup_check():
            cid = await self.store.get("SupplierId")
            cuid = await self.store.get("SupplierUniqId")
            if cid:
                return {"SupplierId": int(cid), "links": [{"href": f"suppliers/{cuid}"}]}
            return None

        # Correct Oracle field names (validated against live GET of existing supplier):
        # - "Supplier" not "SupplierName"
        # - "BusinessRelationshipCode": must be "SPEND_AUTHORIZED" for transactional use
        # - "TaxOrganizationTypeCode": writeable code field (TaxOrganizationType is read-only display)
        # - TaxRegistrationNumber requires TaxRegistrationCountry — omit until known
        body = {
            "Supplier":                 inputs["supplier_name"],
            "BusinessRelationshipCode": "SPEND_AUTHORIZED",
            "TaxOrganizationTypeCode":  inputs.get("supplier_type", "CORPORATION"),
        }
        if inputs.get("duns_number"):
            body["DUNSNumber"] = inputs["duns_number"]

        resp = await self.post("suppliers", body, duplicate_checker=dup_check)
        supplier_id = resp["SupplierId"]
        uniq_id     = self.extract_uniq_id(resp)

        await self.store.set("SupplierId",    supplier_id)
        await self.store.set("SupplierUniqId", uniq_id)
        return supplier_id, uniq_id

    async def _get_or_create_address(self, uniq_id: str, addr: dict) -> int:
        cached = await self.store.get("SupplierAddressId")
        if cached:
            self.log.info(f"[{self.txn_id}] RESUME step 3 — address cached: {cached}")
            return int(cached)

        resp = await self.post(
            f"suppliers/{uniq_id}/child/addresses",
            {
                "AddressLine1": addr["line1"],
                "City":         addr["city"],
                "State":        addr.get("state", ""),
                "PostalCode":   addr["postal_code"],
                "Country":      addr.get("country", "US"),
                "AddressPurposes": [
                    {"Purpose": "ORDERING", "PrimaryFlag": True},
                    {"Purpose": "PAY"},
                    {"Purpose": "RFQ"},
                ],
            }
        )
        addr_id = resp.get("AddressId") or resp.get("SupplierAddressId")
        await self.store.set("SupplierAddressId", addr_id)
        return addr_id

    async def _get_or_create_contact(self, uniq_id: str, contact: dict) -> int:
        cached = await self.store.get("ContactId")
        if cached:
            self.log.info(f"[{self.txn_id}] RESUME step 4 — contact cached: {cached}")
            return int(cached)

        resp = await self.post(
            f"suppliers/{uniq_id}/child/contacts",
            {
                "FirstName":    contact["first_name"],
                "LastName":     contact["last_name"],
                "EmailAddress": contact["email"],
                "PhoneNumber":  contact.get("phone", ""),
            }
        )
        contact_id = resp.get("ContactId") or resp.get("PersonId")
        await self.store.set("ContactId", contact_id)
        return contact_id

    async def _get_or_create_site(self, uniq_id: str, addr_id: int,
                                   bu: str, supplier_name: str) -> tuple[int, str]:
        cached_id   = await self.store.get("SupplierSiteId")
        cached_uniq = await self.store.get("SupplierSiteUniqId")
        if cached_id and cached_uniq:
            self.log.info(f"[{self.txn_id}] RESUME step 5 — site cached: {cached_id}")
            return int(cached_id), cached_uniq

        site_name = f"{supplier_name[:20]}_PURCH"   # max 30 chars
        resp = await self.post(
            f"suppliers/{uniq_id}/child/sites",
            {
                "SupplierSiteName":    site_name,
                "ProcurementBU":       bu,
                "AddressId":           addr_id,
                "PurchasingSiteFlag":  True,
                "PaySiteFlag":         True,
                "PurchasingCurrency":  "USD",
                "PaymentCurrency":     "USD",
                "CommunicationMethod": "EMAIL",
            }
        )
        site_id   = resp["SupplierSiteId"]
        site_uniq = self.extract_uniq_id(resp)
        await self.store.set("SupplierSiteId",    site_id)
        await self.store.set("SupplierSiteUniqId", site_uniq)
        return site_id, site_uniq

    async def _get_or_create_site_assignment(self, supplier_id: int,
                                              site_id: int, bu: str) -> int:
        cached = await self.store.get("SiteAssignmentId")
        if cached:
            self.log.info(f"[{self.txn_id}] RESUME step 6 — site assignment cached: {cached}")
            return int(cached)

        resp = await self.post(
            f"suppliers/{supplier_id}/child/sites/{site_id}/child/assignments",
            {
                "ClientBU":   bu,
                "BillToBU":   bu,
                "ActiveFlag": True,
            }
        )
        assignment_id = resp.get("SupplierSiteAssignmentId")
        await self.store.set("SiteAssignmentId", assignment_id)
        return assignment_id

    async def _get_or_create_bank_account(self, uniq_id: str, bank: dict) -> int:
        cached = await self.store.get("BankAccountId")
        if cached:
            self.log.info(f"[{self.txn_id}] RESUME step 7 — bank account cached: {cached}")
            return int(cached)

        try:
            resp = await self.post(
                f"suppliers/{uniq_id}/child/bankAccounts",
                {
                    "BankName":          bank["name"],
                    "BankBranchName":    bank.get("branch", ""),
                    "BankAccountNumber": bank["account_number"],
                    "BankAccountType":   bank.get("account_type", "CHECKING"),
                    "RoutingNumber":     bank.get("routing_number", ""),
                    "SWIFTCode":         bank.get("swift", ""),
                    "CurrencyCode":      "USD",
                    "PrimaryFlag":       True,
                }
            )
            bank_id = resp.get("BankAccountId") or resp.get("ExternalBankAccountId")
            await self.store.set("BankAccountId", bank_id)
            return bank_id
        except Exception as e:
            # child/bankAccounts returns 404 on some Oracle instances (managed via AP)
            self.log.warning(f"[{self.txn_id}] Bank account creation skipped: {e}")
            return None

    async def _get_or_create_qualifications(self, uniq_id: str,
                                             qualifications: list) -> list:
        cached = await self.store.get("QualificationIds")
        if cached:
            self.log.info(f"[{self.txn_id}] RESUME step 8 — qualifications cached")
            return cached if isinstance(cached, list) else [cached]

        qual_ids = []
        for q in qualifications:
            try:
                resp = await self.post(
                    f"suppliers/{uniq_id}/child/qualifications",
                    {
                        "QualificationTypeCode": q["type"],
                        "CertificationNumber":   q.get("cert_number", ""),
                        "IssueDate":             q.get("issue_date", ""),
                        "ExpiryDate":            q.get("expiry_date", ""),
                        "IssuingAuthority":       q.get("issuing_authority", ""),
                        "QualificationStatus":   "APPROVED",
                    }
                )
                qual_ids.append(resp.get("QualificationId"))
            except Exception as e:
                self.log.warning(f"[{self.txn_id}] Qualification creation skipped: {e}")

        await self.store.set("QualificationIds", qual_ids)
        return qual_ids


class SupplierRejectedError(Exception):
    pass
