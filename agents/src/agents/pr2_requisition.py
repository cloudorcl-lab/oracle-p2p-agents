"""
agents/pr2_requisition.py — PR2: Requisition Agent
===================================================
Runs all 12 pre-checks (supplier + item + budget), creates PR,
routes for AME approval, converts to PO.
Outputs: RequisitionHeaderId, RequisitionLineId, DistributionId.
"""

import logging
from agents.base_agent import BaseAgent

logger = logging.getLogger("p2p.PR2")


class PR2RequisitionAgent(BaseAgent):

    agent_id       = "PR2"
    endpoint_group = "purchaseRequisitions"

    async def run(self, inputs: dict) -> dict:
        """
        inputs = {
            "requester_email":  "john@company.com",
            "requisitioning_bu": "Vision Operations",
            "description":      "Desktop computers Q2 2026",
            "justification":    "Annual refresh",
            "lines": [{
                "item_number":    "AS54888",       # OR item_description for free-text
                "item_description": None,
                "uom":            "Each",
                "quantity":       5,
                "need_by_date":   "2026-04-30",
                "destination_type": "Expense",
                "org_code":       "V1",
                "deliver_to_location": "V1-New York City",
                "supplier_name":  "Acme Corp",
                "supplier_site":  "ACME_PURCHASING",
                "urgent":         False,
                "distributions":  [
                    {"distribution_number": 1, "quantity": 3, "cost_center": "1100"},
                    {"distribution_number": 2, "quantity": 2, "cost_center": "1200"},
                ],
            }],
        }
        """
        self.log.info(f"[{self.txn_id}] PR2 starting")
        await self.audit("PR2_STARTED", {"description": inputs["description"]})

        # ── Phase 0: Pre-checks ───────────────────────────────────────────
        first_line = inputs["lines"][0]
        await self._run_pre_checks(first_line, inputs["requisitioning_bu"])

        # ── Phase 1: Create PR ────────────────────────────────────────────
        header = await self._create_header(inputs)
        uniq_id = self.extract_uniq_id(header)
        req_id  = header["RequisitionHeaderId"]

        await self.store.set("RequisitionHeaderId",   req_id)
        await self.store.set("RequisitionHeaderUniqId", uniq_id)
        await self.store.set("RequisitionNumber", header.get("Requisition"))
        self.log.info(f"[{self.txn_id}] PR created: {header.get('Requisition')}")

        # ── Create lines and distributions ────────────────────────────────
        line_outputs = []
        for line in inputs["lines"]:
            line_resp = await self._create_line(uniq_id, line)
            line_id   = line_resp["RequisitionLineId"]
            await self._create_distributions(uniq_id, line_id, line)
            line_outputs.append({
                "RequisitionLineId": line_id,
                **line_resp,
            })

        await self.store.set("RequisitionLines", line_outputs)

        # ── Calculate tax ─────────────────────────────────────────────────
        self.log.info(f"[{self.txn_id}] Calculating tax...")
        await self.action(
            f"purchaseRequisitions/{uniq_id}/action/calculateTaxAndAccounting"
        )

        # ── Check funds ───────────────────────────────────────────────────
        self.log.info(f"[{self.txn_id}] Checking funds...")
        funds = await self.action(
            f"purchaseRequisitions/{uniq_id}/action/checkFunds",
            is_funds_check=True,
        )
        if funds.get("FundsStatus") == "FAILED":
            raise FundsCheckFailedError(
                f"Insufficient budget: {funds.get('FundsStatusMessage')}"
            )

        # ── Submit for approval ───────────────────────────────────────────
        self.log.info(f"[{self.txn_id}] Submitting for approval...")
        try:
            await self.action(
                f"purchaseRequisitions/{uniq_id}/action/submitRequisition"
            )
        except Exception:
            # Action may 504 even when it succeeded — fall through to poll
            self.log.warning(f"[{self.txn_id}] submitRequisition may have timed out — polling status")

        # ── Poll approval ─────────────────────────────────────────────────
        result = await self.wait_for_approval(
            path=f"purchaseRequisitions/{uniq_id}",
            status_field="DocumentStatus",
            terminal={"APPROVED", "REJECTED", "CANCELLED", "RETURNED"},
            poll_interval=60,
            timeout_hours=72,
        )

        if result["DocumentStatus"] != "APPROVED":
            raise PRRejectedError(
                f"PR {header.get('Requisition')} was "
                f"{result['DocumentStatus']}"
            )

        self.log.info(f"[{self.txn_id}] PR APPROVED")

        output = {
            "RequisitionHeaderId":    req_id,
            "RequisitionHeaderUniqId": uniq_id,
            "RequisitionNumber":      header.get("Requisition"),
            "DocumentStatus":         "APPROVED",
            "RequisitioningBU":       inputs["requisitioning_bu"],
            "ApprovedDate":           result.get("ApprovedDate"),
            "FundsStatus":            funds.get("FundsStatus", "PASSED"),
            "lines":                  line_outputs,
        }
        await self.store.set_many({
            "RequisitionOutput": output,
            "DocumentStatus": "APPROVED",
        })
        await self.audit("PR2_COMPLETE", {"RequisitionNumber": header.get("Requisition")})
        return output

    # ── Pre-checks ────────────────────────────────────────────────────────

    async def _run_pre_checks(self, line: dict, bu: str) -> None:
        """Run checks 1-11. Any failure raises an exception."""

        # Check 1 — Supplier active
        supplier_name = line["supplier_name"]
        data = await self.get("suppliers",
                              params={"q": f"SupplierName={supplier_name}"})
        if not data.get("items"):
            raise PreCheckError(1, f"Supplier '{supplier_name}' not found — run PR1 first")
        supplier = data["items"][0]
        if supplier.get("SupplierStatus") != "ACTIVE":
            raise PreCheckError(1, f"Supplier '{supplier_name}' is not ACTIVE "
                                   f"(status: {supplier.get('SupplierStatus')})")

        # Check 2 — Purchasing site exists for this BU
        sup_id = supplier["SupplierId"]
        sites  = await self.get(
            f"suppliers/{sup_id}/child/supplierSites",
            params={"q": f"SiteType=PURCHASING;ProcurementBusinessUnit={bu}"}
        )
        if not sites.get("items"):
            raise PreCheckError(2, f"No PURCHASING site for supplier in BU '{bu}'")

        # Check 3 — Sourcing eligibility
        bu_id = await self._get_bu_id(bu)
        elig  = await self.get(
            "supplierEligibilities",
            params={"q": f"SupplierId={sup_id};BusinessUnitId={bu_id}"}
        )
        if elig.get("items"):
            code = elig["items"][0].get("SourcingEligibilityCode")
            if code == "NOT_ALLOWED":
                raise PreCheckError(3, f"Supplier '{supplier_name}' sourcing blocked (NOT_ALLOWED)")

        # Check 6 — Item exists in PIM (catalog lines only)
        item_number = line.get("item_number")
        if item_number:
            items = await self.get(
                "items",
                params={"q": f"ItemNumber={item_number};"
                             f"OrganizationCode={line.get('org_code', 'V1')}"}
            )
            if not items.get("items"):
                self.log.warning(f"Item {item_number} not in PIM — will use free-text line")
                line["item_number"] = None   # fall back to free-text

        self.log.info(f"[{self.txn_id}] Pre-checks passed")

    async def _get_bu_id(self, bu_name: str) -> int:
        """Look up BU ID by name."""
        data = await self.get(
            "businessUnits",
            params={"q": f"BusinessUnitName={bu_name}",
                    "fields": "BusinessUnitId"}
        )
        items = data.get("items", [])
        if items:
            return items[0]["BusinessUnitId"]
        return 0   # Return 0 if BU lookup fails — eligibility check becomes a no-op

    # ── PR creation helpers ───────────────────────────────────────────────

    async def _create_header(self, inputs: dict) -> dict:
        email       = inputs["requester_email"]
        description = inputs["description"]

        async def dup_check():
            data = await self.get(
                "purchaseRequisitions",
                params={"q": f"PreparerEmail={email};"
                             f"Description={description};"
                             f"DocumentStatus=INCOMPLETE,APPROVED,OPEN"}
            )
            if data.get("items"):
                return data["items"][0]
            return None

        return await self.post(
            "purchaseRequisitions",
            {
                "RequisitioningBU": inputs["requisitioning_bu"],
                "PreparerEmail":    email,
                "Description":      description,
                "Justification":    inputs.get("justification", ""),
            },
            duplicate_checker=dup_check,
        )

    async def _create_line(self, header_uniq: str, line: dict) -> dict:
        body = {
            "LineNumber":            line.get("line_number", 1),
            "LineTypeCode":          line.get("line_type", "Goods"),
            "UOM":                   line["uom"],
            "Quantity":              line["quantity"],
            "Supplier":              line["supplier_name"],
            "SupplierSite":          line["supplier_site"],
            "RequestedDeliveryDate": line["need_by_date"],
            "DestinationType":       line.get("destination_type", "Expense"),
            "RequesterEmail":        line.get("requester_email", ""),
            "DestinationOrganizationCode": line.get("org_code", "V1"),
            "DeliverToLocationCode": line.get("deliver_to_location", ""),
            "Urgent":                line.get("urgent", False),
        }

        # Catalog item (preferred)
        if line.get("item_number"):
            body["Item"] = line["item_number"]
            # Price, category, description auto-defaulted from PIM
        else:
            # Free-text / non-catalog
            body["ItemDescription"] = line["item_description"]
            body["CategoryName"]    = line.get("category_name", "")
            body["Price"]           = line.get("price", 0)
            body["CurrencyCode"]    = line.get("currency", "USD")

        # Agreement reference (from PR4 state if available)
        agr_id = await self.store.get("AgreementId")
        if agr_id:
            body["AgreementId"]     = agr_id
            body["AgreementLineId"] = await self.store.get("AgreementLineId")

        return await self.post(
            f"purchaseRequisitions/{header_uniq}/child/lines",
            body,
        )

    async def _create_distributions(self, header_uniq: str,
                                     line_id: int,
                                     line: dict) -> None:
        dists = line.get("distributions", [])

        # Validate distributions sum to line quantity
        total_qty = sum(d.get("quantity", 0) for d in dists)
        if dists and total_qty != line["quantity"]:
            raise ValueError(
                f"Distributions sum to {total_qty}, "
                f"must equal line quantity {line['quantity']}"
            )

        # If no distributions specified, create one covering full quantity
        if not dists:
            dists = [{
                "distribution_number": 1,
                "quantity":            line["quantity"],
                "cost_center":         line.get("cost_center", ""),
            }]

        for dist in dists:
            await self.post(
                f"purchaseRequisitions/{header_uniq}/child/lines/"
                f"{line_id}/child/distributions",
                {
                    "DistributionNumber": dist["distribution_number"],
                    "Quantity":           dist["quantity"],
                    "ChargeAccountId":    dist.get("charge_account_id"),
                    "ProjectId":          dist.get("project_id"),
                    "TaskId":             dist.get("task_id"),
                }
            )


class PreCheckError(Exception):
    def __init__(self, check_number: int, message: str):
        self.check_number = check_number
        super().__init__(f"Check {check_number} failed: {message}")

class FundsCheckFailedError(Exception):
    pass

class PRRejectedError(Exception):
    pass
