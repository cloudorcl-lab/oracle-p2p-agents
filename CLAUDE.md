# Oracle Fusion Cloud — Source-to-Pay (P2P) Agent System
# Claude Code Project Master Guide

## What This Project Builds

A 7-agent automation system covering the complete **Source-to-Pay lifecycle** on Oracle Fusion Cloud. Each agent owns one phase of the process. Agents run sequentially, passing IDs through a shared state store.

---

## The 7 Agents — Quick Reference

| Agent | File | Phase | Root Object | Key Output |
|-------|------|-------|-------------|-----------|
| PR1 | `skills/PR1_SUPPLIER_REGISTRATION.md` | Onboard new suppliers | Supplier | SupplierId, SupplierSiteId |
| PR2 | `skills/PR2_REQUISITION.md` | Capture internal demand | PurchaseRequisition | RequisitionHeaderId, RequisitionLineId |
| PR3 | `skills/PR3_SOURCING_NEGOTIATION.md` | Run RFQ/RFP events | SupplierNegotiation | NegotiationId, AwardId |
| PR4 | `skills/PR4_AGREEMENT.md` | Create contracts & BPAs | SupplierAgreement | AgreementId, AgreementLineId |
| PR5 | `skills/PR5_PURCHASE_ORDER.md` | Commit spend with supplier | PurchaseOrder | POHeaderId, POLineId, POScheduleId |
| PR6 | `skills/PR6_RECEIVING.md` | Confirm goods/services | Receipt | ReceiptHeaderId, ReceiptLineId |
| PR7 | `skills/PR7_LIFECYCLE_MONITOR.md` | Monitor, detect gaps | Read-only | Alerts, metrics, audit trail |

---

## API Base & Version

```
Base URL:  https://{your-oracle-host}/fscmRestApi/resources/11.13.18.05
Version:   Oracle Fusion Cloud 26A
Auth:      OAuth 2.0 Bearer token via Oracle IDCS
```

## Required Headers (Every Single API Call)

```http
REST-Framework-Version: 3
Content-Type: application/json
Authorization: Bearer {access_token}
```

---

## State Handoff Chain

```
PR1 Supplier Registration
  └── outputs: SupplierId, SupplierSiteId
        ↓
PR2 Requisition Agent
  └── outputs: RequisitionHeaderId, RequisitionLineId, DistributionId
        ↓
PR3 Sourcing/Negotiation Agent  (skipped if direct-buy or BPA exists)
  └── outputs: NegotiationId, AwardId, WinningSupplierId, AwardedPrice
        ↓
PR4 Agreement Management Agent  (skipped for one-time purchases)
  └── outputs: AgreementId, AgreementLineId, PriceTierId
        ↓
PR5 Purchase Order Agent
  └── outputs: POHeaderId, POLineId, POScheduleId, PODistributionId
        ↓
PR6 Receiving / Work Confirmation Agent
  └── outputs: ReceiptHeaderId, ReceiptLineId, InspectionResultId
        ↓
PR7 Lifecycle Monitor Agent  (always running, read-only)
  └── outputs: Gap alerts, SLA violations, three-way match status
```

---

## Project File Structure to Build

```
p2p_skill_package/
├── CLAUDE.md                          ← This file (read first)
├── skills/
│   ├── PR1_SUPPLIER_REGISTRATION.md   ← Agent 1 skill
│   ├── PR2_REQUISITION.md             ← Agent 2 skill
│   ├── PR3_SOURCING_NEGOTIATION.md    ← Agent 3 skill
│   ├── PR4_AGREEMENT.md               ← Agent 4 skill
│   ├── PR5_PURCHASE_ORDER.md          ← Agent 5 skill
│   ├── PR6_RECEIVING.md               ← Agent 6 skill
│   └── PR7_LIFECYCLE_MONITOR.md       ← Agent 7 skill
├── config/
│   ├── config.yaml                    ← Oracle host, BU, org settings
│   └── .env.example                   ← Required environment variables
├── samples/
│   ├── supplier_onboarding_request.json
│   ├── pr_creation_request.json
│   ├── po_creation_request.json
│   └── receiving_request.json
└── src/                               ← Claude Code builds this
    ├── auth/
    │   └── oauth.py                   ← Token management
    ├── agents/
    │   ├── pr1_supplier.py
    │   ├── pr2_requisition.py
    │   ├── pr3_sourcing.py
    │   ├── pr4_agreement.py
    │   ├── pr5_purchase_order.py
    │   ├── pr6_receiving.py
    │   └── pr7_monitor.py
    ├── state/
    │   ├── redis_store.py             ← Fast ID caching
    │   └── postgres_audit.py          ← Durable audit trail
    ├── exceptions/
    │   └── handler.py                 ← Cross-agent error routing
    ├── orchestrator.py                ← Airflow DAG / main runner
    └── tests/
        ├── test_pr1.py
        ├── test_pr2.py
        ├── test_pr3.py
        ├── test_pr4.py
        ├── test_pr5.py
        ├── test_pr6.py
        └── test_pr7.py
```

---

## Golden Rules for Claude Code

### Rule 1: Read the Agent's SKILL.md Before Writing Any Code
Each agent has its own skill file with exact API paths, field names, and validation logic. Never invent endpoints or field names.

### Rule 2: ID Discipline — Always Capture the URL-Path ID
After every POST, capture the `UniqID` — the field Oracle uses in URL paths for child calls. It is NOT always the same as the `Id` field in the body.

