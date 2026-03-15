"""
agents/pr7_monitor.py — PR7: Lifecycle Monitor Agent
=====================================================
Read-only. Tracks every P2P document, detects gaps, surfaces alerts.
Build and test this FIRST — zero risk of creating or modifying data.

Run it at any time to get a full lifecycle snapshot:
    monitor = PR7LifecycleMonitor(transaction_id="TXN-001")
    report  = await monitor.run({"pr_number": "10505856"})
    gaps    = report["gaps"]
"""

import asyncio
import logging
from datetime import datetime, timedelta

from agents.base_agent import BaseAgent

logger = logging.getLogger("p2p.PR7")


class PR7LifecycleMonitor(BaseAgent):

    agent_id       = "PR7"
    endpoint_group = "monitor"   # shared circuit breaker for all GETs

    # ── Gap detection thresholds (overridden by config.yaml in production) ──
    PR_APPROVAL_SLA_DAYS         = 2
    PO_APPROVAL_SLA_DAYS         = 2
    RECEIPT_TO_INVOICE_SLA_DAYS  = 5
    AGREEMENT_RENEWAL_LEAD_DAYS  = 30
    QUALIFICATION_LEAD_DAYS      = 60

    # ─────────────────────────────────────────────────────────────────────

    async def run(self, inputs: dict) -> dict:
        """
        Main entry point. Pass a PR number or PO number.

        inputs = {
            "pr_number":  "10505856",    # OR
            "po_number":  "PO-2026-001"  # either works
        }

        Returns a report dict:
        {
            "pr":      {...},
            "po":      {...},
            "receipt": {...},
            "gaps":    [{"gap_type": ..., "severity": ..., "message": ...}]
        }
        """
        pr_number = inputs.get("pr_number")
        po_number = inputs.get("po_number")

        gaps   = []
        report = {"pr": None, "po": None, "receipt": None, "gaps": []}

        # ── Fetch PR ──────────────────────────────────────────────────────
        if pr_number:
            pr = await self._get_pr(pr_number)
            report["pr"] = pr
            if pr:
                await self.store.set("RequisitionHeaderId", pr.get("RequisitionHeaderId"))
                gaps += self._check_pr_gaps(pr)

        # ── Fetch PO ──────────────────────────────────────────────────────
        req_id = await self.store.get("RequisitionHeaderId")
        if req_id or po_number:
            po = await self._get_po(req_id=req_id, po_number=po_number)
            report["po"] = po
            if po:
                await self.store.set("POHeaderId", po.get("POHeaderId"))
                gaps += self._check_po_gaps(po)

        # ── Fetch Receipt ─────────────────────────────────────────────────
        po_id = await self.store.get("POHeaderId")
        if po_id:
            receipt = await self._get_receipt(po_id)
            report["receipt"] = receipt
            if receipt:
                gaps += self._check_receipt_gaps(receipt, report.get("po"))

        report["gaps"]             = gaps
        report["gap_count"]        = len(gaps)
        report["critical_count"]   = sum(1 for g in gaps if g["severity"] == "CRITICAL")
        report["generated_at"]     = datetime.utcnow().isoformat()

        await self.audit("LIFECYCLE_SCAN", {
            "gap_count": len(gaps),
            "critical":  report["critical_count"],
        })

        self._log_summary(report)
        return report

    # ── Bulk gap scan (runs without a specific transaction) ───────────────

    async def scan_all_gaps(self) -> list[dict]:
        """
        Scan the full Oracle environment for stale/stuck documents.
        Use this for the weekly scheduled review.
        """
        all_gaps = []
        all_gaps += await self._scan_stale_prs()
        all_gaps += await self._scan_overdue_pos()
        all_gaps += await self._scan_expiring_agreements()
        all_gaps += await self._scan_expiring_qualifications()
        all_gaps += await self._scan_stuck_negotiations()
        return sorted(all_gaps, key=lambda g: g["severity"], reverse=True)

    # ── Individual fetch helpers ──────────────────────────────────────────

    async def _get_pr(self, pr_number: str) -> dict | None:
        data = await self.get(
            "purchaseRequisitions",
            params={"q": f"Requisition={pr_number}",
                    "fields": "RequisitionHeaderId,Requisition,DocumentStatus,"
                              "PreparerEmail,ApprovedDate,LastUpdateDate"}
        )
        items = data.get("items", [])
        return items[0] if items else None

    async def _get_po(self, req_id: int | None = None,
                      po_number: str | None = None) -> dict | None:
        if req_id:
            q = f"RequisitionHeaderId={req_id}"
        elif po_number:
            q = f"OrderNumber={po_number}"
        else:
            return None

        data = await self.get(
            "purchaseOrders",
            params={"q": q,
                    "fields": "POHeaderId,OrderNumber,POHeaderStatusCode,"
                              "SupplierId,SupplierName,ApprovalStatus,"
                              "NeedByDate,LastUpdateDate"}
        )
        items = data.get("items", [])
        return items[0] if items else None

    async def _get_receipt(self, po_header_id: int) -> dict | None:
        data = await self.get(
            "receivingReceipts",
            params={"q": f"POHeaderId={po_header_id}",
                    "fields": "ReceiptHeaderId,ReceiptNumber,ReceiptDate,"
                              "VendorId,ReceivingOrganizationId"}
        )
        items = data.get("items", [])
        return items[0] if items else None

    # ── Gap check methods ─────────────────────────────────────────────────

    def _check_pr_gaps(self, pr: dict) -> list[dict]:
        gaps = []
        status       = pr.get("DocumentStatus", "")
        last_updated = pr.get("LastUpdateDate", "")

        if status == "PENDING_APPROVAL":
            days_waiting = self._days_since(last_updated)
            if days_waiting > self.PR_APPROVAL_SLA_DAYS:
                gaps.append({
                    "gap_type":  "PR_STUCK_IN_APPROVAL",
                    "severity":  "MEDIUM",
                    "document":  pr.get("Requisition"),
                    "message":   f"PR {pr.get('Requisition')} has been pending "
                                 f"approval for {days_waiting} days (SLA: {self.PR_APPROVAL_SLA_DAYS})",
                    "owner":     pr.get("PreparerEmail"),
                    "action":    "Escalate to procurement manager",
                })

        if status == "APPROVED" and not pr.get("ApprovedDate"):
            gaps.append({
                "gap_type":  "PR_APPROVED_NO_PO",
                "severity":  "MEDIUM",
                "document":  pr.get("Requisition"),
                "message":   f"PR {pr.get('Requisition')} approved but no PO created",
                "action":    "Buyer to convert PR to PO",
            })

        return gaps

    def _check_po_gaps(self, po: dict) -> list[dict]:
        gaps = []
        status       = po.get("POHeaderStatusCode", "")
        need_by      = po.get("NeedByDate", "")
        last_updated = po.get("LastUpdateDate", "")

        if status == "PENDING_APPROVAL":
            days = self._days_since(last_updated)
            if days > self.PO_APPROVAL_SLA_DAYS:
                gaps.append({
                    "gap_type": "PO_STUCK_IN_APPROVAL",
                    "severity": "MEDIUM",
                    "document": po.get("OrderNumber"),
                    "message":  f"PO {po.get('OrderNumber')} stuck in approval "
                                f"for {days} days",
                    "action":   "Escalate to VP Procurement",
                })

        if status in ("APPROVED", "COMMUNICATED") and need_by:
            days_overdue = self._days_overdue(need_by)
            if days_overdue > 0:
                gaps.append({
                    "gap_type": "PO_DELIVERY_OVERDUE",
                    "severity": "HIGH",
                    "document": po.get("OrderNumber"),
                    "message":  f"PO {po.get('OrderNumber')} delivery overdue "
                                f"by {days_overdue} days",
                    "action":   "Contact supplier, escalate to buyer",
                })

        return gaps

    def _check_receipt_gaps(self, receipt: dict, po: dict | None) -> list[dict]:
        gaps = []
        receipt_date = receipt.get("ReceiptDate", "")
        days_since   = self._days_since(receipt_date)

        if days_since > self.RECEIPT_TO_INVOICE_SLA_DAYS:
            gaps.append({
                "gap_type": "RECEIPT_NO_INVOICE",
                "severity": "HIGH",
                "document": receipt.get("ReceiptNumber"),
                "message":  f"Receipt {receipt.get('ReceiptNumber')} confirmed "
                            f"{days_since} days ago — AP invoice not detected",
                "action":   "AP team to create invoice against this receipt",
            })

        return gaps

    # ── Bulk scan methods ─────────────────────────────────────────────────

    async def _scan_stale_prs(self) -> list[dict]:
        cutoff = (datetime.utcnow() - timedelta(days=self.PR_APPROVAL_SLA_DAYS)
                  ).strftime("%Y-%m-%d")
        data = await self.get(
            "purchaseRequisitions",
            params={"q": f"DocumentStatus=PENDING_APPROVAL;LastUpdateDate<{cutoff}",
                    "fields": "Requisition,PreparerEmail,LastUpdateDate",
                    "limit": 100}
        )
        return [{
            "gap_type": "STALE_PR",
            "severity": "MEDIUM",
            "document": item["Requisition"],
            "message":  f"PR {item['Requisition']} stuck in approval",
            "owner":    item.get("PreparerEmail"),
        } for item in data.get("items", [])]

    async def _scan_overdue_pos(self) -> list[dict]:
        today = datetime.utcnow().strftime("%Y-%m-%d")
        data  = await self.get(
            "purchaseOrders",
            params={"q": f"POHeaderStatusCode=APPROVED,COMMUNICATED;"
                         f"NeedByDate<{today}",
                    "fields": "OrderNumber,SupplierName,NeedByDate",
                    "limit": 100}
        )
        return [{
            "gap_type": "OVERDUE_PO",
            "severity": "HIGH",
            "document": item["OrderNumber"],
            "message":  f"PO {item['OrderNumber']} from {item.get('SupplierName')} "
                        f"is past NeedByDate {item.get('NeedByDate')}",
        } for item in data.get("items", [])]

    async def _scan_expiring_agreements(self) -> list[dict]:
        soon  = (datetime.utcnow() + timedelta(days=self.AGREEMENT_RENEWAL_LEAD_DAYS)
                 ).strftime("%Y-%m-%d")
        today = datetime.utcnow().strftime("%Y-%m-%d")
        data  = await self.get(
            "supplierAgreements",
            params={"q": f"AgreementStatusCode=ACTIVE;"
                         f"EndDate>={today};EndDate<={soon}",
                    "fields": "AgreementNumber,SupplierName,EndDate",
                    "limit": 50}
        )
        return [{
            "gap_type": "AGREEMENT_EXPIRING",
            "severity": "MEDIUM",
            "document": item["AgreementNumber"],
            "message":  f"Agreement {item['AgreementNumber']} with "
                        f"{item.get('SupplierName')} expires {item.get('EndDate')}",
            "action":   "Initiate renewal sourcing",
        } for item in data.get("items", [])]

    async def _scan_expiring_qualifications(self) -> list[dict]:
        soon  = (datetime.utcnow() + timedelta(days=self.QUALIFICATION_LEAD_DAYS)
                 ).strftime("%Y-%m-%d")
        today = datetime.utcnow().strftime("%Y-%m-%d")
        # Note: must loop over active suppliers and check per-supplier
        # Simplified here — in production use a supplier query + loop
        return []   # TODO: add supplier loop when supplier list available

    async def _scan_stuck_negotiations(self) -> list[dict]:
        today = datetime.utcnow().strftime("%Y-%m-%d")
        data  = await self.get(
            "supplierNegotiations",
            params={"q": f"NegotiationStatus=PUBLISHED;ResponseDueDate<{today}",
                    "fields": "NegotiationNumber,ResponseDueDate,BuyerName",
                    "limit": 50}
        )
        return [{
            "gap_type": "NEGOTIATION_OVERDUE",
            "severity": "MEDIUM",
            "document": item["NegotiationNumber"],
            "message":  f"Negotiation {item['NegotiationNumber']} past due — "
                        f"no award made",
            "action":   "Buyer to make award decision",
        } for item in data.get("items", [])]

    # ── Utility ───────────────────────────────────────────────────────────

    @staticmethod
    def _days_since(date_str: str) -> int:
        if not date_str:
            return 0
        try:
            dt = datetime.fromisoformat(date_str[:10])
            return (datetime.utcnow() - dt).days
        except ValueError:
            return 0

    @staticmethod
    def _days_overdue(date_str: str) -> int:
        if not date_str:
            return 0
        try:
            due = datetime.fromisoformat(date_str[:10])
            delta = (datetime.utcnow() - due).days
            return max(delta, 0)
        except ValueError:
            return 0

    def _log_summary(self, report: dict) -> None:
        gaps = report["gaps"]
        if not gaps:
            self.log.info(f"[{self.txn_id}] Lifecycle scan complete — no gaps found")
            return
        self.log.warning(f"[{self.txn_id}] {len(gaps)} gap(s) detected:")
        for g in gaps:
            level = logging.CRITICAL if g["severity"] == "CRITICAL" else \
                    logging.WARNING  if g["severity"] == "HIGH"     else \
                    logging.INFO
            self.log.log(level, f"  [{g['severity']}] {g['gap_type']}: {g['message']}")
