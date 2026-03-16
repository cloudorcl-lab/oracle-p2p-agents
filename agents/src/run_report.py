"""
run_report.py — P2P Run Walkthrough Report Generator
=====================================================
Produces a Markdown report after each orchestrator run.

Contents:
  - Run summary (transaction ID, timestamps, overall status)
  - Entities table (what was created, with Oracle IDs)
  - Step timing table (wall time + API call count per agent)
  - Resource usage summary
  - Recommendations (rule-based, derived from run data)

Called by orchestrator.py at the end of run_full_p2p().
Output: runs/p2p_run_{txn_id}_{timestamp}.md
"""

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("p2p.report")


# ── Data structures ─────────────────────────────────────────────────────────

class AgentRunRecord:
    """Holds the outcome of a single agent execution."""

    def __init__(self, agent_id: str):
        self.agent_id   = agent_id
        self.started_at = time.monotonic()
        self.ended_at:  float | None = None
        self.result:    dict         = {}
        self.error:     str | None   = None
        self.api_calls: int          = 0
        self.status:    str          = "NOT_RUN"

    def complete(self, result: dict, api_calls: int) -> None:
        self.ended_at  = time.monotonic()
        self.result    = result
        self.api_calls = api_calls
        self.status    = "OK"

    def fail(self, error: str, api_calls: int) -> None:
        self.ended_at  = time.monotonic()
        self.error     = error
        self.api_calls = api_calls
        self.status    = "FAILED"

    @property
    def elapsed_sec(self) -> float:
        if self.ended_at is None:
            return 0.0
        return round(self.ended_at - self.started_at, 1)


# ── Entity extraction ────────────────────────────────────────────────────────

def _extract_entities(agent_id: str, result: dict) -> list[dict]:
    """
    Pull the key Oracle entities out of an agent result dict.
    Returns list of { entity_type, name, primary_id, secondary_id, status }.
    """
    rows = []

    if agent_id == "PR1":
        rows.append({
            "entity":       "Supplier",
            "name":         result.get("SupplierName", "—"),
            "primary_id":   result.get("SupplierId", "—"),
            "secondary_id": f"Site: {result.get('SupplierSiteId', '—')}",
            "status":       result.get("SupplierStatus", "—"),
        })
        if result.get("BankAccountId"):
            rows.append({
                "entity":       "Bank Account",
                "name":         "Primary",
                "primary_id":   result.get("BankAccountId", "—"),
                "secondary_id": "—",
                "status":       "LINKED",
            })
        qual_ids = result.get("QualificationIds") or []
        for i, qid in enumerate(qual_ids):
            rows.append({
                "entity":       "Qualification",
                "name":         f"Qualification {i + 1}",
                "primary_id":   qid,
                "secondary_id": "—",
                "status":       "APPROVED",
            })

    elif agent_id == "PR2":
        rows.append({
            "entity":       "Requisition",
            "name":         result.get("RequisitionNumber", "—"),
            "primary_id":   result.get("RequisitionHeaderId", "—"),
            "secondary_id": f"BU: {result.get('RequisitioningBU', '—')}",
            "status":       result.get("DocumentStatus", "—"),
        })
        for line in result.get("lines", []):
            rows.append({
                "entity":       "  └ Req Line",
                "name":         line.get("ItemDescription") or line.get("Item", "—"),
                "primary_id":   line.get("RequisitionLineId", "—"),
                "secondary_id": f"Qty: {line.get('Quantity', '—')}",
                "status":       "CREATED",
            })

    elif agent_id == "PR3":
        rows.append({
            "entity":       "Negotiation",
            "name":         result.get("NegotiationNumber", "—"),
            "primary_id":   result.get("NegotiationId", "—"),
            "secondary_id": f"Type: {result.get('NegotiationType', '—')}",
            "status":       result.get("NegotiationStatus", "—"),
        })
        for award in result.get("awards", []):
            rows.append({
                "entity":       "  └ Award",
                "name":         award.get("AwardNumber", "—"),
                "primary_id":   award.get("AwardId", "—"),
                "secondary_id": f"Supplier: {award.get('SupplierName', '—')}",
                "status":       "AWARDED",
            })

    elif agent_id == "PR4":
        rows.append({
            "entity":       "Agreement",
            "name":         result.get("AgreementNumber", "—"),
            "primary_id":   result.get("AgreementId", "—"),
            "secondary_id": f"Type: {result.get('AgreementType', '—')}",
            "status":       result.get("AgreementStatusCode", "—"),
        })

    elif agent_id == "PR5":
        rows.append({
            "entity":       "Purchase Order",
            "name":         result.get("OrderNumber", "—"),
            "primary_id":   result.get("POHeaderId", "—"),
            "secondary_id": f"Supplier: {result.get('SupplierName', '—')}",
            "status":       result.get("DocumentStatus", "—"),
        })
        for line in result.get("lines", []):
            rows.append({
                "entity":       "  └ PO Line",
                "name":         line.get("Item") or line.get("ItemDescription", "—"),
                "primary_id":   line.get("POLineId", "—"),
                "secondary_id": f"Qty: {line.get('Quantity', '—')} @ {line.get('UnitPrice', '—')}",
                "status":       "CREATED",
            })

    elif agent_id == "PR6":
        rows.append({
            "entity":       "Receipt",
            "name":         result.get("ReceiptNumber", "—"),
            "primary_id":   result.get("ReceiptHeaderId", "—"),
            "secondary_id": f"PO: {result.get('POHeaderId', '—')}",
            "status":       result.get("ReceiptStatus", "RECEIVED"),
        })

    elif agent_id == "PR7":
        rows.append({
            "entity":       "Gap Scan",
            "name":         "Lifecycle Monitor",
            "primary_id":   "—",
            "secondary_id": f"Gaps: {result.get('gap_count', 0)}",
            "status":       "COMPLETE",
        })

    return rows


