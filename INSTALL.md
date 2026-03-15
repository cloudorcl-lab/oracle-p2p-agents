# Oracle Fusion P2P Agent Package — Quick Start

## What's in this package

```
p2p_master_package/
├── CLAUDE.md               ← Master AI behavioral guide (READ FIRST)
├── INSTALL.md              ← This file
├── requirements.txt        ← Python dependencies
├── config.yaml             ← Agent configuration
├── .env.example            ← Environment variables template
│
├── skills/                 ← 7 skill files (one per agent)
│   ├── PR7_LIFECYCLE_MONITOR.md    ← Build FIRST (read-only gap scanner)
│   ├── PR1_SUPPLIER_REGISTRATION.md
│   ├── PR2_REQUISITION.md
│   ├── PR5_PURCHASE_ORDER.md
│   ├── PR6_RECEIVING.md
│   ├── PR4_AGREEMENT.md
│   └── PR3_SOURCING_NEGOTIATION.md ← Build LAST
│
├── agents/src/             ← Python agent implementations
│   ├── orchestrator.py         ← Main runner
│   ├── oracle_retry.py         ← Retry engine (idempotency)
│   ├── agents/
│   │   ├── base_agent.py       ← Shared helpers (GET/POST/poll)
│   │   ├── pr7_monitor.py      ← Gap detection (8 rules)
│   │   ├── pr1_supplier.py     ← 14-step supplier onboarding
│   │   ├── pr2_requisition.py  ← Requisition creation
│   │   ├── pr5_purchase_order.py
│   │   └── pr6_receiving.py
│   ├── auth/oracle_auth.py     ← Basic auth helper
│   └── state/state_store.py    ← Redis ID cache + audit trail
│
├── deploy/                 ← GitHub Actions + MCP deployment
│   ├── .github/workflows/
│   │   ├── claude.yml
│   │   └── claude_weekly_review.yml
│   ├── push_update.py
│   ├── .mcp.json
│   └── ACTIONS_SETUP.md
│
└── samples/
    └── all_agents_sample_requests.json
```

## Setup (5 steps)

1. **Copy `.env.example` to `.env`** and fill in your Oracle credentials
2. **Install dependencies**: `pip install -r requirements.txt`
3. **Test connection**: `python agents/src/orchestrator.py --test-conn`
4. **Run gap monitor** (safe, read-only): `python agents/src/orchestrator.py --monitor`
5. **Run a request**: `python agents/src/orchestrator.py --request samples/all_agents_sample_requests.json`

## Build order (IMPORTANT)

Always build/test agents in this order:
**PR7 → PR1 → PR2 → PR5 → PR6 → PR4 → PR3**

PR7 is read-only so it's safe to run first. PR3 (sourcing/negotiation) is the
most complex, always build it last.

## Key rules (from CLAUDE.md)

- **GET before POST** — always check if a record exists before creating
- **Distributions must sum to 100%** — Oracle returns 400 otherwise
- **Use URL path ID for child calls** — not the body Id field
- **Poll for approvals** — never assume instant approval
- **Auth**: Basic auth (base64 username:password) — OAuth requires IDCS setup
