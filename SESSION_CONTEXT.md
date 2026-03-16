# Session Context — Oracle P2P Agent System

> Read this at the start of any new session to resume without re-explaining context.

## Build Status: COMPLETE — E2E testing in progress (blocked on PR1 supplier creation)

All 7 agents built. 54 unit tests pass. Connectivity confirmed (Oracle HTTP 200).
Live E2E is blocked at PR1 supplier creation due to Oracle demo LOV configuration.

---

## What Was Done This Session

| Step | Artifact | Status |
|------|----------|--------|
| Redis PATH fix | Added `C:\Program Files\Redis` to Windows PATH | ✅ Done |
| utcnow() deprecation | Fixed in `pr7_monitor.py` + `test_pr7.py` → `_utcnow()` helper | ✅ Done |
| Docker references | Searched — none found in project | ✅ Done |
| Sample request keys | Rebuilt `samples/all_agents_sample_requests.json` with correct orchestrator keys (`supplier`, `requisition`, etc.) | ✅ Done |
| Emoji encoding | Replaced emoji in `orchestrator.py` output with ASCII (`[OK]`, `[FAIL]`, etc.) | ✅ Done |
| Connectivity test | `python orchestrator.py --test-conn` → HTTP 200 confirmed | ✅ Done |
| Oracle q param quoting | Fixed all `q=` params to use single quotes (Oracle requires single, not double) | ✅ Done |
| PR1 supplier POST body | Fixed field names: `Supplier` (not `SupplierName`), `BusinessRelationshipCode`, `TaxOrganizationType` | ✅ Done |
| PR1 child endpoints | Fixed: `child/addresses` (not `supplierAddresses`), `child/sites` (not `supplierSites`) | ✅ Done |
| PR1 site fields | Fixed: `SupplierSiteName`, `ProcurementBU`, `PurchasingSiteFlag: true`, `AddressId` | ✅ Done |
| PR1 site assignment | Added missing `_create_site_assignment` step (was causing PR creation to fail) | ✅ Done |
| PR1 tests | Updated test mocks to match new 7-step POST sequence (removed 3 dead child calls, added assignment) | ✅ Done |
| 54 tests | All 54 pass, zero warnings | ✅ Done |

---

## E2E Blocker: PR1 Supplier Creation — LOV_TaxOrganizationType

**Symptom:** Oracle returns 400 "Applying List binding LOV_TaxOrganizationType with given set of values leads to no matching row"

**Root cause:** Oracle demo instance LOV for `TaxOrganizationType` is intermittent.
- `CORPORATION` worked once (confirmed 201 at ~14:00) — `SupplierId: 300000322467930` was created
- Subsequent identical calls started returning 400
- ~30+ partial supplier rows were created during debugging (IDs: 300000322467xxx)
- All values (`CORPORATION`, `INDIVIDUAL`, `PARTNERSHIP`, etc.) now fail

**What we know from querying existing suppliers:**
- Existing active supplier: **Lee Supplies** (`SupplierId: 300000047414503`, `SupplierNumber: 1252`)
- Their `TaxOrganizationTypeCode = "CORPORATION"` — confirming that code is correct
- `BusinessRelationshipCode = "SPEND_AUTHORIZED"` — confirmed correct

**Next session strategy: Bypass PR1, use existing supplier**

Rather than creating a new supplier (blocked), run PR2–PR7 using existing Oracle data:
1. Find a supplier with a purchasing site and BU assignment → use `Lee Supplies` or similar
2. Update sample request `supplier` block with a known SupplierId (pre-seed Redis instead of creating)
3. OR: Remove `supplier` block from sample request and pre-seed Redis manually before running

---

## Remaining Steps

