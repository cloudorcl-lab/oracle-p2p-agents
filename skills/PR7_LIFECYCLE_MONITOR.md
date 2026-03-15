---
name: PR7-lifecycle-monitor
description: "Agent PR7 — Oracle Fusion Cloud P2P Lifecycle Monitor. Read-only agent that tracks every document across the full Source-to-Pay lifecycle, detects gaps, surfaces SLA violations, and produces audit trails. Build this first — zero risk, immediate visibility."
agent_id: PR7
phase: Lifecycle Monitoring & Gap Detection
endpoints: 0 (read-only GETs only)
---

# PR7: Lifecycle Monitor Agent

## Purpose
Read-only monitoring agent that answers the most important procurement question: **"Where is this document, who owns it right now, and is anything wrong?"**

This agent runs continuously, polls document statuses, detects stuck or stalled documents, and surfaces alerts to buyers and managers. It never creates or modifies data.

**Build this first.** It has zero risk of corrupting data, and it gives you full visibility into the Oracle environment while you build the other 6 agents.

---

## What It Monitors

### 1. Document Status Tracking
Every document across the P2P chain has a status. The monitor tracks transitions and flags documents that are stuck.

```
Tracked Objects:
  Suppliers              → SupplierStatus
  PurchaseRequisitions   → DocumentStatus
  SupplierNegotiations   → NegotiationStatus
  SupplierAgreements     → AgreementStatusCode
  PurchaseOrders         → POHeaderStatusCode
  ReceivingReceipts      → (receipt date vs PO NeedByDate)
  WorkConfirmations      → ConfirmationStatus
```

### 2. Gap Detection Rules

```
GAP 1 — Approved PR with no PO (stale requisition)
  Query: GET /purchaseRequisitions
         ?q=DocumentStatus=APPROVED
         ?q=LastUpdateDate<{today-3days}
  Alert: "PR {number} approved {n} days ago — no PO created"
  Route: Buyer notification

GAP 2 — PO pending approval > SLA threshold
  Query: GET /purchaseOrders
         ?q=POHeaderStatusCode=PENDING_APPROVAL
         ?q=LastUpdateDate<{today-2days}
  Alert: "PO {number} stuck in approval for {n} days"
  Route: Procurement manager escalation

GAP 3 — PO approved but not received by NeedByDate
  Query: GET /purchaseOrders
         ?q=POHeaderStatusCode=APPROVED,COMMUNICATED
  Filter locally: NeedByDate < today AND QuantityReceived = 0
  Alert: "PO {number} delivery overdue by {n} days"
  Route: Buyer + supplier notification

GAP 4 — Partial receipt — remaining quantity overdue
  Query: GET /purchaseOrders
         ?q=POHeaderStatusCode=PARTIALLY_RECEIVED
  Filter: NeedByDate < today for open schedules
  Alert: "PO {number} partially received — {qty} units still outstanding"
  Route: Buyer + warehouse notification

GAP 5 — Receipt confirmed but no AP invoice (3-way match stalled)
  Query: GET /receivingReceipts
         ?q=ReceiptDate<{today-5days}
  Cross-check: Verify AP invoice exists for each receipt
  Alert: "Receipt {number} confirmed {n} days ago — AP invoice not created"
  Route: AP team notification

GAP 6 — Agreement expiring within 30 days
  Query: GET /supplierAgreements
         ?q=AgreementStatusCode=ACTIVE
  Filter: EndDate between today and today+30days
  Alert: "Agreement {number} expires in {n} days — initiate renewal sourcing"
  Route: Buyer + category manager notification

GAP 7 — Supplier qualification expiring within 60 days
  Query: GET /suppliers/{SupplierId}/child/qualifications
  Filter: ExpiryDate between today and today+60days AND IsMandatory=Y
  Alert: "Supplier {name} mandatory cert {type} expires in {n} days"
  Route: Supplier manager notification

GAP 8 — Negotiation past due date with no award
  Query: GET /supplierNegotiations
         ?q=NegotiationStatus=PUBLISHED
  Filter: ResponseDueDate < today AND no award records
  Alert: "Negotiation {number} past due date — no award decision made"
  Route: Buyer escalation
```

