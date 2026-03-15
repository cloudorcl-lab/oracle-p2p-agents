---
name: PR5-purchase-order
description: "Agent PR5 — Oracle Fusion Cloud Purchase Order creation, approval, and management. 78 PO endpoints + 67 schedule endpoints + 58 distribution endpoints = 203 total. Creates POs from requisitions, negotiation awards, or agreement references. Manages delivery schedules, distributions, price breaks, and amendments."
agent_id: PR5
phase: Purchase Commitment
endpoints: 203
---

# PR5: Purchase Order Agent

## Purpose
Create and approve Purchase Orders to legally commit company spend with a supplier. The PO is the binding commercial document. It can reference a requisition (PR2), a negotiation award (PR3), or a blanket agreement (PR4).

## Trigger
- Approved requisition converted via RequisitionProcessingRequest (auto-flow from PR2)
- Negotiation award without auto-agreement (PR3 direct-to-PO path)
- Manual buyer-initiated PO against existing BPA

## Three PO Creation Paths

| Path | Source | Required Inputs |
|------|--------|----------------|
| Direct PO | Approved Requisition | RequisitionHeaderId, RequisitionLineId |
| Award PO | Negotiation Award | NegotiationId, AwardId, AwardedLineId |
| Agreement PO | Blanket Agreement | AgreementId, AgreementLineId |

---

## API Call Sequence

### Step 1 — Supplier & Site Validation
```
GET /suppliers/{SupplierId}
  Validate: SupplierStatus = "ACTIVE", EnabledFlag = "Y"

GET /suppliers/{SupplierId}/child/supplierSites/{SupplierSiteId}
  Validate: SiteType = "PURCHASING", EnabledFlag = "Y"
```

### Step 2 — Duplicate PO Check
```
GET /purchaseOrders
    ?q=RequisitionHeaderId={reqId}  OR
    ?q=AgreementId={agrId};SupplierId={suppId}

If PO already exists for this requisition: return existing POHeaderId
```

### Step 3 — Create PO Header
```
POST /purchaseOrders
Body:
{
  "SupplierId": {supplier_id},
  "SupplierSiteId": {site_id},
  "BuyerId": {buyer_id},
  "CurrencyCode": "USD",
  "BillToLocationId": {bill_to_id},
  "ShipToLocationId": {ship_to_id},
  "PaymentTermsId": {terms_id},
  "FreightTermsCode": "FOB",
  "PODescription": "{description}",
  "DocumentStyle": "STANDARD",           ← STANDARD | BLANKET | CONTRACT
  "RequisitionHeaderId": {req_id}        ← Optional: links to PR2 output
}
Capture: POHeaderId, POHeaderUniqId, OrderNumber (the PO number, e.g., "PO-2026-0099")
Expected: POHeaderStatusCode = "INCOMPLETE"
```

### Step 4 — Create PO Lines
```
POST /purchaseOrders/{POHeaderUniqId}/child/lines
Body:
{
  "LineNumber": 1,
  "LineType": "Goods",
  "ItemId": {item_id},                   ← OR ItemDescription for free-text
  "Quantity": {qty},
  "UOMCode": "{uom}",
  "UnitPrice": {price},
  "NeedByDate": "YYYY-MM-DD",
  "PromisedDate": "YYYY-MM-DD",
  "AgreementId": {agreement_id},         ← From PR4 if BPA referenced
  "AgreementLineId": {agreement_line_id},
  "RequisitionLineId": {req_line_id},    ← From PR2 if converting requisition
  "NegotiationLineId": {neg_line_id},    ← From PR3 if converting award
  "SupplierItemNumber": "{supplier_sku}"
}
Capture: POLineId
```

### Step 5 — Create Delivery Schedule
```
POST /purchaseOrders/{POHeaderUniqId}/child/lines/{POLineId}/child/schedules
Body:
{
  "ScheduleNumber": 1,
  "Quantity": {qty},
  "NeedByDate": "YYYY-MM-DD",
  "PromisedDate": "YYYY-MM-DD",
  "ShipToLocationId": {location_id},
  "ShipToOrganizationId": {org_id},
  "ShipmentNumber": 1
}
Capture: POScheduleId

For split deliveries (e.g., 3 units now, 2 units next month):
  Create schedule 1: Quantity=3, NeedByDate=April
  Create schedule 2: Quantity=2, NeedByDate=May
```

### Step 6 — Create Schedule Distributions
```
POST /purchaseOrders/{POHeaderUniqId}/child/lines/{POLineId}/child/schedules/{POScheduleId}/child/distributions
Body:
{
  "DistributionNumber": 1,
  "ChargeAccountId": {gl_account_id},
  "QuantityOrdered": {qty},
  "ProjectId": {project_id},             ← Optional
  "TaskId": {task_id}                    ← Required if ProjectId provided
}

RULE: Sum of all distribution QuantityOrdered must equal schedule Quantity.
Capture: PODistributionId
```

