"""
agents/pr1_supplier.py — PR1: Supplier Registration Agent
=========================================================
Onboards new suppliers. Root dependency for all downstream agents.
Outputs: SupplierId, SupplierSiteId, BankAccountId, QualificationIds.
"""

import logging
from agents.base_agent import BaseAgent

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
            "procurement_bu": "Vision Operations",
        }
        """
        self.log.info(f"[{self.txn_id}] PR1 starting — {inputs['supplier_name']}")
        await self.audit("PR1_STARTED", {"supplier_name": inputs["supplier_name"]})

        # ── Step 1: Duplicate checks ──────────────────────────────────────
        supplier_id, uniq_id = await self._check_duplicate(
            inputs["tax_id"], inputs["supplier_name"]
        )

        # ── Step 2: Create supplier (if not duplicate) ────────────────────
        if not supplier_id:
            supplier_id, uniq_id = await self._create_supplier(inputs)

        await self.store.set("SupplierId",    supplier_id)
        await self.store.set("SupplierUniqId", uniq_id)

        # ── Step 3: Address ───────────────────────────────────────────────
        addr_id = await self._create_address(uniq_id, inputs["address"])
        await self.store.set("SupplierAddressId", addr_id)

        # ── Step 4: Contact ───────────────────────────────────────────────
        contact_id = await self._create_contact(uniq_id, inputs["contact"])
        await self.store.set("ContactId", contact_id)

        # ── Step 5: Purchasing site ───────────────────────────────────────
        site_id, site_uniq = await self._create_site(
            uniq_id, addr_id, inputs["procurement_bu"],
            inputs["supplier_name"]
        )
        await self.store.set("SupplierSiteId",    site_id)
        await self.store.set("SupplierSiteUniqId", site_uniq)

        # ── Step 6: Remit-to address on site ─────────────────────────────
        await self._create_site_address(uniq_id, site_uniq, inputs["address"])

        # ── Step 7: Site contact ──────────────────────────────────────────
        await self._create_site_contact(uniq_id, site_uniq, contact_id)

        # ── Step 8: Payment terms ─────────────────────────────────────────
        await self._create_payment_terms(
            uniq_id, site_uniq, inputs.get("payment_terms", "NET30")
        )

        # ── Step 9: Bank account ──────────────────────────────────────────
        bank_id = await self._create_bank_account(uniq_id, inputs["bank"])
        await self.store.set("BankAccountId", bank_id)

        # ── Step 10: Qualifications ───────────────────────────────────────
        qual_ids = []
        for q in inputs.get("qualifications", []):
            qid = await self._create_qualification(uniq_id, q)
            qual_ids.append(qid)
        await self.store.set("QualificationIds", qual_ids)

        # ── Step 11: Submit for approval ──────────────────────────────────
        self.log.info(f"[{self.txn_id}] Submitting supplier for approval...")
        await self.action(f"suppliers/{uniq_id}/action/submitForApproval")

        # ── Step 12: Poll for approval ────────────────────────────────────
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

        # ── Step 13: Activate ─────────────────────────────────────────────
        await self.action(f"suppliers/{uniq_id}/action/activate")
        self.log.info(f"[{self.txn_id}] Supplier ACTIVE — ID {supplier_id}")

        output = {
            "SupplierId":      supplier_id,
            "SupplierName":    inputs["supplier_name"],
            "SupplierStatus":  "ACTIVE",
            "SupplierSiteId":  site_id,
            "SupplierSiteName": f"{inputs['supplier_name']}_PURCHASING",
            "ProcurementBU":   inputs["procurement_bu"],
            "BankAccountId":   bank_id,
            "QualificationIds": qual_ids,
            "PaymentTermsCode": inputs.get("payment_terms", "NET30"),
        }
        await self.store.set_many(output)
        await self.audit("PR1_COMPLETE", output)
        return output

    # ── Private helpers ───────────────────────────────────────────────────

    async def _check_duplicate(self, tax_id: str,
                                name: str) -> tuple[int | None, str | None]:
        """GET before POST. Returns (supplier_id, uniq_id) or (None, None)."""
        # Check by tax ID first (strongest match)
        data = await self.get("suppliers",
                              params={"q": f"TaxOrganizationId={tax_id}"})
        if data.get("items"):
            item = data["items"][0]
            sid  = item["SupplierId"]
            uid  = self.extract_uniq_id(item)
            self.log.info(f"[{self.txn_id}] Supplier exists by tax ID — {sid}")
            return sid, uid

        # Check by name (weaker — Oracle allows same name for different entities)
        data = await self.get("suppliers",
                              params={"q": f"SupplierName={name}"})
        if data.get("items"):
            item = data["items"][0]
            # Only return as duplicate if tax ID also matches
            if item.get("TaxOrganizationId") == tax_id:
                sid = item["SupplierId"]
                uid = self.extract_uniq_id(item)
                self.log.info(f"[{self.txn_id}] Supplier exists by name — {sid}")
                return sid, uid

        return None, None

    async def _create_supplier(self, inputs: dict) -> tuple[int, str]:
        async def dup_check():
            sid, uid = await self._check_duplicate(
                inputs["tax_id"], inputs["supplier_name"]
            )
            if sid:
                return {"SupplierId": sid, "links": []}
            return None

        body = {
            "SupplierName":       inputs["supplier_name"],
            "TaxOrganizationId":  inputs["tax_id"],
            "SupplierType":       inputs.get("supplier_type", "CORPORATION"),
            "EnabledFlag":        "Y",
            "BusinessRelationship": "SPEND_AUTHORIZED",
        }
        if inputs.get("duns_number"):
            body["DUNSNumber"] = inputs["duns_number"]

        resp = await self.post("suppliers", body, duplicate_checker=dup_check)
        return resp["SupplierId"], self.extract_uniq_id(resp)

    async def _create_address(self, uniq_id: str, addr: dict) -> int:
        resp = await self.post(
            f"suppliers/{uniq_id}/child/supplierAddresses",
            {
                "AddressLine1": addr["line1"],
                "City":         addr["city"],
                "State":        addr.get("state", ""),
                "PostalCode":   addr["postal_code"],
                "Country":      addr.get("country", "US"),
                "AddressType":  "HEADQUARTER",
                "PrimaryFlag":  "Y",
            }
        )
        return resp.get("SupplierAddressId") or resp.get("AddressId")

    async def _create_contact(self, uniq_id: str, contact: dict) -> int:
        resp = await self.post(
            f"suppliers/{uniq_id}/child/contacts",
            {
                "FirstName":        contact["first_name"],
                "LastName":         contact["last_name"],
                "EmailAddress":     contact["email"],
                "PhoneNumber":      contact.get("phone", ""),
                "IsPrimaryContact": "Y",
            }
        )
        return resp.get("ContactId") or resp.get("PersonId")

    async def _create_site(self, uniq_id: str, addr_id: int,
                            bu: str, supplier_name: str) -> tuple[int, str]:
        resp = await self.post(
            f"suppliers/{uniq_id}/child/supplierSites",
            {
                "SiteName":                    f"{supplier_name}_PURCHASING",
                "ProcurementBusinessUnit":     bu,
                "SiteType":                    "PURCHASING",
                "EnabledFlag":                 "Y",
                "SiteAddressId":               addr_id,
            }
        )
        return resp["SupplierSiteId"], self.extract_uniq_id(resp)

    async def _create_site_address(self, uniq_id: str, site_uniq: str,
                                    addr: dict) -> None:
        await self.post(
            f"suppliers/{uniq_id}/child/supplierSites/{site_uniq}/child/siteAddresses",
            {
                "AddressLine1": addr["line1"],
                "City":         addr["city"],
                "State":        addr.get("state", ""),
                "PostalCode":   addr["postal_code"],
                "Country":      addr.get("country", "US"),
                "AddressType":  "REMIT_TO",
            }
        )

    async def _create_site_contact(self, uniq_id: str, site_uniq: str,
                                    contact_id: int) -> None:
        await self.post(
            f"suppliers/{uniq_id}/child/supplierSites/{site_uniq}/child/siteContacts",
            {"ContactPersonId": contact_id, "ContactType": "PURCHASING"}
        )

    async def _create_payment_terms(self, uniq_id: str, site_uniq: str,
                                     terms_code: str) -> None:
        await self.post(
            f"suppliers/{uniq_id}/child/supplierSites/{site_uniq}/child/paymentTerms",
            {"PaymentTermsCode": terms_code, "DefaultPaymentTermsFlag": "Y"}
        )

    async def _create_bank_account(self, uniq_id: str, bank: dict) -> int:
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
                "PrimaryFlag":       "Y",
            }
        )
        return resp.get("BankAccountId") or resp.get("ExternalBankAccountId")

    async def _create_qualification(self, uniq_id: str, qual: dict) -> int:
        resp = await self.post(
            f"suppliers/{uniq_id}/child/qualifications",
            {
                "QualificationTypeCode": qual["type"],
                "CertificationNumber":   qual.get("cert_number", ""),
                "IssueDate":             qual.get("issue_date", ""),
                "ExpiryDate":            qual.get("expiry_date", ""),
                "IssuingAuthority":      qual.get("issuing_authority", ""),
                "QualificationStatus":   "APPROVED",
            }
        )
        return resp.get("QualificationId")


class SupplierRejectedError(Exception):
    pass
