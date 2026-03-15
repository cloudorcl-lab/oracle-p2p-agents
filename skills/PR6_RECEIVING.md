---
name: PR6-receiving-work-confirmation
description: "Agent PR6 — Oracle Fusion Cloud Receiving and Work Confirmation. Confirms physical receipt of goods and services against approved POs. Triggers 3-way match for AP invoicing. 134 receiving endpoints + 38 receipt transaction endpoints = 172 total."
agent_id: PR6
phase: Goods Receipt & Work Confirmation
endpoints: 172
---

# PR6: Receiving / Work Confirmation Agent

## Purpose
Confirm that goods have been physically received or services have been completed, matching delivery against the approved Purchase Order. The receipt record is the critical link that enables 3-way match (PO + Receipt + Invoice) in Accounts Payable.

## Trigger
- Physical goods delivered to warehouse/receiving dock
- Service completion confirmed by project manager
- Partial delivery received (create partial receipt, leave PO open)
- Return to supplier required

## Required Inputs
```json
{
  "po_header_id": 300100300400500,
  "po_schedule_id": 300100300400502,
  "received_quantity": 5,
  "receipt_date": "2026-04-28",
  "received_by_email": "warehouse.manager@company.com",
  "receiving_organization_id": 204,
  "destination_type": "Expense",
  "inspection_required": true,
  "inspection_result": "ACCEPTED"
}
```

---

## API Call Sequence

### Step 1 — Validate PO is Open for Receipt
```
GET /purchaseOrders/{POHeaderUniqId}
  Validate: POHeaderStatusCode in [APPROVED, COMMUNICATED, PARTIALLY_RECEIVED]
  
  FAIL → INCOMPLETE or PENDING_APPROVAL:
    Cannot receive against unapproved PO — halt, notify buyer
  FAIL → FINALLY_CLOSED:
    PO fully closed — halt, investigate if delivery is a duplicate
```

### Step 2 — Check Open Receipt Quantity
```
GET /purchaseOrders/{POHeaderUniqId}/child/lines/{POLineId}/child/schedules/{POScheduleId}
  
  Capture:
    QuantityOrdered     (from PO)
    QuantityReceived    (cumulative receipts so far)
    QuantityOpen        = QuantityOrdered - QuantityReceived
    
  Validate: received_quantity ≤ QuantityOpen (check over-receipt tolerance)
  
  Over-receipt tolerance rule (typically 5%):
    If received_quantity > QuantityOpen × 1.05:
      Hard block — return excess to supplier
    If received_quantity > QuantityOpen but within 5%:
      Allow with buyer notification
```

### Step 3 — Create Receipt Header
```
POST /receivingReceipts
Body:
{
  "ReceiptSourceCode": "VENDOR",         ← VENDOR | INTERNAL
  "ReceiptDate": "YYYY-MM-DD",
  "VendorId": {supplier_id},
  "VendorSiteId": {supplier_site_id},
  "ShipmentNumber": "{supplier_packing_slip}",
  "BillOfLading": "{bill_of_lading}",
  "WaybillAirbillNumber": "{tracking}",
  "ReceivingOrganizationId": {org_id},
  "ReceivedByPersonId": {person_id}
}
Capture: ReceiptHeaderId, ReceiptHeaderUniqId, ReceiptNumber
```

### Step 4 — Create Receipt Lines
```
POST /receivingReceipts/{ReceiptHeaderUniqId}/child/lines
Body:
{
  "ReceiptLineNumber": 1,
  "TransactionType": "RECEIVE",          ← RECEIVE | RETURN_TO_VENDOR | CORRECT
  "ItemId": {item_id},
  "Quantity": {received_qty},
  "UOMCode": "{uom}",
  "POHeaderId": {po_header_id},
  "POLineId": {po_line_id},
  "POScheduleId": {po_schedule_id},
  "DestinationTypeCode": "EXPENSE",      ← EXPENSE | RECEIVING | INVENTORY
  "DeliverToOrganizationId": {org_id},
  "DeliverToLocationId": {location_id},
  "ShipToLocationId": {ship_to_id},
  "LotNumber": "{lot}",                  ← If lot-controlled item
  "SerialNumber": "{serial}"             ← If serial-controlled item
}
Capture: ReceiptLineId
```

### Step 5 — Record Inspection (if required)
```
POST /receivingReceipts/{ReceiptHeaderUniqId}/child/lines/{ReceiptLineId}/child/inspections
Body:
{
  "InspectionDate": "YYYY-MM-DD",
  "QualityInspectionCode": "ACCEPTED",   ← ACCEPTED | REJECTED | CONDITIONALLY_ACCEPTED
  "QuantityAccepted": {accepted_qty},
  "QuantityRejected": {rejected_qty},
  "InspectionNotes": "All units passed visual inspection",
  "InspectorPersonId": {inspector_id}
}
Capture: InspectionResultId

If QuantityRejected > 0:
  → Create return to vendor (Step 7)
  → Notify buyer and AP
  → Hold AP invoice for rejected qty
```

