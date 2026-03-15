---
name: PR1-supplier-registration
description: "Agent PR1 — Oracle Fusion Cloud Supplier Registration. Onboards new suppliers, creates purchasing sites, captures qualifications, banking details, and routes for approval. Root dependency for all downstream P2P agents. 89 supplier endpoints + 48 supplier site endpoints."
agent_id: PR1
phase: Supplier Onboarding
endpoints: 137
---

# PR1: Supplier Registration Agent

## Purpose
Onboard new suppliers into Oracle Fusion Cloud. Creates the supplier master record, purchasing sites, contacts, qualifications, and bank accounts. This agent is the **root dependency** — no requisition, PO, or payment can exist without an active approved supplier record.

## Trigger
- New supplier application received
- Supplier reactivation request
- Qualification renewal required
- New site needed for existing supplier

## Required Inputs
```json
{
  "supplier_name": "Acme Corp",
  "tax_id": "12-3456789",
  "duns_number": "123456789",
  "supplier_type": "CORPORATION",
  "contact": {
    "first_name": "Jane", "last_name": "Smith",
    "email": "j.smith@acme.com", "phone": "+1-555-0100"
  },
  "address": {
    "line1": "100 Main St", "city": "Austin",
    "state": "TX", "postal_code": "78701", "country": "US"
  },
  "remit_to_address": { "same_as_primary": true },
  "payment_terms": "NET30",
  "bank": {
    "name": "Chase", "branch": "Austin Downtown",
    "account_number": "XXXXXXXXXX", "routing_number": "021000021",
    "account_type": "CHECKING", "swift": "CHASUS33"
  },
  "qualifications": [
    { "type": "INSURANCE", "cert_number": "INS-2024-001",
      "issue_date": "2024-01-01", "expiry_date": "2025-12-31",
      "issuing_authority": "Travelers Insurance" }
  ],
  "procurement_bu": "Vision Operations"
}
```

---

## API Call Sequence

### Step 1 — Duplicate Checks (GET before POST)
```
GET /suppliers?q=TaxOrganizationId={taxId}
  ↳ If found: return existing SupplierId, skip to Step 6 (site check)

GET /suppliers?q=SupplierName={supplierName}
  ↳ If found: verify it's the same entity (check tax ID)
  ↳ If same entity: return existing SupplierId
  ↳ If different entity with same name: proceed with creation (Oracle allows)
```

### Step 2 — Create Supplier Header
```
POST /suppliers
Body:
{
  "SupplierName": "{name}",
  "TaxOrganizationId": "{taxId}",
  "DUNSNumber": "{duns}",
  "SupplierType": "CORPORATION",       ← CORPORATION | INDIVIDUAL | GOVERNMENT
  "EnabledFlag": "Y",
  "BusinessRelationship": "SPEND_AUTHORIZED"
}

Capture from response:
  SupplierId          ← body field, for reference
  SupplierUniqId      ← from links[0].href last segment, USE IN URL PATHS
```

### Step 3 — Supplier HQ Address
```
POST /suppliers/{SupplierUniqId}/child/supplierAddresses
Body:
{
  "AddressLine1": "{line1}",
  "City": "{city}", "State": "{state}",
  "PostalCode": "{zip}", "Country": "US",
  "AddressType": "HEADQUARTER",
  "PrimaryFlag": "Y"
}
Capture: SupplierAddressId
```

### Step 4 — Primary Contact
```
POST /suppliers/{SupplierUniqId}/child/contacts
Body:
{
  "FirstName": "{first}", "LastName": "{last}",
  "EmailAddress": "{email}", "PhoneNumber": "{phone}",
  "IsPrimaryContact": "Y",
  "JobTitle": "Accounts Payable Manager"
}
Capture: ContactId
```

### Step 5 — Create Purchasing Site
```
POST /suppliers/{SupplierUniqId}/child/supplierSites
Body:
{
  "SiteName": "{name}_PURCHASING",
  "ProcurementBusinessUnit": "{bu_name}",
  "SiteType": "PURCHASING",
  "EnabledFlag": "Y",
  "SiteAddressId": "{SupplierAddressId}"
}
Capture: SupplierSiteId, SupplierSiteUniqId
```

### Step 6 — Remit-To Address on Site
```
POST /suppliers/{SupplierUniqId}/child/supplierSites/{SupplierSiteUniqId}/child/siteAddresses
Body:
{
  "AddressLine1": "{remit_line1}",
  "City": "{city}", "State": "{state}",
  "PostalCode": "{zip}", "Country": "US",
  "AddressType": "REMIT_TO"
}
```