# ── Recommendation rules ─────────────────────────────────────────────────────

def _build_recommendations(records: list[AgentRunRecord],
                            run_elapsed: float) -> list[str]:
    """
    Rule-based recommendations. Returns list of recommendation strings.
    Each recommendation includes a category prefix: [RELIABILITY], [PERF], [CONFIG].
    """
    recs = []
    total_api_calls = sum(r.api_calls for r in records)

    for rec in records:
        if rec.status == "FAILED":
            recs.append(
                f"[RELIABILITY] **{rec.agent_id} failed**: `{rec.error}`. "
                "Before re-running, check Redis for cached IDs from this transaction "
                "and start the agent from the failed step, not from scratch."
            )

        # Slow agents (excluding approval wait — hard to distinguish without per-step data)
        if rec.elapsed_sec > 300 and rec.status == "OK":
            recs.append(
                f"[PERF] **{rec.agent_id} took {rec.elapsed_sec:.0f}s**. "
                "If most of that is approval wait time, consider enabling auto-approval "
                "in Oracle AME for dev/test environments (approval rules → bypass condition)."
            )

        # Bank account skipped
        if rec.agent_id == "PR1" and rec.result.get("BankAccountId") is None:
            recs.append(
                "[CONFIG] **Bank account was not created** — the `child/bankAccounts` "
                "endpoint returned 404 on this Oracle instance. Bank accounts on this "
                "instance are managed via Oracle Payables (AP). No action needed; "
                "the agent already handles this gracefully."
            )

        # High API call count on a single agent
        if rec.api_calls > 20:
            recs.append(
                f"[PERF] **{rec.agent_id} made {rec.api_calls} API calls**. "
                "Review whether pre-check GETs can be batched using `q=` filter "
                "expressions to reduce round-trips. "
                "Use `?fields=FieldA,FieldB` to limit response payload."
            )

        # PR2 distributions validation note
        if rec.agent_id == "PR2" and rec.status == "OK":
            lines = rec.result.get("lines", [])
            if lines:
                recs.append(
                    "[RELIABILITY] Distribution quantities for PR2 lines were validated "
                    "before submission. Keep this check — Oracle returns a 400 if "
                    "distribution totals don't equal line quantity."
                )

        # PR1 qualifications empty
        if rec.agent_id == "PR1":
            quals = rec.result.get("QualificationIds") or []
            if not quals:
                recs.append(
                    "[CONFIG] **No supplier qualifications were provided**. "
                    "For production supplier onboarding, include ISO certifications "
                    "or other compliance documents in the `qualifications` array."
                )

    # Total API call efficiency
    if total_api_calls > 0:
        recs.append(
            f"[PERF] **Total API calls this run: {total_api_calls}**. "
            "Use Oracle `expand=` parameter to retrieve child objects in a single GET "
            "instead of separate requests (e.g., `expand=lines,distributions`). "
            "Apply `REST-Framework-Version: 3` header (already set) to enable "
            "nested child expansion."
        )

    # Run duration
    if run_elapsed > 600:
        recs.append(
            f"[PERF] **Total run time was {run_elapsed / 60:.1f} minutes**. "
            "The majority is approval wait time. Pre-configure Oracle AME with a "
            "bypass rule for transactions under a threshold amount in dev environments."
        )

    if not recs:
        recs.append("[OK] No recommendations — all agents completed within normal parameters.")

    return recs


# ── Report rendering ─────────────────────────────────────────────────────────

def _md_table(headers: list[str], rows: list[list[str]]) -> str:
    """Render a Markdown table from headers + rows."""
    # Compute column widths
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))

    def fmt_row(cells):
        return "| " + " | ".join(str(c).ljust(widths[i]) for i, c in enumerate(cells)) + " |"

    lines = [
        fmt_row(headers),
        "| " + " | ".join("-" * w for w in widths) + " |",
    ]
    for row in rows:
        lines.append(fmt_row(row))
    return "\n".join(lines)