### Step A — Pre-seed Redis with existing supplier, run PR2+
```bash
# Find Lee Supplies' purchasing site
cd agents/src
python -c "
import asyncio
from auth.oracle_auth import load_config, make_client
async def t():
    config = load_config()
    async with make_client(config) as client:
        for _ in range(3):
            try:
                r = await client.get(f'{config.base_url}/suppliers/300000047414503/child/sites?limit=10')
                print(r.status_code, r.text[:600])
                break
            except: await asyncio.sleep(3)
asyncio.run(t())
"

# Then pre-seed Redis with real IDs
redis-cli.exe hset p2p:TXN-E2E-002 SupplierId 300000047414503 SupplierSiteId <found_site_id>

# Run from PR2 only (remove supplier block from sample request or add --from pr2 flag)
python orchestrator.py --request ../../samples/all_agents_sample_requests.json --txn-id TXN-E2E-002
```

### Step B — Fix PR1 LOV permanently (for future supplier creation)
Try `TaxOrganizationTypeCode` instead of `TaxOrganizationType` in the POST body:
```python
body = {
    "Supplier": inputs["supplier_name"],
    "BusinessRelationshipCode": "SPEND_AUTHORIZED",
    "TaxOrganizationTypeCode": inputs.get("supplier_type", "CORPORATION"),
}
```
Note: `TaxOrganizationTypeCode` returned different (progressed further) errors in testing
which suggests it may be the right input key when the LOV is properly loaded.

### Step C — Live E2E verification
```bash
redis-cli hgetall p2p:TXN-E2E-002
```

---

## Key Technical Decisions Made

- **HTTP client:** `httpx` (async) — not `requests`
- **PR1 field names (validated live):** `Supplier`, `BusinessRelationshipCode`, `TaxOrganizationType`
- **PR1 child endpoints (validated vs skill docs):** `child/addresses`, `child/sites`, `child/sites/{id}/child/assignments`
- **Site creation fields:** `SupplierSiteName` (max 30 chars), `ProcurementBU`, `PurchasingSiteFlag: true`, `AddressId`
- **Oracle q param quoting:** Single quotes required (`q=Field='value'`) — double quotes rejected
- **Queryable supplier fields:** Only `SupplierId` and `SupplierNumber` support `q=` filter on `/suppliers`
- **Test runner:** `asyncio.run()` — not `asyncio.get_event_loop()` (Python 3.12+ compat)
- **PR3 awards:** `CreateAgreement="N"` always — PR4 owns agreement creation
- **ID discipline:** all agents store root object ID to Redis before starting line loops
- **Duplicate check:** PR1 uses Redis cache only (Oracle GET not viable — SupplierName not queryable)

---

## Quick Verification Commands

```bash
# From agents/src — confirm all tests still pass
python -m unittest discover -s tests -p "test_*.py" -v

# Confirm Oracle connectivity
python orchestrator.py --test-conn

# Confirm Redis
"C:/Program Files/Redis/redis-cli.exe" ping

# Confirm all agents import
python -c "
from agents.pr1_supplier import PR1SupplierAgent
from agents.pr2_requisition import PR2RequisitionAgent
from agents.pr3_sourcing import PR3SourcingAgent
from agents.pr4_agreement import PR4AgreementAgent
from agents.pr5_purchase_order import PR5PurchaseOrderAgent
from agents.pr6_receiving import PR6ReceivingAgent
from agents.pr7_monitor import PR7LifecycleMonitor
print('All 7 agents OK')
"
```

---

## Environment Variables Required (`.env`)

```env
# Oracle Fusion Cloud (Basic Auth)
ORACLE_HOST=https://fa-eqih-dev20-saasfademo1.ds-fa.oraclepdemos.com/
ORACLE_USERNAME=calvin.roth
ORACLE_PASSWORD=<in .env file>

# Redis
REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_DB=0

# PostgreSQL
DB_HOST=localhost
DB_PORT=5432
DB_NAME=p2p_audit
DB_USER=p2p_user
DB_PASSWORD=<in .env file>
```

---

## Full Transcript (for deep context recovery)
```
C:\Users\livet\.claude\projects\f--GoogleDrive-Oracle-AI-Builds-claude-p2p-master-package\a95161fc-4c4d-4d2c-8afb-378a51e54c00.jsonl
```

*Last updated: 2026-03-15 | 54 tests passing | E2E blocked at PR1 LOV issue — next: bypass with existing supplier*