### Step 7 — Add Price Breaks (Optional)
```
POST /purchaseOrders/{POHeaderUniqId}/child/lines/{POLineId}/child/priceBreaks
Body:
{
  "BreakType": "CUMULATIVE",             ← CUMULATIVE | NON_CUMULATIVE
  "Quantity": 50,
  "UnitPrice": 1050.00,
  "BreakCurrencyCode": "USD"
}
```

### Step 8 — Attach Supporting Documents
```
POST /purchaseOrders/{POHeaderUniqId}/child/attachments
Body:
{
  "FileName": "specification.pdf",
  "FileType": "PDF",
  "FileContent": "{base64}",
  "Description": "Technical specification"
}
```

### Step 9 — Calculate Tax
```
POST /purchaseOrders/{POHeaderUniqId}/action/calculateTax
  Must return HTTP 200 before submission
```

### Step 10 — Submit for Approval
```
POST /purchaseOrders/{POHeaderUniqId}/action/submitForApproval
  POHeaderStatusCode transitions: INCOMPLETE → PENDING_APPROVAL
```

### Step 11 — Poll PO Approval
```
GET /purchaseOrders/{POHeaderUniqId}
Poll until: POHeaderStatusCode in [APPROVED, REJECTED, CANCELLED]
Interval: 30 seconds | Timeout: 48 hours

Track: ApprovalStatus, ApprovedDate, ApproverName
```

### Step 12 — Transmit PO to Supplier
```
POST /purchaseOrders/{POHeaderUniqId}/action/communicate
  Sends PO to supplier via configured channel:
  - Oracle iSupplier Portal (preferred)
  - Email (PDF attachment)
  - EDI (if configured)
```

---

## PO Amendment Flow
```
When price, quantity, or delivery date needs to change after approval:

POST /purchaseOrders/{POHeaderUniqId}/action/createAmendment
  → Creates amendment in DRAFT status

PATCH /purchaseOrders/{POHeaderUniqId}  (or child lines/schedules)
  → Apply the changes

POST /purchaseOrders/{POHeaderUniqId}/action/submitForApproval
POST /purchaseOrders/{POHeaderUniqId}/action/communicate
  → Re-send to supplier
```

## PO Status State Machine
```
INCOMPLETE → (submitForApproval) → PENDING_APPROVAL
PENDING_APPROVAL → (approved) → APPROVED (also called "OPEN")
APPROVED → (communicate) → COMMUNICATED
COMMUNICATED → (receipt created in PR6) → PARTIALLY_RECEIVED
PARTIALLY_RECEIVED → (fully received) → CLOSED (receipt complete)
CLOSED (receipt) → (invoice matched) → FINALLY_CLOSED
```

---

## Agent Output Payload (Passed to PR6)
```json
{
  "POHeaderId": 300100300400500,
  "OrderNumber": "PO-2026-0099",
  "POHeaderStatusCode": "APPROVED",
  "SupplierId": 300100099887766,
  "SupplierSiteId": 300100099887767,
  "CurrencyCode": "USD",
  "ApprovalStatus": "APPROVED",
  "lines": [{
    "POLineId": 300100300400501,
    "LineNumber": 1,
    "ItemId": 300100012345678,
    "ItemNumber": "AS54888",
    "Quantity": 5,
    "UOMCode": "Each",
    "UnitPrice": 1050.00,
    "schedules": [{
      "POScheduleId": 300100300400502,
      "ScheduleNumber": 1,
      "Quantity": 5,
      "NeedByDate": "2026-04-30",
      "ShipToOrganizationId": 204,
      "distributions": [{
        "PODistributionId": 300100300400503,
        "ChargeAccountId": 300100055667788,
        "QuantityOrdered": 5
      }]
    }]
  }]
}
```

---

## Error Handling

| Condition | Action |
|-----------|--------|
| Supplier site not purchasing-enabled | Activate site before creating PO |
| Agreement not active | Cannot reference inactive agreement, check status |
| Distribution qty ≠ schedule qty | Recalculate, must = 100% |
| Price exceeds tolerance vs BPA | Route to exception agent for approval |
| PO rejected in approval | Capture reason, route back to buyer |
| Supplier not on ASL for item | Block PO creation, route to sourcing |
| Over-commitment vs agreement amount | Warning — agreement balance will go negative |

---

*Agent: PR5 | Endpoints: 203 | API version: 26A*