### Step 7 — Site Contact
```
POST /suppliers/{SupplierUniqId}/child/supplierSites/{SupplierSiteUniqId}/child/siteContacts
Body: { "ContactPersonId": "{ContactId}", "ContactType": "PURCHASING" }
```

### Step 8 — Payment Terms on Site
```
POST /suppliers/{SupplierUniqId}/child/supplierSites/{SupplierSiteUniqId}/child/paymentTerms
Body:
{
  "PaymentTermsCode": "NET30",         ← NET30 | NET45 | NET60 | IMMEDIATE
  "DefaultPaymentTermsFlag": "Y"
}
```

### Step 9 — Bank Account
```
POST /suppliers/{SupplierUniqId}/child/bankAccounts
Body:
{
  "BankName": "{bankName}",
  "BankBranchName": "{branch}",
  "BankAccountNumber": "{accountNumber}",
  "BankAccountType": "CHECKING",
  "RoutingNumber": "{routing}",
  "IBAN": "{iban_if_international}",
  "SWIFTCode": "{swift}",
  "CurrencyCode": "USD",
  "PrimaryFlag": "Y"
}
Capture: BankAccountId
```

### Step 10 — Qualifications & Certifications
```
POST /suppliers/{SupplierUniqId}/child/qualifications
Body (per qualification):
{
  "QualificationTypeCode": "INSURANCE",    ← INSURANCE | SAFETY | DIVERSITY | ISO
  "CertificationNumber": "{cert_num}",
  "IssueDate": "YYYY-MM-DD",
  "ExpiryDate": "YYYY-MM-DD",
  "IssuingAuthority": "{authority}",
  "QualificationStatus": "APPROVED"
}
Capture: QualificationId (per cert)
```

### Step 11 — Diversity Classification (if applicable)
```
POST /suppliers/{SupplierUniqId}/child/diversityClassifications
Body:
{
  "DiversityTypeCode": "MBE",            ← MBE | WBE | VOSB | SDVOSB | SBE
  "CertificationNumber": "{cert}",
  "CertifyingAgency": "{agency}",
  "ExpirationDate": "YYYY-MM-DD"
}
```

### Step 12 — Submit for Approval
```
POST /suppliers/{SupplierUniqId}/action/submitForApproval
```

### Step 13 — Poll for Approval
```
GET /suppliers/{SupplierUniqId}
  Poll until: SupplierStatus = "APPROVED"
  Interval: 30 seconds | Timeout: 60 minutes
  
  Terminal statuses: APPROVED | REJECTED | INACTIVE
```

### Step 14 — Activate Supplier
```
POST /suppliers/{SupplierUniqId}/action/activate
  Only callable after SupplierStatus = APPROVED

GET /suppliers/{SupplierUniqId}
  Verify: SupplierStatus = ACTIVE, EnabledFlag = Y
```

---

## Agent Output Payload (Passed to PR2)
```json
{
  "SupplierId": 300100099887766,
  "SupplierName": "Acme Corp",
  "SupplierStatus": "ACTIVE",
  "SupplierSiteId": 300100099887767,
  "SupplierSiteName": "ACME_PURCHASING",
  "ProcurementBU": "Vision Operations",
  "BankAccountId": 300100099887768,
  "QualificationIds": [300100099887769],
  "PaymentTermsCode": "NET30"
}
```

---

## Error Handling

| Condition | Action |
|-----------|--------|
| Duplicate tax ID found | Return existing SupplierId, log warning |
| Invalid bank routing number | Validation error — request correction |
| Mandatory qualification missing | Hold approval, notify procurement |
| Site already exists for this BU | Return existing SupplierSiteId |
| Approval rejected | Capture reason, notify supplier contact |
| Tax ID format invalid | Validation error — request correction |
| Duplicate name, different entity | Proceed — Oracle allows same name |

---

## Key Field Reference

| Field | Valid Values |
|-------|-------------|
| SupplierType | CORPORATION, INDIVIDUAL, GOVERNMENT, FOREIGN_CORPORATION |
| BusinessRelationship | SPEND_AUTHORIZED, PROSPECTIVE |
| SiteType | PURCHASING, PAY_ONLY, RFQ_ONLY |
| AddressType | HEADQUARTER, REMIT_TO, SHIP_FROM |
| PaymentTermsCode | NET30, NET45, NET60, IMMEDIATE, 2/10NET30 |
| BankAccountType | CHECKING, SAVINGS |

---

*Agent: PR1 | Endpoints: ~137 | API version: 26A*
