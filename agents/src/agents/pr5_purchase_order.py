"""
agents/pr5_purchase_order.py — PR5: Purchase Order Agent
=========================================================
Creates POs from approved requisitions, negotiation awards, or BPA references.
Outputs: POHeaderId, POLineId, POScheduleId, PODistributionId.
"""

import logging
from agents.base_agent import BaseAgent

logger = logging.getLogger("p2p.PR5")


class PR5PurchaseOrderAgent(BaseAgent):

    agent_id       = "PR5"
    endpoint_group = "purchaseOrders"

    async def run(self, inputs: dict) -> dict:
        """
        inputs = {
            "supplier_id":       300100099887766,
            "supplier_site_id":  300100099887767,
            "buyer_id":          300100178696854,
            "currency":          "USD",
            "bill_to_location_id": 204,
            "ship_to_location_id": 204,
            "payment_terms_id":   1001,
            "description":        "PO for desktop computers",
            "lines": [{
                "item_id":           300100012345678,
                "item_description":  "Standard Desktop",   # if no item_id
                "quantity":          5,
                "uom":               "Each",
                "unit_price":        1050.00,
                "need_by_date":      "2026-04-30",
                "agreement_id":      300100011223344,      # from PR4 (optional)
                "agreement_line_id": 300100011223345,
                "schedules": [{
                    "schedule_number": 1,
                    "quantity":        5,
                    "need_by_date":    "2026-04-30",
                    "ship_to_org_id":  204,
                    "distributions": [{
                        "distribution_number": 1,
                        "quantity_ordered":    5,
                        "charge_account_id":   300100055667788,
                    }],
                }],
            }],
        }
        """
        self.log.info(f"[{self.txn_id}] PR5 starting")
        await self.audit("PR5_STARTED", {"description": inputs.get("description")})

        # Load upstream IDs from state
        req_id = await self.store.get("RequisitionHeaderId")

        # ── Step 1: Validate supplier ─────────────────────────────────────
        await self._validate_supplier(inputs["supplier_id"],
                                       inputs["supplier_site_id"])

        # ── Step 2: Check for existing PO (idempotency) ───────────────────
        po_header, uniq_id = await self._get_or_create_po(inputs, req_id)
        po_id = po_header["POHeaderId"]

        await self.store.set("POHeaderId",    po_id)
        await self.store.set("POHeaderUniqId", uniq_id)
        await self.store.set("OrderNumber", po_header.get("OrderNumber"))
        self.log.info(f"[{self.txn_id}] PO created: {po_header.get('OrderNumber')}")

        # ── Steps 3-6: Lines, schedules, distributions ────────────────────
        line_outputs = []
        for line in inputs["lines"]:
            line_resp = await self._create_line(uniq_id, line, req_id)
            line_id   = line_resp["POLineId"]

            schedule_outputs = []
            for sched in line.get("schedules", []):
                sched_resp = await self._create_schedule(uniq_id, line_id, sched)
                sched_id   = sched_resp["POScheduleId"]

                for dist in sched.get("distributions", []):
                    await self._create_distribution(uniq_id, line_id, sched_id, dist)

                schedule_outputs.append({"POScheduleId": sched_id, **sched_resp})

            line_outputs.append({"POLineId": line_id, "schedules": schedule_outputs})

        await self.store.set("POLines", line_outputs)

        # ── Step 7: Calculate tax ─────────────────────────────────────────
        self.log.info(f"[{self.txn_id}] Calculating PO tax...")
        await self.action(f"purchaseOrders/{uniq_id}/action/calculateTax")

        # ── Step 8: Submit for approval ───────────────────────────────────
        self.log.info(f"[{self.txn_id}] Submitting PO for approval...")
        try:
            await self.action(f"purchaseOrders/{uniq_id}/action/submitForApproval")
        except Exception:
            self.log.warning(f"[{self.txn_id}] PO submit may have timed out — polling status")

        # ── Step 9: Poll approval ─────────────────────────────────────────
        result = await self.wait_for_approval(
            path=f"purchaseOrders/{uniq_id}",
            status_field="POHeaderStatusCode",
            terminal={"APPROVED", "REJECTED", "CANCELLED"},
            poll_interval=30,
            timeout_hours=48,
        )

        if result["POHeaderStatusCode"] != "APPROVED":
            raise POApprovalError(
                f"PO {po_header.get('OrderNumber')} was "
                f"{result['POHeaderStatusCode']}"
            )

        self.log.info(f"[{self.txn_id}] PO APPROVED")

        # ── Step 10: Transmit to supplier ─────────────────────────────────
        try:
            await self.action(f"purchaseOrders/{uniq_id}/action/communicate")
        except Exception:
            self.log.warning(f"[{self.txn_id}] PO transmit failed — may need manual send")

        output = {
            "POHeaderId":        po_id,
            "OrderNumber":       po_header.get("OrderNumber"),
            "POHeaderStatusCode": "APPROVED",
            "SupplierId":        inputs["supplier_id"],
            "SupplierSiteId":    inputs["supplier_site_id"],
            "CurrencyCode":      inputs.get("currency", "USD"),
            "lines":             line_outputs,
        }
        await self.store.set_many({"POOutput": output, "POStatus": "APPROVED"})
        await self.audit("PR5_COMPLETE", {"OrderNumber": po_header.get("OrderNumber")})
        return output

    # ── Helpers ───────────────────────────────────────────────────────────

    async def _validate_supplier(self, supplier_id: int,
                                  site_id: int) -> None:
        data = await self.get(f"suppliers/{supplier_id}")
        if data.get("SupplierStatus") != "ACTIVE":
            raise ValueError(f"Supplier {supplier_id} is not ACTIVE")

    async def _get_or_create_po(self, inputs: dict,
                                 req_id: int | None) -> tuple[dict, str]:
        # Idempotency: check if PO already exists for this requisition
        if req_id:
            data = await self.get(
                "purchaseOrders",
                params={"q": f"RequisitionHeaderId={req_id}"}
            )
            if data.get("items"):
                item = data["items"][0]
                self.log.info(f"[{self.txn_id}] PO already exists: {item.get('OrderNumber')}")
                return item, self.extract_uniq_id(item)

        async def dup_check():
            if not req_id:
                return None
            data = await self.get("purchaseOrders",
                                   params={"q": f"RequisitionHeaderId={req_id}"})
            if data.get("items"):
                return data["items"][0]
            return None

        body = {
            "SupplierId":        inputs["supplier_id"],
            "SupplierSiteId":    inputs["supplier_site_id"],
            "BuyerId":           inputs["buyer_id"],
            "CurrencyCode":      inputs.get("currency", "USD"),
            "BillToLocationId":  inputs.get("bill_to_location_id"),
            "ShipToLocationId":  inputs.get("ship_to_location_id"),
            "PaymentTermsId":    inputs.get("payment_terms_id"),
            "PODescription":     inputs.get("description", ""),
        }
        if req_id:
            body["RequisitionHeaderId"] = req_id

        resp = await self.post("purchaseOrders", body, duplicate_checker=dup_check)
        return resp, self.extract_uniq_id(resp)

    async def _create_line(self, header_uniq: str, line: dict,
                            req_id: int | None) -> dict:
        body = {
            "LineNumber":   line.get("line_number", 1),
            "LineType":     line.get("line_type", "Goods"),
            "Quantity":     line["quantity"],
            "UOMCode":      line["uom"],
            "UnitPrice":    line["unit_price"],
            "NeedByDate":   line["need_by_date"],
        }
        if line.get("item_id"):
            body["ItemId"] = line["item_id"]
        else:
            body["ItemDescription"] = line.get("item_description", "")

        if line.get("agreement_id"):
            body["AgreementId"]     = line["agreement_id"]
            body["AgreementLineId"] = line.get("agreement_line_id")

        req_lines = await self.store.get("RequisitionLines", [])
        if req_lines:
            body["RequisitionLineId"] = req_lines[0].get("RequisitionLineId")

        return await self.post(f"purchaseOrders/{header_uniq}/child/lines", body)

    async def _create_schedule(self, header_uniq: str,
                                line_id: int, sched: dict) -> dict:
        return await self.post(
            f"purchaseOrders/{header_uniq}/child/lines/{line_id}/child/schedules",
            {
                "ScheduleNumber":      sched.get("schedule_number", 1),
                "Quantity":            sched["quantity"],
                "NeedByDate":          sched["need_by_date"],
                "ShipToOrganizationId": sched.get("ship_to_org_id"),
            }
        )

    async def _create_distribution(self, header_uniq: str, line_id: int,
                                    sched_id: int, dist: dict) -> None:
        await self.post(
            f"purchaseOrders/{header_uniq}/child/lines/{line_id}"
            f"/child/schedules/{sched_id}/child/distributions",
            {
                "DistributionNumber": dist["distribution_number"],
                "ChargeAccountId":    dist.get("charge_account_id"),
                "QuantityOrdered":    dist["quantity_ordered"],
                "ProjectId":          dist.get("project_id"),
                "TaskId":             dist.get("task_id"),
            }
        )


class POApprovalError(Exception):
    pass
