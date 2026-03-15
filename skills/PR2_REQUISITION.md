---
name: PR2-requisition-agent
description: "Agent PR2 — Oracle Fusion Cloud Purchase Requisition creation with full supplier master, item master, and budget pre-checks. 127 PR endpoints + all pre-validation checks. Covers catalog items, free-text lines, dual-UOM, BPA references, project charging, split distributions, and all 26A action endpoints."
agent_id: PR2
phase: Demand Capture
endpoints: 127
api_version: 26A
---

# PR2: Requisition Agent

## Purpose
Capture internal demand for goods and services. Validates supplier master, item master, and budget before creating the PR. Routes for approval via Oracle AME. Converts approved PR to PO via requisition processing requests.

## Trigger
- Employee purchase request
- Automated reorder signal from inventory
- Project material requisition
- Service request from department

---

## Phase 0: Pre-Creation Checks (All Must Pass)

### Check 1 — Supplier Active & Authorized
```
GET /suppliers?q=SupplierName={name}  OR  ?q=TaxOrganizationId={taxId}

Validate:
  SupplierStatus       = "ACTIVE"
  EnabledFlag          = "Y"
  BusinessRelationship = "SPEND_AUTHORIZED"

FAIL → INACTIVE: route to supplier reactivation, halt PR
FAIL → BLOCKED:  route to AP holds review, halt PR
FAIL → not found: route to PR1 Supplier Registration Agent
```

### Check 2 — Purchasing Site Valid for This BU
```
GET /suppliers/{SupplierId}/child/supplierSites
    ?q=SiteType=PURCHASING;ProcurementBusinessUnit={bu_name}

Validate: EnabledFlag = "Y"  AND  SiteType = "PURCHASING"

FAIL → no site:     route to procurement manager
FAIL → PAY_ONLY:    cannot source from pay-only site, halt
```

### Check 3 — Sourcing Eligibility
```
GET /supplierEligibilities
    ?q=SupplierId={id};BusinessUnitId={buId}

Validate: SourcingEligibilityCode = "ALLOWED"

FAIL → NOT_ALLOWED: hard block, notify buyer
FAIL → WARNING:     allow with mandatory justification note
```

### Check 4 — Qualifications Current
```
GET /suppliers/{SupplierId}/child/qualifications

Validate (for each where IsMandatory = "Y"):
  ExpiryDate > today
  QualificationStatus = "APPROVED"

FAIL → expired mandatory cert: hold PR, notify supplier manager
FAIL → missing cert:           hold PR, notify procurement
```

### Check 5 — AP Hold Status
```
GET /suppliers/{SupplierId}
Check: HoldFlag field

FAIL → HoldFlag = Y, type FRAUD_HOLD:   hard block, escalate to CFO
FAIL → HoldFlag = Y, type PAYMENT_HOLD: allow PR, flag for AP review
FAIL → HoldFlag = Y, type DEBIT_MEMO:   warning only, allow PR
```

### Check 6 — Item Exists in Oracle PIM
```
GET /items?q=ItemNumber={itemNumber};OrganizationCode={orgCode}

Validate:
  ItemStatus         = "Active"
  PurchasingEnabled  = "Y"
  BuyerName          != null

FAIL → not found:          use free-text description line (non-catalog)
FAIL → Inactive:           suggest alternative, halt catalog line
FAIL → PurchasingEnabled=N: cannot add to PR, request PIM update
```

### Check 7 — UOM Validation
```
GET /items/{ItemId}/child/unitOfMeasures

Validate: UOMCode provided by requester exists in item's valid UOM list
For dual-UOM items: both primary and secondary UOMs must be supplied

FAIL → UOM not valid:           request correct UOM from requester
FAIL → dual-UOM missing:        prompt requester for secondary UOM
FAIL → no conversion rule:      route to PIM administrator
```

### Check 8 — Item Category
```
Derived automatically from item master when ItemNumber is provided.
No separate API call needed.

Validate (from item response):
  CategoryName must map to active procurement category for this BU

FAIL → restricted category: route to category manager for exemption
```

### Check 9 — Approved Supplier List (ASL)
```
GET /approvedSupplierLists
    ?q=ItemId={itemId};SupplierId={supplierId};OrganizationId={orgId}

Validate: ASLStatus = "APPROVED"  AND  PurchasingEnabled = "Y"
Capture:  SupplierItemNumber (use on PO line if available)

FAIL → not on ASL:       check if BPA exists; if not, route to PR3 sourcing
FAIL → EXCLUDED:         hard block, must source from different supplier
FAIL → UNAPPROVED:       route to sourcing for qualification
```

### Check 10 — BPA / Contract Agreement
```
GET /supplierAgreements
    ?q=SupplierId={supplierId};ItemId={itemId};AgreementStatusCode=ACTIVE

If BPA found:
  Capture: AgreementId, AgreementLineId
  Validate: AgreementEndDate > NeedByDate
  Use BPA price on PR line (override requester-entered price)

If no BPA:
  If PR value > sourcing threshold ({config.sourcing_threshold_usd}):
    Flag for mandatory sourcing event (route to PR3 after PR approval)
  If PR value ≤ threshold:
    Allow free-market purchase, flag for spend analysis
```