### Step 6 — Deliver to Final Destination
```
POST /receivingReceipts/{ReceiptHeaderUniqId}/child/lines/{ReceiptLineId}/child/transactions
Body:
{
  "TransactionType": "DELIVER",
  "Quantity": {accepted_qty},
  "TransactionDate": "YYYY-MM-DD",
  "DestinationTypeCode": "EXPENSE",      ← Or "INVENTORY" for stock items
  "DeliverToOrganizationId": {org_id},
  "DeliverToLocationId": {location_id}
}
```

### Step 7 — Return to Vendor (if rejection)
```
POST /receivingReceipts/{ReceiptHeaderUniqId}/child/lines
Body:
{
  "TransactionType": "RETURN_TO_VENDOR",
  "Quantity": {rejected_qty},
  "UOMCode": "{uom}",
  "POHeaderId": {po_header_id},
  "POLineId": {po_line_id},
  "POScheduleId": {po_schedule_id},
  "ReturnReason": "QUALITY_DEFECT",      ← QUALITY_DEFECT | WRONG_ITEM | OVER_DELIVERY
  "ReturnDate": "YYYY-MM-DD"
}
```

### Step 8 — Work Confirmation (Services Only)
```
For service POs where delivery = completed work (not physical goods):

POST /workConfirmations
Body:
{
  "POHeaderId": {po_header_id},
  "POLineId": {po_line_id},
  "ConfirmationDate": "YYYY-MM-DD",
  "Description": "Software development sprint 4 completed",
  "RequesterId": {requester_id},
  "lines": [{
    "POScheduleId": {schedule_id},
    "ConfirmedQuantity": 160,            ← Hours completed
    "UOMCode": "HR"
  }]
}
Capture: WorkConfirmationId

POST /workConfirmations/{WorkConfirmationId}/action/submit
GET  /workConfirmations/{WorkConfirmationId}
  Poll until: ConfirmationStatus = "APPROVED"
```

### Step 9 — Verify 3-Way Match Ready
```
After receipt is confirmed, Oracle AP can now perform 3-way match:
  PO Line Price    vs.  Invoice Price    (price tolerance: typically ±5%)
  PO Quantity      vs.  Receipt Quantity vs.  Invoice Quantity

Downstream: AP agent creates payables invoice referencing:
  POHeaderId
  ReceiptHeaderId
  SupplierId
  GL ChargeAccountId
```

---

## Partial Receipt Handling
```
When only part of the ordered quantity arrives:

  Example: PO = 10 units, first delivery = 6 units
  
  Create Receipt:  Quantity = 6
  PO Schedule:     QuantityReceived = 6, QuantityOpen = 4
  
  PO Status:       PARTIALLY_RECEIVED (stays open for remainder)
  AP Invoice:      Matches against 6 units received
  
  When remaining 4 units arrive:
    Create second Receipt:  Quantity = 4
    PO Status:              CLOSED (fully received)
```

---

## Agent Output Payload (Passed to Finance AP / PR7)
```json
{
  "ReceiptHeaderId": 300100400500600,
  "ReceiptNumber": "RCV-2026-0055",
  "ReceiptDate": "2026-04-28",
  "POHeaderId": 300100300400500,
  "SupplierId": 300100099887766,
  "ReceivingOrganizationId": 204,
  "lines": [{
    "ReceiptLineId": 300100400500601,
    "POLineId": 300100300400501,
    "POScheduleId": 300100300400502,
    "TransactionType": "RECEIVE",
    "QuantityReceived": 5,
    "QuantityAccepted": 5,
    "QuantityRejected": 0,
    "InspectionResult": "ACCEPTED",
    "UOMCode": "Each",
    "ItemId": 300100012345678,
    "DeliverToOrganizationId": 204
  }],
  "three_way_match_ready": true,
  "ap_invoice_trigger": {
    "POHeaderId": 300100300400500,
    "ReceiptHeaderId": 300100400500600,
    "SupplierId": 300100099887766,
    "InvoiceAmount": 5250.00,
    "ChargeAccountId": 300100055667788
  }
}
```

---

## Error Handling

| Condition | Action |
|-----------|--------|
| PO not approved | Block receipt, notify buyer to approve PO first |
| Quantity received > PO quantity + 5% | Hard block, return excess |
| Quantity received > PO qty but within 5% | Allow with buyer notification |
| Quality inspection failed | Create return to vendor, hold AP invoice for rejected qty |
| Wrong item delivered | Create return, notify buyer and supplier |
| Serial/lot number missing for controlled item | Validation error, request tracking info |
| PO fully closed | Investigate duplicate delivery |
| Work confirmation rejected | Route back to supplier for revision |

---

## Key Transaction Types

| Type | Use Case |
|------|---------|
| RECEIVE | Standard goods receipt against PO |
| DELIVER | Move received goods to final destination |
| RETURN_TO_VENDOR | Send goods back to supplier |
| CORRECT | Correct a previous receipt (quantity error) |
| INSPECT | Formal quality inspection |

---

*Agent: PR6 | Endpoints: 172 | API version: 26A*
