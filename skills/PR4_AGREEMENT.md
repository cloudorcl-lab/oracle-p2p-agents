---
name: PR4-agreement-management
description: "Agent PR4 — Oracle Fusion Cloud Supplier Agreement (BPA/CPA) creation and management. Creates blanket purchase agreements with tiered pricing, discounts, and SLA terms. 156 agreement endpoints. Agreements created here are referenced by PR2 (requisitions) and PR5 (purchase orders)."
agent_id: PR4
phase: Contract & Agreement Management
endpoints: 156
---

# PR4: Agreement Management Agent

## Purpose
Create and manage long-term supplier agreements — Blanket Purchase Agreements (BPA) for recurring goods and Contract Purchase Agreements (CPA) for services. Agreements lock in negotiated pricing and terms. Referenced by PR2 and PR5 to enforce contract pricing automatically.

## Trigger
- Sourcing negotiation awarded (PR3 output with CreateAgreement = Y)
- Repeat purchase pattern identified (spend analysis alert)
- Strategic supplier relationship formalized
- Existing agreement expiring — renewal required

## Required Inputs
```json
{
  "agreement_type": "BPA",
  "supplier_id": 300100099887766,
  "supplier_site_id": 300100099887767,
  "start_date": "2026-04-01",
  "end_date": "2027-03-31",
  "agreement_amount": 50000.00,
  "currency": "USD",
  "payment_terms": "NET30",
  "negotiation_id": 300100200300400,
  "lines": [{
    "item_id": 300100012345678,
    "item_number": "AS54888",
    "quantity": 100,
    "uom": "Each",
    "unit_price": 1050.00,
    "price_tiers": [
      {"min_qty": 1, "max_qty": 50, "price": 1100.00},
      {"min_qty": 51, "max_qty": 999, "price": 1050.00}
    ]
  }],
  "procurement_bu": "Vision Operations"
}
```

---

## API Call Sequence

### Step 1 — Check for Existing Agreement
```
GET /supplierAgreements
    ?q=SupplierId={supplierId};AgreementType={type};AgreementStatusCode=ACTIVE

If active agreement found for same supplier/category:
  Check if amendment is needed vs new agreement
  Return existing AgreementId if still valid
```

### Step 2 — Create Agreement Header
```
POST /supplierAgreements
Body:
{
  "AgreementType": "BPA",                ← BPA (blanket) | CPA (contract)
  "SupplierId": {supplier_id},
  "SupplierSiteId": {site_id},
  "ProcurementBU": "{bu_name}",
  "CurrencyCode": "USD",
  "StartDate": "YYYY-MM-DD",
  "EndDate": "YYYY-MM-DD",
  "AgreementAmount": {total_value},
  "PaymentTermsCode": "NET30",
  "FreightTermsCode": "FREE_ON_BOARD",
  "AgreementDescription": "{description}",
  "NegotiationId": {neg_id},             ← Links back to PR3 (optional)
  "AutoCreateOrdersFlag": "N",           ← Y = auto-PO when requisition matches
  "ConsumeAgreementOnApproval": "Y"      ← Reduce agreement balance on PO creation
}
Capture: AgreementId, AgreementUniqId
```

### Step 3 — Create Agreement Lines
```
POST /supplierAgreements/{AgreementUniqId}/child/lines
Body:
{
  "LineNumber": 1,
  "ItemId": {item_id},                   ← OR ItemDescription for free-text
  "UOMCode": "{uom}",
  "UnitPrice": {price},
  "Quantity": {committed_qty},           ← Optional: committed minimum qty
  "NeedByDate": "YYYY-MM-DD",
  "NegotiationLineId": {neg_line_id},    ← Links to PR3 negotiation line
  "AllowPriceOverride": "N",             ← N = enforce price on every PO
  "MatchApprovalLevel": "THREE_WAY"      ← TWO_WAY | THREE_WAY
}
Capture: AgreementLineId
```

### Step 4 — Add Price Tiers (Volume Discounts)
```
POST /supplierAgreements/{AgreementUniqId}/child/lines/{AgreementLineId}/child/priceTiers
Body (per tier):
{
  "TierNumber": 1,
  "MinimumQuantity": 1,
  "MaximumQuantity": 50,
  "TierPrice": 1100.00
}
POST again for tier 2:
{
  "TierNumber": 2,
  "MinimumQuantity": 51,
  "MaximumQuantity": 999,
  "TierPrice": 1050.00
}

RULE: No gaps allowed in quantity ranges.
      Tier 1 max + 1 must equal Tier 2 min.
Capture: PriceTierId (per tier)
```