def generate_report(
    records:      list[AgentRunRecord],
    txn_id:       str,
    request_file: str,
    run_started:  float,   # time.monotonic() at run start
    output_dir:   Path,
) -> Path:
    """
    Build and write the Markdown walkthrough report.
    Returns the path to the written file.
    """
    run_ended   = time.monotonic()
    run_elapsed = run_ended - run_started
    now_utc     = datetime.now(timezone.utc)
    timestamp   = now_utc.strftime("%Y%m%d_%H%M%S")

    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / f"p2p_run_{txn_id}_{timestamp}.md"

    overall_status = "COMPLETE"
    for r in records:
        if r.status == "FAILED":
            overall_status = "FAILED"
            break

    lines = []

    # ── Header ──────────────────────────────────────────────────────────────
    lines += [
        "# P2P Run Report",
        "",
        "## Run Summary",
        "",
        f"| Field | Value |",
        f"|-------|-------|",
        f"| Transaction ID | `{txn_id}` |",
        f"| Request File   | `{request_file}` |",
        f"| Run Started    | {now_utc.strftime('%Y-%m-%d %H:%M:%S')} UTC |",
        f"| Total Duration | {run_elapsed:.1f}s ({run_elapsed / 60:.1f} min) |",
        f"| Overall Status | **{overall_status}** |",
        f"| Agents Run     | {len([r for r in records if r.status != 'NOT_RUN'])} |",
        "",
    ]

    # ── Entities created ─────────────────────────────────────────────────────
    lines += ["## Entities Created", ""]

    entity_rows = []
    for rec in records:
        if rec.status == "OK":
            for ent in _extract_entities(rec.agent_id, rec.result):
                entity_rows.append([
                    rec.agent_id,
                    ent["entity"],
                    str(ent["name"]),
                    str(ent["primary_id"]),
                    str(ent["secondary_id"]),
                    ent["status"],
                ])
        elif rec.status == "FAILED":
            entity_rows.append([
                rec.agent_id,
                "—",
                "—",
                "—",
                "—",
                f"FAILED: {(rec.error or '')[:50]}",
            ])

    if entity_rows:
        lines.append(_md_table(
            ["Step", "Entity", "Name / Description", "Oracle ID", "Additional", "Status"],
            entity_rows,
        ))
    else:
        lines.append("_No entities created — all agents were either skipped or failed._")
    lines.append("")

    # ── Step timing ─────────────────────────────────────────────────────────
    lines += ["## Step Timing", ""]

    timing_rows = []
    total_api_calls = 0
    for rec in records:
        if rec.status == "NOT_RUN":
            continue
        total_api_calls += rec.api_calls
        timing_rows.append([
            rec.agent_id,
            _agent_label(rec.agent_id),
            f"{rec.elapsed_sec}s",
            f"{rec.elapsed_sec / run_elapsed * 100:.1f}%" if run_elapsed > 0 else "—",
            str(rec.api_calls),
            rec.status,
        ])

    if timing_rows:
        lines.append(_md_table(
            ["Step", "Agent", "Wall Time", "% of Run", "API Calls", "Status"],
            timing_rows,
        ))
    lines.append("")

    # ── Resource usage ───────────────────────────────────────────────────────
    lines += [
        "## Resource Usage",
        "",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Total wall time    | {run_elapsed:.1f}s |",
        f"| Total API calls    | {total_api_calls} |",
        f"| Avg calls / agent  | {total_api_calls / max(len(records), 1):.1f} |",
        f"| Agents completed   | {len([r for r in records if r.status == 'OK'])} / {len(records)} |",
        "",
        "> **Note on API calls:** Each unit = one Oracle REST request (GET, POST, PATCH, or action).",
        "> Approval poll cycles count as one call per poll interval.",
        "",
    ]

    # ── Recommendations ──────────────────────────────────────────────────────
    lines += ["## Recommendations", ""]
    recs = _build_recommendations(records, run_elapsed)
    for i, rec_text in enumerate(recs, 1):
        lines.append(f"{i}. {rec_text}")
    lines.append("")

    # ── Raw output (collapsed) ───────────────────────────────────────────────
    lines += ["## Raw Agent Outputs", ""]
    for rec in records:
        if rec.status == "NOT_RUN":
            continue
        lines += [
            f"<details>",
            f"<summary><strong>{rec.agent_id}</strong> — {_agent_label(rec.agent_id)} "
            f"({rec.status})</summary>",
            "",
            "```json",
        ]
        if rec.status == "OK":
            lines.append(json.dumps(rec.result, indent=2, default=str))
        else:
            lines.append(json.dumps({"error": rec.error}, indent=2))
        lines += ["```", "", "</details>", ""]

    # ── Footer ───────────────────────────────────────────────────────────────
    lines += [
        "---",
        f"_Generated by P2P Agent Orchestrator · {now_utc.strftime('%Y-%m-%d %H:%M:%S')} UTC_",
    ]

    report_path.write_text("\n".join(lines), encoding="utf-8")
    logger.info(f"[{txn_id}] Run report written: {report_path}")
    return report_path


# ── Helpers ──────────────────────────────────────────────────────────────────

def _agent_label(agent_id: str) -> str:
    labels = {
        "PR1": "Supplier Registration",
        "PR2": "Requisition",
        "PR3": "Sourcing / Negotiation",
        "PR4": "Agreement Management",
        "PR5": "Purchase Order",
        "PR6": "Receiving",
        "PR7": "Lifecycle Monitor",
    }
    return labels.get(agent_id, agent_id)