### 3. SLA Thresholds (Configurable in config.yaml)

```
PR Approval SLA:    2 business days (alert at 1 day, escalate at 2)
PO Approval SLA:    2 business days (alert at 1 day, escalate at 2)
PO Delivery SLA:    Per PO NeedByDate (alert 3 days before, escalate on day)
Receipt to Invoice: 5 business days (alert at day 5)
Negotiation Award:  3 business days after response due date
Agreement Renewal:  30 days before expiry (alert), 14 days (escalate)
```

---

## API Queries (All GET — No POST/PATCH)

### Full Lifecycle Status for One Transaction
```
# Step 1: Find the requisition
GET /purchaseRequisitions?q=Requisition={pr_number}
  Extract: RequisitionHeaderId, DocumentStatus, SupplierId, Items

# Step 2: Find linked PO
GET /purchaseOrders?q=RequisitionHeaderId={req_id}
  Extract: POHeaderId, OrderNumber, POHeaderStatusCode, NeedByDate

# Step 3: Find receipts for this PO
GET /receivingReceipts?q=POHeaderId={po_header_id}
  Extract: ReceiptHeaderId, ReceiptNumber, ReceiptDate, QuantityReceived

# Step 4: Check AP invoice (Finance domain)
GET /invoices?q=POHeaderId={po_header_id}
  Extract: InvoiceId, InvoiceStatus, PaymentStatus

# Step 5: Assemble timeline
Return: {
  pr_number, pr_status, pr_approved_date,
  po_number, po_status, po_approval_date, po_need_by_date,
  receipt_number, receipt_date, quantity_received,
  invoice_number, invoice_status, payment_status,
  gaps: [list of detected gaps],
  cycle_time_days: receipt_date - pr_submission_date
}
```

### Supplier 360 View
```
GET /suppliers/{SupplierId}
GET /suppliers/{SupplierId}/child/supplierSites
GET /suppliers/{SupplierId}/child/qualifications
GET /suppliers/{SupplierId}/child/bankAccounts
GET /purchaseOrders?q=SupplierId={supplierId}&limit=10&orderBy=CreationDate:desc
GET /receivingReceipts?q=VendorId={supplierId}&limit=10
GET /supplierAgreements?q=SupplierId={supplierId};AgreementStatusCode=ACTIVE

Assemble:
  supplier_profile, active_sites, certifications, active_agreements,
  recent_pos, recent_receipts, on_time_delivery_rate, quality_rejection_rate
```

### Agreement Utilization Report
```
GET /supplierAgreements?q=AgreementStatusCode=ACTIVE
For each agreement:
  GET /supplierAgreements/{AgreementId}
  Extract: AgreementAmount, AmountAgreed, AmountReleased (consumed)
  
  utilization_rate = AmountReleased / AgreementAmount × 100
  
  If utilization_rate > 90%: Alert "Agreement near exhaustion"
  If utilization_rate < 10% and agreement is 50% through its term: Alert "Under-utilized agreement"
```

### Spend Analysis by Supplier
```
GET /purchaseOrders
    ?q=CreationDate>={start_date};CreationDate<={end_date}
    &fields=SupplierId,SupplierName,TotalAmount,CurrencyCode,POHeaderStatusCode
    &orderBy=TotalAmount:desc
    &limit=100

Aggregate: total_spend_by_supplier, po_count, avg_po_value
```

### 3-Way Match Status Check
```
GET /purchaseOrders/{POHeaderId}
  Extract: OrderNumber, TotalAmount, POLineId list

GET /receivingReceipts?q=POHeaderId={po_header_id}
  Extract: QuantityReceived per line

GET /invoices?q=POHeaderId={po_header_id}  (Finance domain)
  Extract: InvoiceAmount, InvoiceQuantity

3-way match logic:
  PO qty match:       abs(po_qty - receipt_qty) / po_qty < tolerance (5%)
  Price match:        abs(po_price - invoice_price) / po_price < tolerance (5%)
  
  Status:
    ALL_MATCHED:      Auto-approve invoice for payment
    QUANTITY_MISMATCH: Hold invoice, route to buyer + warehouse
    PRICE_MISMATCH:    Hold invoice, route to buyer + AP
    RECEIPT_MISSING:   Hold invoice, route to buyer + warehouse
```

