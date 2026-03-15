---
name: PR3-sourcing-negotiation
description: "Agent PR3 — Oracle Fusion Cloud Sourcing & Negotiation. Runs competitive RFQ/RFP events, invites suppliers, collects bids, scores responses, and awards to winning supplier. 282 endpoints — largest subdomain in P2P. Most complex agent, build last."
agent_id: PR3
phase: Competitive Sourcing
endpoints: 282
---

# PR3: Sourcing / Negotiation Agent

## Purpose
Execute competitive sourcing to achieve the best price and terms from qualified suppliers. Covers full RFQ (Request for Quotation), RFP (Request for Proposal), sealed bid, and reverse auction processes.

## Trigger
- Approved requisition above sourcing threshold (no BPA available)
- Existing agreement expiring — renewal sourcing
- New category requiring qualified supplier identification
- Ad-hoc strategic sourcing event

## Required Inputs
```json
{
  "negotiation_type": "RFQ",
  "requisition_line_ids": [300100546441809],
  "invited_supplier_ids": [300100099887766, 300100099887770],
  "response_due_date": "2026-04-15",
  "award_by_line": true,
  "evaluation_criteria": ["PRICE", "DELIVERY", "QUALITY"],
  "auto_extend": false,
  "max_extension_days": 5,
  "buyer_id": 300100178696854
}
```

---

## API Call Sequence

### Step 1 — Check for Existing Negotiation
```
GET /supplierNegotiations
    ?q=RequisitionHeaderId={reqId};NegotiationStatus=DRAFT,PUBLISHED

If active negotiation found: return existing NegotiationId
```

### Step 2 — Create Negotiation Header
```
POST /supplierNegotiations
Body:
{
  "NegotiationTitle": "RFQ — {item_category} {date}",
  "NegotiationType": "RFQ",              ← RFQ | RFP | SEALED_BID | AUCTION
  "BuyerId": {buyer_id},
  "OpenBiddingDate": "YYYY-MM-DDTHH:MM:SSZ",
  "ResponseDueDate": "YYYY-MM-DDTHH:MM:SSZ",
  "AutoExtendFlag": "N",
  "MaxExtensionDays": 0,
  "AwardByLine": "Y",                    ← Y = award per line | N = award whole event
  "AllowSupplierToViewBidRanking": "N",  ← Sealed bid: N | Open auction: Y
  "OverallScoringMethod": "MANUAL"       ← MANUAL | AUTOMATIC
}
Capture: NegotiationId, NegotiationUniqId
```

### Step 3 — Create Negotiation Lines
```
POST /supplierNegotiations/{NegotiationUniqId}/child/lines
Body:
{
  "LineNumber": 1,
  "ItemId": {item_id},                   ← OR use ItemDescription for free-text
  "ItemDescription": "{desc}",
  "Quantity": {qty},
  "UOMCode": "{uom}",
  "NeedByDate": "YYYY-MM-DD",
  "RequisitionLineId": {req_line_id},    ← Links back to PR2 output
  "TargetPrice": {target_price},         ← Optional: budget ceiling
  "LineType": "Goods"                    ← Goods | Services
}
Capture: NegotiationLineId
```

### Step 4 — Add Line Requirements (Mandatory Criteria)
```
POST /supplierNegotiations/{NegotiationUniqId}/child/lines/{NegotiationLineId}/child/requirements
Body (per requirement):
{
  "RequirementType": "TECHNICAL",        ← TECHNICAL | COMMERCIAL | COMPLIANCE
  "RequirementDescription": "Provide ISO 9001 certificate",
  "IsMandatory": "Y",                    ← Y = disqualify if missing
  "ResponseType": "FILE"                 ← TEXT | FILE | DATE | NUMBER
}
```

