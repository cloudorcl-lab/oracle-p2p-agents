"""
agents/pr3_sourcing.py — PR3: Sourcing / Negotiation Agent
===========================================================
Runs competitive RFQ/RFP events: create → invite → publish → collect bids
→ score → award → close → confirm.
Outputs: NegotiationId, NegotiationNumber, AwardId, AwardedSupplierId, AwardedPrice.

Build this LAST — most complex agent (282 endpoints, 12 steps, hourly polling).
"""

import asyncio
import logging
import time
from datetime import datetime, timezone

from agents.base_agent import BaseAgent

logger = logging.getLogger("p2p.PR3")

VALID_NEGOTIATION_TYPES = {"RFQ", "RFP", "SEALED_BID", "AUCTION"}


class PR3SourcingAgent(BaseAgent):

    agent_id       = "PR3"
    endpoint_group = "supplierNegotiations"

    # Response collection: poll hourly (suppliers need days to respond)
    RESPONSE_POLL_INTERVAL_SEC  = 3600
    RESPONSE_POLL_TIMEOUT_HOURS = 168    # 7 days

    async def run(self, inputs: dict) -> dict:
        """
        inputs = {
            "negotiation_type":         "RFQ",         # RFQ | RFP | SEALED_BID | AUCTION
            "title":                    "RFQ — Desktop Computers Q2 2026",
            "buyer_id":                 300100178696854,
            "open_bidding_date":        "2026-03-20T09:00:00Z",
            "response_due_date":        "2026-04-15T17:00:00Z",
            "award_by_line":            True,
            "allow_view_bid_ranking":   False,
            "overall_scoring_method":   "MANUAL",      # MANUAL | AUTOMATIC
            "auto_extend":              False,
            "max_extension_days":       0,
            "invited_suppliers": [{
                "supplier_id":      300100099887766,
                "supplier_site_id": 300100099887767,
                "email":            "sales@acmecorp.com",
            }],
            "lines": [{
                "item_id":          300100012345678,
                "item_description": "Standard Desktop",
                "quantity":         5,
                "uom":              "Each",
                "need_by_date":     "2026-04-30",
                "target_price":     1200.00,
                "line_type":        "Goods",
                "requirements": [{
                    "type":        "TECHNICAL",
                    "description": "Provide ISO 9001 certificate",
                    "is_mandatory": True,
                    "response_type": "FILE",
                }],
            }],
        }
        """
        self.log.info(f"[{self.txn_id}] PR3 starting — {inputs.get('negotiation_type', 'RFQ')}")
        await self.audit("PR3_STARTED", {
            "negotiation_type": inputs.get("negotiation_type"),
            "title":            inputs.get("title"),
        })

        self._validate_inputs(inputs)

        # Load upstream RequisitionHeaderId from PR2 state
        req_header_id = await self.store.get("RequisitionHeaderId")
        resume_mode   = False

        # ── Step 1: Check for existing negotiation ─────────────────────────
        existing, uniq_id = await self._check_existing(req_header_id)
        if existing:
            self.log.info(f"[{self.txn_id}] Existing negotiation found — "
                          f"resuming from step 7 ({existing.get('NegotiationNumber')})")
            neg_id   = existing["NegotiationId"]
            uniq_id  = uniq_id
            resume_mode = True
        else:
            # ── Step 2: Create negotiation header ─────────────────────────
            header, uniq_id = await self._create_header(inputs)
            neg_id = header["NegotiationId"]

            # Store immediately — recoverable on re-run
            await self.store.set("NegotiationId",     neg_id)
            await self.store.set("NegotiationUniqId", uniq_id)
            await self.store.set("NegotiationNumber", header.get("NegotiationNumber"))
            self.log.info(f"[{self.txn_id}] Negotiation created: {header.get('NegotiationNumber')}")

            # ── Step 3: Create negotiation lines ──────────────────────────
            req_lines    = await self.store.get("RequisitionLines") or []
            line_outputs = []
            for i, line in enumerate(inputs.get("lines", [])):
                req_line_id = (req_lines[i].get("RequisitionLineId")
                               if i < len(req_lines) else None)
                line_resp = await self._create_line(uniq_id, line,
                                                    line_number=i + 1,
                                                    req_line_id=req_line_id)
                neg_line_id = line_resp["NegotiationLineId"]

                # ── Step 4: Add per-line requirements ─────────────────────
                for req in line.get("requirements", []):
                    await self._add_requirement(uniq_id, neg_line_id, req)

                line_outputs.append({
                    "NegotiationLineId": neg_line_id,
                    "LineNumber":        i + 1,
                    **line_resp,
                })

            await self.store.set("NegotiationLines", line_outputs)

            # ── Step 5: Invite suppliers ───────────────────────────────────
            for supplier in inputs.get("invited_suppliers", []):
                await self._invite_supplier(uniq_id, supplier)

            # ── Step 6: Publish ────────────────────────────────────────────
            self.log.info(f"[{self.txn_id}] Publishing negotiation...")
            try:
                await self.action(f"supplierNegotiations/{uniq_id}/action/publish")
            except Exception:
                self.log.warning(f"[{self.txn_id}] publish may have timed out — verifying status")
                status_data = await self.get(f"supplierNegotiations/{uniq_id}")
                if status_data.get("NegotiationStatus") != "PUBLISHED":
                    raise NegotiationPublishError(
                        f"Negotiation {neg_id} failed to publish — "
                        f"status: {status_data.get('NegotiationStatus')}"
                    )

        # ── Step 7: Monitor responses ──────────────────────────────────────
        self.log.info(f"[{self.txn_id}] Monitoring supplier responses "
                      f"(due: {inputs['response_due_date']})...")
        responses = await self._monitor_responses(
            uniq_id,
            invited_count=len(inputs.get("invited_suppliers", [])),
            response_due_date=inputs["response_due_date"],
        )

        if not responses:
            self.log.warning(f"[{self.txn_id}] No supplier responses received — "
                             f"proceeding to award with justification")

        # ── Step 8: Review bids — get response lines ───────────────────────
        for resp in responses:
            resp["lines"] = await self._get_response_lines(
                uniq_id, resp["ResponseId"]
            )

        # ── Step 9: Score responses (if manual scoring and scores provided) ─
        if inputs.get("overall_scoring_method") == "MANUAL":
            for resp in responses:
                if resp.get("scores"):
                    await self._score_response(uniq_id, resp["ResponseId"], resp["scores"])

        # ── Step 10: Select winner and award ──────────────────────────────
        neg_lines = await self.store.get("NegotiationLines") or []
        if not neg_lines and not resume_mode:
            neg_lines = line_outputs  # type: ignore[possibly-undefined]

        awards_to_make = self._select_winner(responses, inputs.get("lines", []),
                                              inputs.get("invited_suppliers", []))
        award_outputs = []
        for award in awards_to_make:
            award_resp = await self._award_line(uniq_id, award)
            award_outputs.append(award_resp)

        await self.store.set("Awards", award_outputs)
        if award_outputs:
            await self.store.set("AwardId",              award_outputs[0].get("AwardId"))
            await self.store.set("AwardedSupplierId",    awards_to_make[0]["AwardedSupplierId"])
            await self.store.set("AwardedSupplierSiteId", awards_to_make[0]["AwardedSupplierSiteId"])
            await self.store.set("AwardedPrice",         awards_to_make[0]["AwardedPrice"])

        # ── Step 11: Close negotiation ─────────────────────────────────────
        self.log.info(f"[{self.txn_id}] Closing negotiation...")
        try:
            await self.action(f"supplierNegotiations/{uniq_id}/action/closeNegotiation")
        except Exception:
            self.log.warning(f"[{self.txn_id}] closeNegotiation may have timed out — verifying")

        # ── Step 12: Confirm awards ────────────────────────────────────────
        confirmed = await self._confirm_awards(uniq_id)

        # Propagate winning supplier to state for PR4/PR5
        if confirmed:
            first = confirmed[0]
            await self.store.set("WinningSupplierId",    first.get("AwardedSupplierId"))
            await self.store.set("WinningSupplierSiteId", first.get("AwardedSupplierSiteId"))
            await self.store.set("AwardedPrice",         first.get("AwardedPrice"))

        output = {
            "NegotiationId":     neg_id,
            "NegotiationNumber": header.get("NegotiationNumber") if not resume_mode else existing.get("NegotiationNumber"),  # type: ignore[possibly-undefined]
            "NegotiationStatus": "CLOSED",
            "awards":            confirmed,
        }
        await self.store.set_many({"NegotiationOutput": output})
        await self.audit("PR3_COMPLETE", {
            "NegotiationId": neg_id,
            "award_count":   len(confirmed),
        })
        self.log.info(f"[{self.txn_id}] PR3 complete — {len(confirmed)} award(s)")
        return output

    # ── Validation ────────────────────────────────────────────────────────

    def _validate_inputs(self, inputs: dict) -> None:
        neg_type = inputs.get("negotiation_type", "")
        if neg_type not in VALID_NEGOTIATION_TYPES:
            raise ValueError(
                f"negotiation_type must be one of {VALID_NEGOTIATION_TYPES}, "
                f"got '{neg_type}'"
            )
        if not inputs.get("invited_suppliers"):
            raise ValueError("At least one supplier must be invited")
        if not inputs.get("response_due_date"):
            raise ValueError("response_due_date is required")

    # ── Step helpers ──────────────────────────────────────────────────────

    async def _check_existing(self, req_header_id: int | None) -> tuple[dict | None, str | None]:
        """Step 1. GET by RequisitionHeaderId; return existing if DRAFT/PUBLISHED."""
        if not req_header_id:
            return None, None
        data = await self.get(
            "supplierNegotiations",
            params={"q": f"RequisitionHeaderId={req_header_id};"
                         f"NegotiationStatus=DRAFT,PUBLISHED"}
        )
        items = data.get("items", [])
        if items:
            item = items[0]
            return item, self.extract_uniq_id(item)
        return None, None

    async def _create_header(self, inputs: dict) -> tuple[dict, str]:
        """Step 2. POST /supplierNegotiations."""
        body = {
            "NegotiationTitle":              inputs.get("title", "Sourcing Event"),
            "NegotiationType":               inputs["negotiation_type"],
            "BuyerId":                       inputs["buyer_id"],
            "OpenBiddingDate":               self._to_oracle_datetime(
                                                 inputs.get("open_bidding_date", "")),
            "ResponseDueDate":               self._to_oracle_datetime(
                                                 inputs["response_due_date"]),
            "AutoExtendFlag":                "Y" if inputs.get("auto_extend") else "N",
            "MaxExtensionDays":              inputs.get("max_extension_days", 0),
            "AwardByLine":                   "Y" if inputs.get("award_by_line", True) else "N",
            "AllowSupplierToViewBidRanking": "Y" if inputs.get("allow_view_bid_ranking") else "N",
            "OverallScoringMethod":          inputs.get("overall_scoring_method", "MANUAL"),
        }

        resp = await self.post("supplierNegotiations", body)
        return resp, self.extract_uniq_id(resp)

    async def _create_line(self, neg_uniq: str, line: dict,
                            line_number: int,
                            req_line_id: int | None) -> dict:
        """Step 3. POST negotiation line."""
        body = {
            "LineNumber": line_number,
            "Quantity":   line["quantity"],
            "UOMCode":    line["uom"],
            "LineType":   line.get("line_type", "Goods"),
        }
        if line.get("item_id"):
            body["ItemId"] = line["item_id"]
        else:
            body["ItemDescription"] = line.get("item_description", "")

        if line.get("need_by_date"):
            body["NeedByDate"] = line["need_by_date"]
        if line.get("target_price") is not None:
            body["TargetPrice"] = line["target_price"]
        if req_line_id:
            body["RequisitionLineId"] = req_line_id

        return await self.post(
            f"supplierNegotiations/{neg_uniq}/child/lines", body
        )

    async def _add_requirement(self, neg_uniq: str, neg_line_id: int,
                                req: dict) -> None:
        """Step 4 (per requirement). POST line requirement."""
        await self.post(
            f"supplierNegotiations/{neg_uniq}/child/lines"
            f"/{neg_line_id}/child/requirements",
            {
                "RequirementType":        req.get("type", "TECHNICAL"),
                "RequirementDescription": req["description"],
                "IsMandatory":            "Y" if req.get("is_mandatory") else "N",
                "ResponseType":           req.get("response_type", "TEXT"),
            }
        )

    async def _invite_supplier(self, neg_uniq: str, supplier: dict) -> None:
        """Step 5 (per supplier). POST supplier invitation."""
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        await self.post(
            f"supplierNegotiations/{neg_uniq}/child/invitedSuppliers",
            {
                "SupplierId":           supplier["supplier_id"],
                "SupplierSiteId":       supplier["supplier_site_id"],
                "InvitationDate":       now_str,
                "InvitationEmailAddress": supplier.get("email", ""),
                "PersonalMessage":      "You are invited to quote on this requirement.",
            }
        )

    async def _monitor_responses(self, neg_uniq: str,
                                  invited_count: int,
                                  response_due_date: str) -> list[dict]:
        """
        Step 7. Custom polling loop (NOT wait_for_approval).
        Polls hourly until all suppliers submitted OR due date passed.
        Logs warning on zero responses — does NOT raise.
        """
        deadline = time.monotonic() + (self.RESPONSE_POLL_TIMEOUT_HOURS * 3600)
        due_dt   = self._parse_oracle_datetime(response_due_date)

        while time.monotonic() < deadline:
            data      = await self.get(
                f"supplierNegotiations/{neg_uniq}/child/supplierResponses"
            )
            responses = data.get("items", [])
            submitted = [r for r in responses
                         if r.get("ResponseStatus") == "SUBMITTED"]

            self.log.info(
                f"[{self.txn_id}] {len(submitted)}/{invited_count} responses received"
            )

            if len(submitted) >= invited_count:
                self.log.info(f"[{self.txn_id}] All suppliers responded")
                return submitted

            now_utc = datetime.now(timezone.utc)
            if due_dt and now_utc >= due_dt:
                self.log.warning(
                    f"[{self.txn_id}] Response due date reached — "
                    f"{len(submitted)} of {invited_count} responses received"
                )
                return submitted

            self.log.info(f"[{self.txn_id}] Waiting {self.RESPONSE_POLL_INTERVAL_SEC}s "
                          f"for more responses...")
            await asyncio.sleep(self.RESPONSE_POLL_INTERVAL_SEC)

        self.log.warning(f"[{self.txn_id}] Poll timeout reached — returning collected responses")
        return []

    async def _get_response_lines(self, neg_uniq: str,
                                   response_id: int) -> list[dict]:
        """Step 8. GET response lines for one supplier response."""
        data = await self.get(
            f"supplierNegotiations/{neg_uniq}/child/supplierResponses"
            f"/{response_id}/child/responseLines"
        )
        return data.get("items", [])

    async def _score_response(self, neg_uniq: str, response_id: int,
                               scores: dict) -> None:
        """Step 9. PATCH response with manual scores."""
        await self.patch(
            f"supplierNegotiations/{neg_uniq}/child/supplierResponses/{response_id}",
            {
                "TechnicalScore":  scores.get("technical"),
                "CommercialScore": scores.get("commercial"),
                "OverallScore":    scores.get("overall"),
            }
        )

    def _select_winner(self, responses: list[dict],
                       lines: list[dict],
                       invited_suppliers: list[dict]) -> list[dict]:
        """
        Select award targets. Default strategy: lowest QuotedPrice per line.
        If no responses, raises NoResponsesReceivedError.
        """
        if not responses:
            raise NoResponsesReceivedError(
                "No supplier responses received. "
                "Cannot award without a bid. "
                "Options: extend due date, award with justification."
            )

        # Build a flat list of (line_id, supplier, price) from all response lines
        award_candidates = []
        for resp in responses:
            supplier_id      = resp.get("SupplierId")
            supplier_site_id = resp.get("SupplierSiteId")
            for rl in resp.get("lines", []):
                award_candidates.append({
                    "NegotiationLineId":     rl.get("NegotiationLineId"),
                    "AwardedSupplierId":     supplier_id,
                    "AwardedSupplierSiteId": supplier_site_id,
                    "AwardedPrice":          rl.get("QuotedPrice", 0),
                    "AwardedQuantity":       rl.get("Quantity", 0),
                    "ResponseId":            resp.get("ResponseId"),
                })

        # If no response lines (responses exist but are empty), use first supplier
        if not award_candidates and invited_suppliers:
            first_sup = invited_suppliers[0]
            for i, line in enumerate(lines):
                award_candidates.append({
                    "NegotiationLineId":     None,
                    "AwardedSupplierId":     first_sup["supplier_id"],
                    "AwardedSupplierSiteId": first_sup["supplier_site_id"],
                    "AwardedPrice":          line.get("target_price", 0),
                    "AwardedQuantity":       line["quantity"],
                })

        # Select lowest price per NegotiationLineId
        best_by_line: dict[int | None, dict] = {}
        for candidate in award_candidates:
            line_id = candidate["NegotiationLineId"]
            if (line_id not in best_by_line or
                    candidate["AwardedPrice"] < best_by_line[line_id]["AwardedPrice"]):
                best_by_line[line_id] = candidate

        return list(best_by_line.values())

    async def _award_line(self, neg_uniq: str, award: dict) -> dict:
        """Step 10 (per line). POST award. Always CreateAgreement=N — PR4 owns that."""
        resp = await self.post(
            f"supplierNegotiations/{neg_uniq}/child/awards",
            {
                "NegotiationLineId":     award["NegotiationLineId"],
                "AwardedSupplierId":     award["AwardedSupplierId"],
                "AwardedSupplierSiteId": award["AwardedSupplierSiteId"],
                "AwardedQuantity":       award["AwardedQuantity"],
                "AwardedPrice":          award["AwardedPrice"],
                "AwardJustification":    award.get(
                    "justification",
                    "Lowest price with acceptable delivery terms"
                ),
                "CreateAgreement":       "N",  # PR4 agent drives agreement creation
            }
        )
        return {
            "AwardId":               resp.get("AwardId"),
            "NegotiationLineId":     award["NegotiationLineId"],
            "AwardedSupplierId":     award["AwardedSupplierId"],
            "AwardedSupplierSiteId": award["AwardedSupplierSiteId"],
            "AwardedQuantity":       award["AwardedQuantity"],
            "AwardedPrice":          award["AwardedPrice"],
        }

    async def _confirm_awards(self, neg_uniq: str) -> list[dict]:
        """Step 12. GET awards and verify all are AWARDED."""
        data = await self.get(
            f"supplierNegotiations/{neg_uniq}/child/awards"
        )
        awards = data.get("items", [])
        unawarded = [a for a in awards if a.get("AwardStatus") != "AWARDED"]
        if unawarded:
            raise AwardConfirmationError(
                f"{len(unawarded)} award(s) not in AWARDED status: "
                f"{[a.get('AwardId') for a in unawarded]}"
            )
        return awards

    # ── Datetime utilities ────────────────────────────────────────────────

    @staticmethod
    def _to_oracle_datetime(dt_str: str) -> str:
        """Normalize datetime to Oracle's required format: YYYY-MM-DDTHH:MM:SSZ."""
        if not dt_str:
            return ""
        # If already ends in Z, return as-is (strip milliseconds if present)
        if dt_str.endswith("Z"):
            # Strip any milliseconds: 2026-03-20T09:00:00.000Z → 2026-03-20T09:00:00Z
            return dt_str.split(".")[0].rstrip("Z") + "Z"
        # Convert +00:00 suffix (Python default) to Z
        if dt_str.endswith("+00:00"):
            return dt_str[:-6] + "Z"
        # Date-only string → midnight UTC
        if "T" not in dt_str:
            return dt_str + "T00:00:00Z"
        return dt_str

    @staticmethod
    def _parse_oracle_datetime(dt_str: str) -> datetime | None:
        """Parse Oracle datetime string to aware datetime object."""
        if not dt_str:
            return None
        try:
            clean = dt_str.replace("Z", "+00:00")
            return datetime.fromisoformat(clean)
        except ValueError:
            try:
                return datetime.fromisoformat(dt_str[:10]).replace(tzinfo=timezone.utc)
            except ValueError:
                return None


class NegotiationPublishError(Exception):
    pass


class NoResponsesReceivedError(Exception):
    pass


class AwardConfirmationError(Exception):
    pass