### Check 11 — GL Account Validation
```
GET /glAccounts?q=SegmentValue={costCenter};LedgerId={ledgerId}

Validate:
  AccountStatus  = "Active"
  PostingAllowed = "Y"
  AccountType    must permit operating expenses

FAIL → account not found:       request correction from requester
FAIL → account closed for period: route to finance for period extension
FAIL → capital account for opex: route to capital projects team
```

### Check 12 — Funds Availability
```
Called AFTER PR header created (Step 3 below):
POST /purchaseRequisitions/{UniqId}/action/checkFunds

Validate: FundsStatus = "PASSED"

FAIL → FAILED:   hard block, escalate to budget manager
FAIL → ADVISORY: allow with mandatory CFO override approval
```

---

## Phase 1: PR Creation Sequence

### Step 1 — Duplicate Check
```
GET /purchaseRequisitions
    ?q=Description={description};PreparerEmail={email};DocumentStatus=INCOMPLETE,APPROVED,OPEN

If identical PR found:
  Return existing RequisitionHeaderId (do not create duplicate)
If similar PR found (same item, different requester):
  Warn buyer, allow creation with note
```

### Step 2 — Create PR Header
```
POST /purchaseRequisitions
Body:
{
  "RequisitioningBU": "{bu_name}",
  "PreparerEmail": "{requester_email}",
  "Description": "{pr_description}",
  "Justification": "{business_justification}"
}

Capture: RequisitionHeaderId, RequisitionHeaderUniqID (from URL path), Requisition (PR number)
Expected: DocumentStatus = "Incomplete"
```

### Step 3A — Create Line: Catalog Item (Master Item)
```
POST /purchaseRequisitions/{RequisitionHeaderUniqID}/child/lines
Body:
{
  "LineNumber": 1,
  "LineTypeCode": "Goods",
  "Item": "{ItemNumber}",
  "UOM": "{UOMCode}",
  "Quantity": {qty},
  "Supplier": "{SupplierName}",
  "SupplierSite": "{SupplierSiteName}",
  "RequestedDeliveryDate": "YYYY-MM-DD",
  "DestinationType": "Expense",
  "RequesterEmail": "{requester_email}",
  "DestinationOrganizationCode": "{org_code}",
  "DeliverToLocationCode": "{location_code}",
  "AgreementId": "{bpa_id}",           ← from Check 10 if found
  "AgreementLineId": "{bpa_line_id}",  ← from Check 10 if found
  "Urgent": false,
  "EPPLineId": "{epp_id}"              ← 26A: external purchase price line ref
}

Auto-defaulted from Item Master (do NOT override):
  Price, CategoryName, CategoryId, ItemDescription, CurrencyCode

Capture: RequisitionLineId
```

### Step 3B — Create Line: Free-Text / Non-Catalog
```
POST /purchaseRequisitions/{RequisitionHeaderUniqID}/child/lines
Body:
{
  "LineNumber": 1,
  "LineTypeCode": "Goods",             ← or "Services"
  "ItemDescription": "{description}",
  "CategoryName": "{category_name}",
  "UOM": "{UOMCode}",
  "Quantity": {qty},
  "Price": {unit_price},
  "CurrencyCode": "USD",
  "Supplier": "{SupplierName}",
  "SupplierSite": "{SupplierSiteName}",
  "RequestedDeliveryDate": "YYYY-MM-DD",
  "DestinationType": "Expense",
  "RequesterEmail": "{requester_email}",
  "DestinationOrganizationCode": "{org_code}",
  "DeliverToLocationCode": "{location_code}"
}
Capture: RequisitionLineId
```

### Step 4 — Create Distributions
```
POST /purchaseRequisitions/{RequisitionHeaderUniqID}/child/lines/{RequisitionLineId}/child/distributions
Body (single cost center):
{
  "DistributionNumber": 1,
  "Quantity": {full_quantity},
  "ChargeAccountId": "{gl_account_id}",
  "ProjectId": null,
  "TaskId": null
}

Body (split across two cost centers — both dists must sum to line quantity):
[
  { "DistributionNumber": 1, "Quantity": 3, "ChargeAccountId": "{acct1}" },
  { "DistributionNumber": 2, "Quantity": 2, "ChargeAccountId": "{acct2}" }
]

Body (project-charged):
{
  "DistributionNumber": 1,
  "Quantity": {qty},
  "ChargeAccountId": "{gl_account_id}",
  "ProjectId": "{project_id}",
  "TaskId": "{task_id}"
}

RULE: Sum of all distribution quantities must equal line quantity exactly.
Capture: DistributionId (per distribution)
```

### Step 5 — Derive Charge Account (if GL account unknown)
```
POST /purchaseRequisitions/{RequisitionHeaderUniqID}/action/deriveChargeAccount
  Oracle derives GL account from expense rules
  Capture derived ChargeAccountId, update distribution
```

