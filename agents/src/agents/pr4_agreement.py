"""
agents/pr4_agreement.py — PR4: Agreement Management Agent
=========================================================
Creates Blanket Purchase Agreements (BPA) and Contract Purchase Agreements (CPA).
Locks in negotiated pricing with tiered volume discounts.
Outputs: AgreementId, AgreementNumber, AgreementLineId, PriceTierId(s).

Referenced by: PR2 (requisition lines via AgreementId/AgreementLineId)
               PR5 (PO lines via AgreementId/AgreementLineId)
"""

import logging
from agents.base_agent import BaseAgent

logger = logging.getLogger("p2p.PR4")


class PR4AgreementAgent(BaseAgent):

    agent_id       = "PR4"
    endpoint_group = "supplierAgreements"

    async def run(self, inputs: dict) -> dict:
        """
        inputs = {
            "agreement_type":   "BPA",               # BPA | CPA
            "supplier_id":      300100099887766,
            "supplier_site_id": 300100099887767,
            "start_date":       "2026-04-01",
            "end_date":         "2027-03-31",
            "agreement_amount": 50000.00,
            "currency":         "USD",
            "payment_terms":    "NET30",
            "description":      "Annual desktop supply agreement",
            "procurement_bu":   "Vision Operations",
            "negotiation_id":   300100200300400,      # optional, from PR3
            "sla_description":  "99% on-time delivery",  # optional
            "documents": [],                          # optional
            "lines": [{
                "item_id":    300100012345678,
                "item_number": "AS54888",             # optional, for reference
                "quantity":   100,
                "uom":        "Each",
                "unit_price": 1050.00,
                "need_by_date": "2027-03-31",         # optional
                "negotiation_line_id": 300100200300401,  # optional, from PR3
                "price_tiers": [
                    {"min_qty": 1,  "max_qty": 50,  "price": 1100.00},
                    {"min_qty": 51, "max_qty": 999, "price": 1050.00},
                ],
            }],
        }
        """
        self.log.info(f"[{self.txn_id}] PR4 starting — {inputs.get('agreement_type', 'BPA')}")
        await self.audit("PR4_STARTED", {
            "agreement_type": inputs.get("agreement_type"),
            "supplier_id":    inputs.get("supplier_id"),
        })

        # Optional: NegotiationId forwarded from PR3
        neg_id = await self.store.get("NegotiationId") or inputs.get("negotiation_id")

        # ── Step 1: Check for existing active agreement ────────────────────
        existing, uniq_id = await self._check_duplicate(
            inputs["supplier_id"], inputs["agreement_type"]
        )
        if existing:
            self.log.info(f"[{self.txn_id}] Active agreement already exists — "
                          f"returning {existing.get('AgreementNumber')}")
            await self.store.set("AgreementId",     existing["AgreementId"])
            await self.store.set("AgreementUniqId", uniq_id)
            await self.store.set("AgreementNumber", existing.get("AgreementNumber"))
            return {
                "AgreementId":         existing["AgreementId"],
                "AgreementNumber":     existing.get("AgreementNumber"),
                "AgreementType":       inputs["agreement_type"],
                "AgreementStatusCode": "ACTIVE",
                "SupplierId":          inputs["supplier_id"],
                "SupplierSiteId":      inputs["supplier_site_id"],
                "StartDate":           existing.get("StartDate"),
                "EndDate":             existing.get("EndDate"),
                "AgreementAmount":     existing.get("AgreementAmount"),
                "RemainingAmount":     existing.get("RemainingAmount"),
                "lines":               [],
            }

        # ── Step 2: Create agreement header ───────────────────────────────
        header, uniq_id = await self._create_header(inputs, neg_id)
        agr_id = header["AgreementId"]

        # Store immediately — recoverable on re-run
        await self.store.set("AgreementId",     agr_id)
        await self.store.set("AgreementUniqId", uniq_id)
        await self.store.set("AgreementNumber", header.get("AgreementNumber"))
        self.log.info(f"[{self.txn_id}] Agreement created: {header.get('AgreementNumber')}")

        # ── Steps 3-4: Lines and price tiers ──────────────────────────────
        line_outputs = []
        for i, line in enumerate(inputs.get("lines", [])):
            # Validate before any line POST
            self._validate_price_tiers(line.get("price_tiers", []))

            line_resp = await self._create_line(uniq_id, line, line_number=i + 1)
            line_id   = line_resp["AgreementLineId"]

            # Store first line ID for PR2/PR5 state bridge
            if i == 0:
                await self.store.set("AgreementLineId", line_id)

            tier_ids = []
            for j, tier in enumerate(line.get("price_tiers", [])):
                tier_id = await self._add_price_tier(uniq_id, line_id, tier, tier_number=j + 1)
                tier_ids.append(tier_id)

            line_outputs.append({
                "AgreementLineId": line_id,
                "LineNumber":      i + 1,
                "ItemId":          line.get("item_id"),
                "ItemNumber":      line.get("item_number"),
                "UnitPrice":       line.get("unit_price"),
                "UOMCode":         line.get("uom"),
                "price_tiers":     [
                    {
                        "TierNumber":       j + 1,
                        "MinimumQuantity":  t["min_qty"],
                        "MaximumQuantity":  t["max_qty"],
                        "TierPrice":        t["price"],
                    }
                    for j, t in enumerate(line.get("price_tiers", []))
                ],
                "tier_ids": tier_ids,
            })

        await self.store.set("AgreementLines", line_outputs)

        # ── Step 5: SLA deliverable (optional) ────────────────────────────
        if inputs.get("sla_description"):
            await self._add_deliverable(uniq_id, inputs)

        # ── Step 6: Attach supporting documents (optional) ────────────────
        for doc in inputs.get("documents", []):
            await self._attach_document(uniq_id, doc)

        # ── Step 7: Submit for approval ───────────────────────────────────
        self.log.info(f"[{self.txn_id}] Submitting agreement for approval...")
        try:
            await self.action(f"supplierAgreements/{uniq_id}/action/submitForApproval")
        except Exception:
            self.log.warning(f"[{self.txn_id}] submitForApproval may have timed out — polling")

        # ── Step 8: Poll approval ─────────────────────────────────────────
        result = await self.wait_for_approval(
            path=f"supplierAgreements/{uniq_id}",
            status_field="ApprovalStatus",
            terminal={"APPROVED", "REJECTED"},
            poll_interval=30,
            timeout_hours=1,
        )

        if result.get("ApprovalStatus") != "APPROVED":
            raise AgreementRejectedError(
                f"Agreement {header.get('AgreementNumber')} was "
                f"{result.get('ApprovalStatus')}: "
                f"{result.get('RejectionReason', '')}"
            )

        self.log.info(f"[{self.txn_id}] Agreement APPROVED")

        # ── Step 9: Activate ──────────────────────────────────────────────
        await self.action(f"supplierAgreements/{uniq_id}/action/activate")
        final = await self.get(f"supplierAgreements/{uniq_id}")
        if final.get("AgreementStatusCode") != "ACTIVE":
            raise AgreementActivationError(
                f"Agreement {header.get('AgreementNumber')} status is "
                f"{final.get('AgreementStatusCode')} — expected ACTIVE"
            )

        self.log.info(f"[{self.txn_id}] Agreement ACTIVE — {header.get('AgreementNumber')}")

        output = {
            "AgreementId":         agr_id,
            "AgreementNumber":     header.get("AgreementNumber"),
            "AgreementType":       inputs["agreement_type"],
            "AgreementStatusCode": "ACTIVE",
            "SupplierId":          inputs["supplier_id"],
            "SupplierSiteId":      inputs["supplier_site_id"],
            "StartDate":           inputs["start_date"],
            "EndDate":             inputs["end_date"],
            "AgreementAmount":     inputs["agreement_amount"],
            "RemainingAmount":     inputs["agreement_amount"],
            "lines":               line_outputs,
        }
        await self.store.set_many({"AgreementOutput": output, "AgreementStatus": "ACTIVE"})
        await self.audit("PR4_COMPLETE", {"AgreementNumber": header.get("AgreementNumber")})
        return output

    # ── Helpers ───────────────────────────────────────────────────────────

    async def _check_duplicate(self, supplier_id: int,
                                agreement_type: str) -> tuple[dict | None, str | None]:
        """Step 1. GET active agreement for supplier/type. Returns (dict, uniq_id) or (None, None)."""
        data = await self.get(
            "supplierAgreements",
            params={"q": f"SupplierId={supplier_id};"
                         f"AgreementType={agreement_type};"
                         f"AgreementStatusCode=ACTIVE"}
        )
        items = data.get("items", [])
        if items:
            item = items[0]
            return item, self.extract_uniq_id(item)
        return None, None

    async def _create_header(self, inputs: dict,
                              neg_id: int | None) -> tuple[dict, str]:
        """Step 2. POST /supplierAgreements with dup_checker closure."""
        supplier_id = inputs["supplier_id"]
        agr_type    = inputs["agreement_type"]

        async def dup_check():
            item, _ = await self._check_duplicate(supplier_id, agr_type)
            if item:
                return item
            return None

        body = {
            "AgreementType":              agr_type,
            "SupplierId":                 supplier_id,
            "SupplierSiteId":             inputs["supplier_site_id"],
            "ProcurementBU":              inputs["procurement_bu"],
            "CurrencyCode":               inputs.get("currency", "USD"),
            "StartDate":                  inputs["start_date"],
            "EndDate":                    inputs["end_date"],
            "AgreementAmount":            inputs["agreement_amount"],
            "PaymentTermsCode":           inputs.get("payment_terms", "NET30"),
            "AgreementDescription":       inputs.get("description", ""),
            "AutoCreateOrdersFlag":       "N",
            "ConsumeAgreementOnApproval": "Y",
        }
        if neg_id:
            body["NegotiationId"] = neg_id

        resp = await self.post("supplierAgreements", body, duplicate_checker=dup_check)
        return resp, self.extract_uniq_id(resp)

    async def _create_line(self, agr_uniq: str, line: dict,
                            line_number: int) -> dict:
        """Step 3. POST /supplierAgreements/{UniqId}/child/lines."""
        body = {
            "LineNumber":         line_number,
            "UOMCode":            line["uom"],
            "UnitPrice":          line["unit_price"],
            "AllowPriceOverride": "N",
            "MatchApprovalLevel": "THREE_WAY",
        }
        if line.get("item_id"):
            body["ItemId"] = line["item_id"]
        else:
            body["ItemDescription"] = line.get("item_description", "")

        if line.get("quantity"):
            body["Quantity"] = line["quantity"]
        if line.get("need_by_date"):
            body["NeedByDate"] = line["need_by_date"]
        if line.get("negotiation_line_id"):
            body["NegotiationLineId"] = line["negotiation_line_id"]

        return await self.post(
            f"supplierAgreements/{agr_uniq}/child/lines", body
        )

    def _validate_price_tiers(self, tiers: list[dict]) -> None:
        """Pre-validate tiers before any POST. No gaps allowed in qty ranges."""
        if not tiers:
            return
        sorted_tiers = sorted(tiers, key=lambda t: t["min_qty"])
        for i in range(len(sorted_tiers) - 1):
            current_max = sorted_tiers[i]["max_qty"]
            next_min    = sorted_tiers[i + 1]["min_qty"]
            if current_max + 1 != next_min:
                raise PriceTierGapError(
                    f"Price tier gap: tier {i + 1} ends at {current_max}, "
                    f"tier {i + 2} starts at {next_min}. "
                    f"Expected {next_min} = {current_max + 1}."
                )

    async def _add_price_tier(self, agr_uniq: str, line_id: int,
                               tier: dict, tier_number: int) -> int | None:
        """Step 4 (per tier). POST price tier. Returns PriceTierId."""
        resp = await self.post(
            f"supplierAgreements/{agr_uniq}/child/lines/{line_id}/child/priceTiers",
            {
                "TierNumber":      tier_number,
                "MinimumQuantity": tier["min_qty"],
                "MaximumQuantity": tier["max_qty"],
                "TierPrice":       tier["price"],
            }
        )
        return resp.get("PriceTierId")

    async def _add_deliverable(self, agr_uniq: str, inputs: dict) -> None:
        """Step 5. POST SLA deliverable."""
        await self.post(
            f"supplierAgreements/{agr_uniq}/child/deliverables",
            {
                "DeliverableType":  "PERFORMANCE",
                "Description":      inputs["sla_description"],
                "DueDate":          inputs["end_date"],
                "ResponsibleParty": "SUPPLIER",
            }
        )

    async def _attach_document(self, agr_uniq: str, doc: dict) -> None:
        """Step 6 (per doc). POST attachment."""
        await self.post(
            f"supplierAgreements/{agr_uniq}/child/attachments",
            {
                "FileName":    doc["file_name"],
                "FileType":    doc.get("file_type", "PDF"),
                "FileContent": doc["file_content"],
                "Description": doc.get("description", ""),
            }
        )

    # ── Amendment flow (called externally when renewal is needed) ─────────

    async def create_amendment(self, agr_uniq: str,
                                line_updates: list[dict]) -> dict:
        """
        POST createAmendment → PATCH lines → submit → activate.
        Returns the activated amendment response.
        """
        self.log.info(f"[{self.txn_id}] Creating agreement amendment...")
        amendment = await self.action(
            f"supplierAgreements/{agr_uniq}/action/createAmendment"
        )
        new_uniq = self.extract_uniq_id(amendment) if amendment else agr_uniq

        for update in line_updates:
            line_id = update.pop("AgreementLineId")
            await self.patch(
                f"supplierAgreements/{new_uniq}/child/lines/{line_id}",
                update,
            )

        try:
            await self.action(f"supplierAgreements/{new_uniq}/action/submitForApproval")
        except Exception:
            self.log.warning(f"[{self.txn_id}] Amendment submitForApproval timed out — polling")

        result = await self.wait_for_approval(
            path=f"supplierAgreements/{new_uniq}",
            status_field="ApprovalStatus",
            terminal={"APPROVED", "REJECTED"},
            poll_interval=30,
            timeout_hours=1,
        )
        if result.get("ApprovalStatus") != "APPROVED":
            raise AgreementRejectedError(
                f"Amendment rejected: {result.get('RejectionReason', '')}"
            )

        await self.action(f"supplierAgreements/{new_uniq}/action/activate")
        return await self.get(f"supplierAgreements/{new_uniq}")


class AgreementRejectedError(Exception):
    pass


class AgreementActivationError(Exception):
    pass


class PriceTierGapError(Exception):
    pass