### Step 5 — Payment Terms & SLA
```
POST /supplierAgreements/{AgreementUniqId}/child/deliverables
Body:
{
  "DeliverableType": "PERFORMANCE",
  "Description": "99% on-time delivery within agreed lead time",
  "DueDate": "{agreement_end_date}",
  "ResponsibleParty": "SUPPLIER"
}
```

### Step 6 — Attach Supporting Documents
```
POST /supplierAgreements/{AgreementUniqId}/child/attachments
Body:
{
  "FileName": "signed_contract.pdf",
  "FileType": "PDF",
  "FileContent": "{base64_encoded_content}",
  "Description": "Signed master service agreement"
}
```

### Step 7 — Submit for Approval
```
POST /supplierAgreements/{AgreementUniqId}/action/submitForApproval
  AgreementStatusCode transitions: DRAFT → PENDING_APPROVAL
```

### Step 8 — Poll Approval
```
GET /supplierAgreements/{AgreementUniqId}
Poll until: ApprovalStatus in [APPROVED, REJECTED]
Interval: 30 seconds | Timeout: 60 minutes
```

### Step 9 — Activate Agreement
```
POST /supplierAgreements/{AgreementUniqId}/action/activate
  Only callable after ApprovalStatus = APPROVED
  AgreementStatusCode → ACTIVE

GET /supplierAgreements/{AgreementUniqId}
Verify: AgreementStatusCode = "ACTIVE"
```

---

## Agreement Amendment Flow
```
When an active agreement needs price or scope changes:

POST /supplierAgreements/{AgreementUniqId}/action/createAmendment
  → Creates a copy in DRAFT status with revision number incremented

PATCH /supplierAgreements/{NewDraftUniqId}/child/lines/{AgreementLineId}
  → Update price, quantity, terms

POST /supplierAgreements/{NewDraftUniqId}/action/submitForApproval
POST /supplierAgreements/{NewDraftUniqId}/action/activate
  → Original agreement superseded; new version becomes ACTIVE
```

---

## Agreement Status State Machine
```
DRAFT → (submitForApproval) → PENDING_APPROVAL
PENDING_APPROVAL → (approved) → APPROVED
APPROVED → (activate) → ACTIVE
ACTIVE → (amendment created) → ACTIVE (original) + DRAFT (amendment)
ACTIVE → (end date reached) → EXPIRED
ACTIVE → (manual close) → CLOSED
```

---

## Agent Output Payload (Passed to PR5)
```json
{
  "AgreementId": 300100011223344,
  "AgreementNumber": "BPA-2026-0012",
  "AgreementType": "BPA",
  "AgreementStatusCode": "ACTIVE",
  "SupplierId": 300100099887766,
  "SupplierSiteId": 300100099887767,
  "StartDate": "2026-04-01",
  "EndDate": "2027-03-31",
  "AgreementAmount": 50000.00,
  "RemainingAmount": 50000.00,
  "lines": [{
    "AgreementLineId": 300100011223345,
    "LineNumber": 1,
    "ItemId": 300100012345678,
    "ItemNumber": "AS54888",
    "UnitPrice": 1050.00,
    "UOMCode": "Each",
    "price_tiers": [
      {"TierNumber": 1, "MinimumQuantity": 1, "MaximumQuantity": 50, "TierPrice": 1100.00},
      {"TierNumber": 2, "MinimumQuantity": 51, "MaximumQuantity": 999, "TierPrice": 1050.00}
    ]
  }]
}
```

---

## Error Handling

| Condition | Action |
|-----------|--------|
| Agreement already active for same supplier/item | Check if amendment needed |
| End date before start date | Validation error |
| Agreement amount < sum of line amounts | Validation error |
| Price tier gaps in quantity range | Validation error — fill the gap |
| Overlapping agreements | Warning — allow but flag for review |
| Approval rejected | Capture reason, route back to buyer |
| Activation before approval | Validation error |

---

*Agent: PR4 | Endpoints: 156 | API version: 26A*
