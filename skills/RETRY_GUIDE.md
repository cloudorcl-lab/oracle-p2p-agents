# Oracle Fusion Retry Guide
## Quick Reference for P2P Agents

---

## The Two Failure Layers

```
Your Code
    │
    ├─► LAYER 1 — Network (never reached Oracle)
    │     ConnectTimeout    → retry immediately, safe
    │     ReadTimeout       → GET: retry safe | POST: check duplicate first
    │     ConnectError      → retry with backoff
    │     RemoteProtocol    → retry with backoff
    │
    └─► LAYER 2 — HTTP (Oracle responded with error)
          400  → NO RETRY — fix the request
          401  → refresh token, retry immediately
          403  → NO RETRY — fix RBAC roles
          404  → NO RETRY — fix URL or ID
          408  → GET: retry | POST: check duplicate first
          409  → NO RETRY — return existing ID (it already exists)
          422  → NO RETRY — validation error, fix the payload
          429  → SLOW RETRY — wait 65s (Oracle rate limit window)
          500  → GET: retry | POST: check duplicate first
          502  → retry with backoff (load balancer issue)
          503  → SLOW RETRY — Oracle maintenance window
          504  → GET: retry | POST: check duplicate first (MOST DANGEROUS)
```

---

## The POST Idempotency Problem

**Oracle does NOT guarantee idempotency on POST calls.**

If a POST for a supplier, requisition, or PO returns 500, 504, or a
read timeout, the record **may already exist** in Oracle. Retrying
without checking first creates duplicates.

**Always provide a `duplicate_checker` function on every POST call:**

```python
async def create_pr(client, payload):
    async def check_existing():
        r = await oracle_call(client, "GET", f"{BASE}/purchaseRequisitions",
            params={"q": f"Description={payload['Description']};"
                        f"PreparerEmail={payload['PreparerEmail']};"
                        f"DocumentStatus=INCOMPLETE,APPROVED,OPEN"}, ...)
        items = r.json().get("items", [])
        return items[0] if items else None

    return await oracle_call(client, "POST", f"{BASE}/purchaseRequisitions",
        duplicate_checker=check_existing, ...)  # ← runs before any POST retry
```

**Duplicate check strategies per object:**

| Object | Duplicate Check Field(s) |
|--------|--------------------------|
| Supplier | `TaxOrganizationId` |
| SupplierSite | `SupplierId + SiteType + ProcurementBU` |
| PurchaseRequisition | `PreparerEmail + Description + DocumentStatus` |
| PurchaseOrder | `RequisitionHeaderId` |
| SupplierNegotiation | `RequisitionHeaderId + NegotiationStatus` |
| SupplierAgreement | `SupplierId + AgreementType + AgreementStatusCode` |
| Receipt | `POHeaderId + ReceiptDate + VendorId` |

---

## Action Endpoints — Special Rules

`/action/submitForApproval`, `/action/calculateTaxAndAccounting`,
`/action/checkFunds`, `/action/publish` are all async on Oracle's side.

They frequently return **504** even when they succeeded because Oracle's
async processing takes longer than the gateway timeout.

**Rule: Never abort a workflow based on an action endpoint failure alone.**
Always follow up with a status poll to see the real outcome.

```python
# WRONG — treats 504 as fatal
await submit_for_approval(pr_id)  # raises 504
raise Exception("Submit failed")   # but Oracle did submit it!

# CORRECT — fallthrough to status check
try:
    await submit_for_approval(pr_id)
except OracleMaxRetriesExceeded:
    logger.warning("Submit action failed — checking status anyway")
    pass  # fall through to poll

result = await poll_approval(pr_id, ...)  # truth is here
```

---

## Circuit Breaker

After 5 consecutive failures on an endpoint group, all calls to that
group are blocked for 120 seconds. This prevents cascading failures
when Oracle is in maintenance.

```
5 consecutive failures → OPEN (120s block)
After 120s             → HALF_OPEN (one probe call)
Probe succeeds         → CLOSED (normal)
Probe fails            → OPEN (another 120s)
```

One circuit breaker per endpoint group: `suppliers`, `purchaseRequisitions`,
`purchaseOrders`, `supplierAgreements`, `receivingReceipts`, etc.

---

## Backoff Formula

```
wait = min(base_backoff × 2^attempt, max_backoff) + jitter

Default:
  attempt 0 → ~2-3s
  attempt 1 → ~4-6s
  attempt 2 → ~8-11s
  attempt 3 → ~16-22s
  attempt 4 → ~32-44s (capped at 120s max)

Rate limit (429) / Maintenance (503):
  Flat 65s wait (Oracle's rate limit window is 60s)

Funds check (checkFunds):
  Flat 30s wait (budget table lock release time)
```

---

## Month-End Config

Oracle slows significantly in the last 3 days of each month.
`oracle_retry_usage.py` has `get_retry_config()` that auto-detects this:

```
Normal:     read_timeout=60s,  max_attempts_get=5
Month-end:  read_timeout=120s, max_attempts_get=7, max_backoff=240s
```

---

## Files

| File | Purpose |
|------|---------|
| `src/oracle_retry.py` | Core retry engine — import this in every agent |
| `src/oracle_retry_usage.py` | Working examples for every call pattern |
| `RETRY_GUIDE.md` | This file |

---

*Version: 1.0 | March 2026*