### Step 6 — Calculate Tax & Accounting
```
POST /purchaseRequisitions/{RequisitionHeaderUniqID}/action/calculateTaxAndAccounting
  Must return HTTP 200 before submission
```

### Step 7 — Check Funds
```
POST /purchaseRequisitions/{RequisitionHeaderUniqID}/action/checkFunds
  Validate: FundsStatus = "PASSED"
  FAILED   → escalate to budget manager, halt
  ADVISORY → require override approver annotation
```

### Step 8 — Submit for Approval
```
POST /purchaseRequisitions/{RequisitionHeaderUniqID}/action/submitRequisition
  DocumentStatus transitions: INCOMPLETE → PENDING_APPROVAL
```

### Step 9 — Poll Approval
```
GET /purchaseRequisitions/{RequisitionHeaderUniqID}
Poll until DocumentStatus in [APPROVED, REJECTED, CANCELLED, RETURNED]
Interval: 60 seconds | Timeout: 72 hours

Track: ApprovedDate, ApprovedByEmail, FundsStatus

Optional — get current approver queue:
POST /purchaseRequisitions/{RequisitionHeaderUniqID}/action/retrieveCurrentApprovers
```

### Step 10 — Convert Approved PR to PO
```
POST /requisitionProcessingRequests
Body:
{
  "RequisitioningBU": "{bu_name}",
  "Type": "New Order",
  "Supplier": "{SupplierName}",
  "SupplierSite": "{SupplierSiteName}",
  "Buyer": "{buyer_name}",
  "lines": [
    { "RequisitionLineId": {line_id} }
  ]
}
→ Triggers PR5 Purchase Order Agent
```

---

## Post-Creation Lifecycle Actions (26A)

```
WITHDRAW (pull back during approval):
POST /purchaseRequisitions/{id}/action/withdraw

APPROVER CHECKOUT (approver modifies during approval flow):
POST /purchaseRequisitions/action/initiateApproverCheckout
PATCH /purchaseRequisitions/{id}  (approver makes changes)
POST /purchaseRequisitions/{id}/action/submitRequisition  (re-submit)

SPLIT LINE (one line → multiple):
POST /purchaseRequisitions/{id}/action/splitLine
Body: { "RequisitionLineId": {id}, "SplitQuantity": {qty} }

COPY LINE:
POST /purchaseRequisitions/{id}/action/copyLine
Body: { "RequisitionLineId": {id} }

COPY FROM ANOTHER PR:
POST /purchaseRequisitions/{id}/action/copyLinesFromRequisition
Body: { "SourceRequisitionHeaderId": {source_id} }

MARK URGENT (26A):
PATCH /purchaseRequisitions/{id}/child/lines/{line_id}
Body: { "Urgent": true }

SUGGEST CATEGORY (AI-powered):
POST /purchaseRequisitions/action/suggestCategory
Body: { "ItemDescription": "{free_text}" }

CANCEL:
POST /purchaseRequisitions/{id}/action/cancel

REASSIGN BUYER:
POST /purchaseRequisitions/action/reassignBuyer
Body: { "NewBuyerId": {id}, "RequisitionLineIds": [{line_id}] }
```

---

## Status State Machine
```
INCOMPLETE → (submitRequisition) → PENDING_APPROVAL
PENDING_APPROVAL → (all approve)  → APPROVED
PENDING_APPROVAL → (withdraw)     → INCOMPLETE
PENDING_APPROVAL → (rejected)     → REJECTED
APPROVED → (cancel)               → CANCELLED
APPROVED → (requisitionProcessingRequests) → OPEN → CLOSED
```

---

## Agent Output Payload (Passed to PR3 or PR5)
```json
{
  "RequisitionHeaderId": 300100546441808,
  "RequisitionHeaderUniqID": "300100546441808",
  "RequisitionNumber": "10505856",
  "DocumentStatus": "APPROVED",
  "RequisitioningBU": "Vision Operations",
  "ApprovedDate": "2026-03-15",
  "FundsStatus": "PASSED",
  "lines": [{
    "RequisitionLineId": 300100546441809,
    "ItemNumber": "AS54888",
    "ItemDescription": "Standard Desktop Computer",
    "CategoryName": "Computer Hardware",
    "Quantity": 5,
    "UOM": "Each",
    "Price": 1107.86,
    "SupplierId": 300100099887766,
    "SupplierSiteId": 300100099887767,
    "AgreementId": 300100011223344,
    "AgreementLineId": 300100011223345,
    "RequestedDeliveryDate": "2026-04-30",
    "distributions": [{
      "DistributionNumber": 1, "Quantity": 5,
      "ChargeAccountId": 300100055667788
    }]
  }]
}
```

---

## RBAC Roles Required

| Operation | Oracle Role |
|-----------|------------|
| Create PR for self | Self Service Procurement User |
| View all PRs | Procurement Requester + View Requisition All |
| Modify during approval | Approver Checkout privilege |
| Cancel approved PR | Procurement Manager |
| Reassign buyer | Buyer or Procurement Manager |

---

*Agent: PR2 | Endpoints: 127 | API version: 26A*