```python
# CORRECT
response = await client.post(f"{BASE}/purchaseOrders", json=payload)
po_header_id = response.json()["POHeaderId"]      # body ID (for reference)
po_uniq_id   = response.json()["links"][0]["href"].split("/")[-1]  # URL ID (for child calls)

# WRONG — causes 404 on child calls
po_id = response.json()["POHeaderId"]  # may not match URL path
```

### Rule 3: GET Before POST — Always Check for Duplicates
Every agent must do a duplicate check before creating a new document. Oracle will not always prevent duplicates.

```python
# Pattern for every root object
existing = await client.get(f"{BASE}/suppliers?q=TaxOrganizationId={tax_id}")
if existing.json()["count"] > 0:
    return existing.json()["items"][0]["SupplierId"]  # return existing, don't create
```

### Rule 4: Distributions Must Sum to 100%
Applies to: Requisition lines, PO schedules. Validate before submission. Oracle will reject with a 400.

### Rule 5: Poll for Approvals — Don't Assume
Every document with an approval step needs a polling loop. Never assume approval happens instantly.

```python
# Standard approval polling pattern
async def poll_approval(client, endpoint, doc_id, status_field, 
                        terminal_statuses, interval=60, timeout_hours=72):
    deadline = time.time() + (timeout_hours * 3600)
    while time.time() < deadline:
        r = await client.get(f"{BASE}/{endpoint}/{doc_id}")
        status = r.json()[status_field]
        if status in terminal_statuses:
            return r.json()
        await asyncio.sleep(interval)
    raise TimeoutError(f"Approval timeout after {timeout_hours}h")
```

### Rule 6: Store Every ID in Redis Immediately
After capturing any ID from an API response, write it to Redis before the next API call. If the agent crashes, IDs are recoverable.

```python
redis_client.hset(f"p2p:{transaction_id}", mapping={
    "SupplierId": supplier_id,
    "SupplierSiteId": site_id,
    "RequisitionHeaderId": req_id,
    # ... etc
})
```

### Rule 7: Never Hardcode IDs, Host URLs, or Credentials
All configuration comes from `config.yaml` and environment variables. No exceptions.

---

## Technology Stack

| Component | Technology | Purpose |
|-----------|-----------|---------|
| Language | Python 3.11+ | Agent runtime |
| HTTP | `httpx` (async) | Oracle REST API calls |
| Data Models | `pydantic` v2 | Request/response validation |
| Auth | `authlib` or custom | OAuth 2.0 IDCS |
| State (fast) | Redis | ID cache, polling state |
| State (durable) | PostgreSQL | Full audit trail |
| Orchestration | Apache Airflow | Agent sequencing, retries |
| Secrets | HashiCorp Vault or `.env` | Credentials |
| Monitoring | Prometheus + Grafana | SLA dashboards |
| Testing | `pytest` + `respx` (mock) | Unit and integration |
| Containers | Docker + docker-compose | Local dev |

---

## Recommended Build Order (Start Here)

Build agents in this order to minimize risk:

1. `PR7` — Read-only monitor. Zero risk. Gives visibility while building others.
2. `PR1` — Supplier Registration. Root dependency for everything.
3. `PR2` — Requisition. Core demand capture. Test with 1 line + 1 distribution.
4. `PR5` — Purchase Order. Build direct-PO path first (skip agreement reference).
5. `PR6` — Receiving. Test 2-way match first, then add inspection.
6. `PR4` — Agreement Management. Add after PO agent is stable.
7. `PR3` — Sourcing/Negotiation. Most complex. Build last.

---

## Common Errors & Solutions

| HTTP Code | Oracle Error | Fix |
|-----------|-------------|-----|
| 400 | Distribution quantities don't sum to line quantity | Recalculate splits to = 100% |
| 400 | UOM not valid for item | Use ItemNumber to look up valid UOMs |
| 400 | End date before start date | Validate dates before POST |
| 403 | Insufficient privileges | Check Oracle RBAC role assignment |
| 404 | Resource not found | Validate UniqID — use URL path ID, not body ID |
| 409 | Duplicate document | Return existing ID, do not create |
| 422 | Validation failed | Read `o:errorDetails` array in response body |
| 500 | Internal server error | Retry 3× with exponential backoff, then escalate |

---

## When to Stop and Ask the User

Stop and ask when:
- An API returns fields not documented in the skill files
- Oracle returns a different API version than 26A
- A business rule is ambiguous (e.g., sourcing threshold dollar amount)
- RBAC roles needed for a specific operation are unclear
- The Oracle environment is a sandbox vs production (different base URLs)

---

## Integration With Other Domains

After P2P agents are running, connect to:

| Downstream Domain | Connection Point | Data Passed |
|------------------|-----------------|------------|
| Finance — AP | PR6 Receipt confirmed | POHeaderId, ReceiptHeaderId, SupplierId, GL accounts → triggers AP invoice |
| Finance — GL | PR5 PO approved | ChargeAccountId, Amount → Budget reservation |
| Projects | PR5 PO with ProjectId | ProjectId, TaskId, Amount → Project cost tracking |
| SCM / Inventory | PR6 Receipt posted | ItemId, Quantity, OrganizationId → Inventory on-hand |

---

*Package version: 1.0 | API version: Oracle Fusion Cloud 26A | March 2026*
