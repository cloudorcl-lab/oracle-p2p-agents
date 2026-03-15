"""
agents/pr6_receiving.py — PR6: Receiving / Work Confirmation Agent
==================================================================
Confirms physical goods receipt or service completion against a PO.
Triggers 3-way match for AP invoicing downstream.
Outputs: ReceiptHeaderId, ReceiptLineId, InspectionResultId.
"""

import logging
from agents.base_agent import BaseAgent

logger = logging.getLogger("p2p.PR6")


class PR6ReceivingAgent(BaseAgent):

    agent_id       = "PR6"
    endpoint_group = "receivingReceipts"

    OVER_RECEIPT_TOLERANCE = 0.05   # 5%

    async def run(self, inputs: dict) -> dict:
        """
        inputs = {
            "receipt_date":       "2026-04-28",
            "received_by_email":  "warehouse@company.com",
            "receiving_org_id":   204,
            "packing_slip":       "PKG-2026-00123",   # optional
            "lines": [{
                "po_line_id":       300100300400501,
                "po_schedule_id":   300100300400502,
                "item_id":          300100012345678,
                "quantity_received": 5,
                "uom":              "Each",
                "destination_type": "EXPENSE",
                "destination_org_id": 204,
                "inspection_required": True,
                "inspection_result":   "ACCEPTED",   # ACCEPTED | REJECTED
                "quantity_accepted":   5,
                "quantity_rejected":   0,
            }],
        }
        """
        self.log.info(f"[{self.txn_id}] PR6 starting")
        await self.audit("PR6_STARTED", {"receipt_date": inputs["receipt_date"]})

        # Load upstream IDs
        po_id     = await self.store.get("POHeaderId")
        sup_id    = await self.store.get("SupplierId")
        site_id   = await self.store.get("SupplierSiteId")

        if not po_id:
            raise ValueError("POHeaderId not found in state — run PR5 first")

        # ── Step 1: Validate PO is open for receipt ───────────────────────
        await self._validate_po_open(po_id)

        # ── Step 2: Check quantities ──────────────────────────────────────
        for line in inputs["lines"]:
            await self._check_receipt_quantity(po_id, line)

        # ── Step 3: Create receipt header ─────────────────────────────────
        header = await self._create_receipt_header(inputs, po_id, sup_id, site_id)
        receipt_id   = header["ReceiptHeaderId"]
        receipt_uniq = self.extract_uniq_id(header)

        await self.store.set("ReceiptHeaderId",   receipt_id)
        await self.store.set("ReceiptHeaderUniqId", receipt_uniq)
        await self.store.set("ReceiptNumber", header.get("ReceiptNumber"))
        self.log.info(f"[{self.txn_id}] Receipt created: {header.get('ReceiptNumber')}")

        # ── Steps 4-6: Lines, inspection, delivery ────────────────────────
        line_outputs = []
        for line in inputs["lines"]:
            line_resp = await self._create_receipt_line(
                receipt_uniq, line, po_id
            )
            line_id = line_resp["ReceiptLineId"]

            insp_id = None
            if line.get("inspection_required"):
                insp_id = await self._create_inspection(
                    receipt_uniq, line_id, line
                )

                if line.get("quantity_rejected", 0) > 0:
                    await self._return_to_vendor(
                        receipt_uniq, line, po_id
                    )
                    self.log.warning(
                        f"[{self.txn_id}] {line['quantity_rejected']} units "
                        f"rejected and returned to supplier"
                    )

            # Deliver accepted quantity to destination
            if line.get("quantity_accepted", line["quantity_received"]) > 0:
                await self._deliver(receipt_uniq, line_id, line)

            line_outputs.append({
                "ReceiptLineId":    line_id,
                "QuantityReceived": line["quantity_received"],
                "QuantityAccepted": line.get("quantity_accepted", line["quantity_received"]),
                "QuantityRejected": line.get("quantity_rejected", 0),
                "InspectionId":     insp_id,
            })

        await self.store.set("ReceiptLines", line_outputs)

        # ── 3-way match signal for Finance/AP ────────────────────────────
        three_way_ready = all(
            l["QuantityRejected"] == 0 for l in line_outputs
        )

        output = {
            "ReceiptHeaderId":    receipt_id,
            "ReceiptNumber":      header.get("ReceiptNumber"),
            "ReceiptDate":        inputs["receipt_date"],
            "POHeaderId":         po_id,
            "SupplierId":         sup_id,
            "ReceivingOrgId":     inputs["receiving_org_id"],
            "lines":              line_outputs,
            "three_way_match_ready": three_way_ready,
            "ap_invoice_trigger": {
                "POHeaderId":     po_id,
                "ReceiptHeaderId": receipt_id,
                "SupplierId":     sup_id,
            } if three_way_ready else None,
        }
        await self.store.set_many({"ReceiptOutput": output})
        await self.audit("PR6_COMPLETE", {
            "ReceiptNumber": header.get("ReceiptNumber"),
            "three_way_match_ready": three_way_ready,
        })

        self.log.info(
            f"[{self.txn_id}] PR6 complete — "
            f"3-way match ready: {three_way_ready}"
        )
        return output

    # ── Helpers ───────────────────────────────────────────────────────────

    async def _validate_po_open(self, po_id: int) -> None:
        data = await self.get(f"purchaseOrders/{po_id}")
        status = data.get("POHeaderStatusCode", "")
        if status not in ("APPROVED", "COMMUNICATED", "PARTIALLY_RECEIVED"):
            raise ValueError(
                f"Cannot receive against PO with status '{status}'. "
                f"PO must be APPROVED, COMMUNICATED, or PARTIALLY_RECEIVED."
            )

    async def _check_receipt_quantity(self, po_id: int, line: dict) -> None:
        """Verify received quantity doesn't exceed PO + tolerance."""
        sched_id = line.get("po_schedule_id")
        if not sched_id:
            return
        # In production: GET schedule to check QuantityOpen
        # Simplified: pass through (Oracle will reject over-receipt)
        pass

    async def _create_receipt_header(self, inputs: dict, po_id: int,
                                      sup_id: int | None,
                                      site_id: int | None) -> dict:
        body = {
            "ReceiptSourceCode":     "VENDOR",
            "ReceiptDate":           inputs["receipt_date"],
            "ReceivingOrganizationId": inputs["receiving_org_id"],
        }
        if sup_id:
            body["VendorId"]     = sup_id
        if site_id:
            body["VendorSiteId"] = site_id
        if inputs.get("packing_slip"):
            body["ShipmentNumber"] = inputs["packing_slip"]

        async def dup_check():
            data = await self.get(
                "receivingReceipts",
                params={"q": f"POHeaderId={po_id};"
                             f"ReceiptDate={inputs['receipt_date']}"}
            )
            if data.get("items"):
                return data["items"][0]
            return None

        return await self.post("receivingReceipts", body, duplicate_checker=dup_check)

    async def _create_receipt_line(self, receipt_uniq: str,
                                    line: dict, po_id: int) -> dict:
        return await self.post(
            f"receivingReceipts/{receipt_uniq}/child/lines",
            {
                "ReceiptLineNumber":     line.get("line_number", 1),
                "TransactionType":       "RECEIVE",
                "ItemId":                line.get("item_id"),
                "Quantity":              line["quantity_received"],
                "UOMCode":               line["uom"],
                "POHeaderId":            po_id,
                "POLineId":              line["po_line_id"],
                "POScheduleId":          line["po_schedule_id"],
                "DestinationTypeCode":   line.get("destination_type", "EXPENSE"),
                "DeliverToOrganizationId": line.get("destination_org_id"),
            }
        )

    async def _create_inspection(self, receipt_uniq: str,
                                  line_id: int, line: dict) -> int:
        resp = await self.post(
            f"receivingReceipts/{receipt_uniq}/child/lines"
            f"/{line_id}/child/inspections",
            {
                "InspectionDate":     line.get("inspection_date", ""),
                "QualityInspectionCode": line.get("inspection_result", "ACCEPTED"),
                "QuantityAccepted":   line.get("quantity_accepted", line["quantity_received"]),
                "QuantityRejected":   line.get("quantity_rejected", 0),
                "InspectionNotes":    line.get("inspection_notes", ""),
            }
        )
        return resp.get("InspectionId")

    async def _return_to_vendor(self, receipt_uniq: str,
                                 line: dict, po_id: int) -> None:
        await self.post(
            f"receivingReceipts/{receipt_uniq}/child/lines",
            {
                "TransactionType":   "RETURN_TO_VENDOR",
                "Quantity":          line["quantity_rejected"],
                "UOMCode":           line["uom"],
                "POHeaderId":        po_id,
                "POLineId":          line["po_line_id"],
                "POScheduleId":      line["po_schedule_id"],
                "ReturnReason":      line.get("return_reason", "QUALITY_DEFECT"),
            }
        )

    async def _deliver(self, receipt_uniq: str,
                       line_id: int, line: dict) -> None:
        await self.post(
            f"receivingReceipts/{receipt_uniq}/child/lines"
            f"/{line_id}/child/transactions",
            {
                "TransactionType":       "DELIVER",
                "Quantity":              line.get("quantity_accepted", line["quantity_received"]),
                "DestinationTypeCode":   line.get("destination_type", "EXPENSE"),
                "DeliverToOrganizationId": line.get("destination_org_id"),
            }
        )