### Step 5 — Invite Suppliers
```
POST /supplierNegotiations/{NegotiationUniqId}/child/invitedSuppliers
Body (repeat for each supplier):
{
  "SupplierId": {supplier_id},
  "SupplierSiteId": {site_id},
  "InvitationDate": "YYYY-MM-DDTHH:MM:SSZ",
  "InvitationEmailAddress": "{email}",
  "PersonalMessage": "You are invited to quote on this requirement."
}
```

### Step 6 — Publish Negotiation
```
POST /supplierNegotiations/{NegotiationUniqId}/action/publish
  NegotiationStatus transitions: DRAFT → PUBLISHED
  Invited suppliers receive notification emails automatically
```

### Step 7 — Monitor Response Collection
```
GET /supplierNegotiations/{NegotiationUniqId}/child/supplierResponses
Poll until ResponseDueDate OR all invited suppliers have responded
Interval: 60 minutes (not seconds — suppliers need days to respond)

Track per response:
  ResponseStatus     (SUBMITTED | DRAFT | NOT_RESPONDED)
  SubmissionDate
  SupplierId, SupplierName
```

### Step 8 — Review Supplier Bids
```
GET /supplierNegotiations/{NegotiationUniqId}/child/supplierResponses/{ResponseId}/child/responseLines

Capture per response line:
  NegotiationLineId
  QuotedPrice
  QuotedDeliveryDate
  SupplierItemNumber
  Notes
  RequirementResponses (attachments, certifications)
```

### Step 9 — Score Responses (if scoring enabled)
```
PATCH /supplierNegotiations/{NegotiationUniqId}/child/supplierResponses/{ResponseId}
Body:
{
  "TechnicalScore": 85,
  "CommercialScore": 90,
  "OverallScore": 88
}
```

### Step 10 — Award Decision
```
POST /supplierNegotiations/{NegotiationUniqId}/child/awards
Body (per awarded line):
{
  "NegotiationLineId": {line_id},
  "AwardedSupplierId": {winning_supplier_id},
  "AwardedSupplierSiteId": {winning_site_id},
  "AwardedQuantity": {qty},
  "AwardedPrice": {price},
  "AwardJustification": "Lowest price with acceptable delivery terms",
  "CreateAgreement": "Y"                 ← Y = auto-create BPA from award
}
Capture: AwardId
```

### Step 11 — Close Negotiation
```
POST /supplierNegotiations/{NegotiationUniqId}/action/closeNegotiation
  Oracle sends award and regret notifications to all invited suppliers
  NegotiationStatus → CLOSED
```

### Step 12 — Confirm Award
```
GET /supplierNegotiations/{NegotiationUniqId}/child/awards
Verify: AwardStatus = AWARDED  for all lines
```

---

## Agent Output Payload (Passed to PR4 or PR5)
```json
{
  "NegotiationId": 300100200300400,
  "NegotiationNumber": "NB-2026-0042",
  "NegotiationStatus": "CLOSED",
  "awards": [{
    "AwardId": 300100200300401,
    "NegotiationLineId": 300100200300410,
    "AwardedSupplierId": 300100099887766,
    "AwardedSupplierSiteId": 300100099887767,
    "AwardedQuantity": 5,
    "AwardedPrice": 1050.00,
    "CreateAgreement": true
  }]
}
```

---

## Error Handling

| Condition | Action |
|-----------|--------|
| No invited suppliers | Validation error — must invite at least 1 |
| Response due date in past | Validation error — set future date |
| Award to uninvited supplier | Validation error |
| Award qty > line qty | Validation error |
| No responses received | Allow award with justification; extend due date option |
| Award before due date | Allowed — sends regret notice to non-responding suppliers |
| Negotiation not published | Cannot receive responses — publish first |

---

## Key Negotiation Types

| Type | Use Case | Supplier Can See Competing Bids? |
|------|---------|--------------------------------|
| RFQ | Standard quote request | No |
| RFP | Complex services / proposals | No |
| SEALED_BID | High value, one-shot bid | No |
| AUCTION | Reverse auction (price competition) | Yes |

---

*Agent: PR3 | Endpoints: 282 | API version: 26A*