---

## Dashboard Metrics (Produced by PR7)

```python
def get_dashboard_metrics(start_date, end_date) -> dict:
    return {
        "cycle_time": {
            "avg_pr_to_po_days": ...,     # PR approved → PO created
            "avg_po_to_receipt_days": ..., # PO approved → first receipt
            "avg_receipt_to_pay_days": ... # Receipt → AP payment
        },
        "volume": {
            "prs_submitted": ...,
            "prs_approved": ...,
            "pos_created": ...,
            "pos_approved": ...,
            "receipts_confirmed": ...
        },
        "quality": {
            "on_time_delivery_rate": ...,  # % POs received by NeedByDate
            "three_way_match_rate": ...,   # % invoices that auto-matched
            "rejection_rate": ...,          # % receipts with quality rejects
            "po_amendment_rate": ...        # % POs that required amendment
        },
        "compliance": {
            "maverick_spend_pct": ...,     # % spend outside agreements
            "contracts_expiring_30d": ...,
            "qualifications_expiring_60d": ...,
            "pos_without_pr": ...           # Direct POs bypassing requisition
        },
        "exceptions": {
            "stale_requisitions": [...],
            "overdue_pos": [...],
            "missing_receipts": [...],
            "three_way_match_failures": [...]
        }
    }
```

---

## Alert Routing Matrix

| Gap Type | Severity | Primary Recipient | Escalation (after SLA) |
|----------|----------|------------------|----------------------|
| PR stuck in approval | MEDIUM | Approver | Procurement Manager |
| PO stuck in approval | MEDIUM | Approver | VP Procurement |
| Delivery overdue | HIGH | Buyer + Supplier | Procurement Manager |
| 3-way match failure | HIGH | Buyer + AP | Controller |
| Agreement expiring | MEDIUM | Buyer | Category Manager |
| Qualification expiring | HIGH | Supplier Manager | Procurement Manager |
| Fraud hold triggered | CRITICAL | CFO | CEO |
| Budget exhausted | HIGH | Requester + Finance | Budget Manager |

---

## Python Skeleton

```python
import httpx
import asyncio
from datetime import datetime, timedelta

BASE_URL = "https://{host}/fscmRestApi/resources/11.13.18.05"
HEADERS = {
    "REST-Framework-Version": "3",
    "Content-Type": "application/json",
    "Authorization": "Bearer {access_token}"
}

async def run_gap_detection(config: dict) -> list[dict]:
    """Run all 8 gap detection checks, return list of alerts."""
    async with httpx.AsyncClient(headers=HEADERS, timeout=30) as client:
        alerts = []
        alerts += await check_stale_requisitions(client, config["pr_approval_sla_days"])
        alerts += await check_overdue_pos(client)
        alerts += await check_missing_receipts(client, config["po_to_receipt_sla_days"])
        alerts += await check_receipt_to_invoice_gap(client, config["receipt_to_invoice_sla_days"])
        alerts += await check_expiring_agreements(client, config["agreement_renewal_lead_days"])
        alerts += await check_expiring_qualifications(client, config["qualification_lead_days"])
        alerts += await check_stale_negotiations(client)
        alerts += await check_three_way_match_failures(client)
        return sorted(alerts, key=lambda x: x["severity"], reverse=True)

async def check_stale_requisitions(client, sla_days=2) -> list[dict]:
    cutoff = (datetime.now() - timedelta(days=sla_days)).strftime("%Y-%m-%d")
    r = await client.get(
        f"{BASE_URL}/purchaseRequisitions",
        params={"q": f"DocumentStatus=APPROVED;LastUpdateDate<{cutoff}",
                "fields": "RequisitionHeaderId,Requisition,PreparerEmail,LastUpdateDate",
                "limit": 100}
    )
    return [{
        "gap_type": "STALE_REQUISITION",
        "severity": "MEDIUM",
        "document": item["Requisition"],
        "message": f"PR {item['Requisition']} approved but no PO created",
        "owner": item["PreparerEmail"]
    } for item in r.json().get("items", [])]
```

---

*Agent: PR7 | Endpoints: GET only | API version: 26A | Build order: FIRST*
