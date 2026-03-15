"""
agents/base_agent.py — Base class for all P2P agents
=====================================================
Provides:
  - Basic auth client (from oracle_auth)
  - State store (from state_store)
  - Retry wrapper (from oracle_retry)
  - Approval polling helper
  - Duplicate-check GET helper
  - Structured logging
"""

import asyncio
import logging
import time
from typing import Any

import httpx

from auth.oracle_auth import OracleConfig, load_config, make_client
from state.state_store import AgentStateStore, audit_log
from oracle_retry import (
    RetryConfig, oracle_call, oracle_action, poll_approval,
    OracleNonRetryableError, OracleMaxRetriesExceeded,
)

logger = logging.getLogger("p2p_agent")


class BaseAgent:
    """
    Inherit from this in every agent.

    class PR1SupplierAgent(BaseAgent):
        agent_id = "PR1"
        endpoint_group = "suppliers"
    """

    agent_id:       str = "BASE"
    endpoint_group: str = "default"

    def __init__(self,
                 transaction_id: str,
                 config:         OracleConfig | None = None,
                 retry_config:   RetryConfig | None  = None):

        self.txn_id       = transaction_id
        self.config       = config or load_config()
        self.retry        = retry_config or RetryConfig()
        self.store        = AgentStateStore(transaction_id)
        self.base_url     = self.config.base_url
        self.log          = logging.getLogger(f"p2p.{self.agent_id}")

    # ── Core HTTP helpers ─────────────────────────────────────────────────

    async def get(self, path: str, params: dict | None = None) -> dict:
        """GET with retry. Returns response JSON."""
        async with make_client(self.config) as client:
            r = await oracle_call(
                client=client, method="GET",
                url=f"{self.base_url}/{path.lstrip('/')}",
                config=self.retry,
                endpoint_group=self.endpoint_group,
                params=params or {},
            )
        return r.json()

    async def post(self, path: str, body: dict,
                   duplicate_checker=None) -> dict:
        """POST with retry + idempotency check."""
        async with make_client(self.config) as client:
            r = await oracle_call(
                client=client, method="POST",
                url=f"{self.base_url}/{path.lstrip('/')}",
                config=self.retry,
                endpoint_group=self.endpoint_group,
                duplicate_checker=duplicate_checker,
                json=body,
            )
        return r.json()

    async def patch(self, path: str, body: dict) -> dict:
        """PATCH with retry."""
        async with make_client(self.config) as client:
            r = await oracle_call(
                client=client, method="PATCH",
                url=f"{self.base_url}/{path.lstrip('/')}",
                config=self.retry,
                endpoint_group=self.endpoint_group,
                json=body,
            )
        return r.json()

    async def action(self, path: str, body: dict | None = None,
                     is_funds_check: bool = False) -> dict:
        """POST to an /action/ endpoint with retry."""
        action_name = path.split("/action/")[-1]
        async with make_client(self.config) as client:
            r = await oracle_call(
                client=client, method="POST",
                url=f"{self.base_url}/{path.lstrip('/')}",
                config=self.retry,
                endpoint_group=self.endpoint_group,
                action_name=action_name,
                is_funds_check=is_funds_check,
                json=body or {},
            )
        return r.json()

    # ── Approval polling ──────────────────────────────────────────────────

    async def wait_for_approval(self, path: str, status_field: str,
                                terminal: set[str],
                                poll_interval: int = 60,
                                timeout_hours: float = 72) -> dict:
        """Poll a document status URL until a terminal state is reached."""
        async with make_client(self.config) as client:
            return await poll_approval(
                client=client,
                url=f"{self.base_url}/{path.lstrip('/')}",
                status_field=status_field,
                terminal=terminal,
                config=self.retry,
                poll_interval=poll_interval,
                timeout_hours=timeout_hours,
                endpoint_group=self.endpoint_group,
            )

    # ── ID extraction ─────────────────────────────────────────────────────

    @staticmethod
    def extract_uniq_id(response_body: dict) -> str:
        """
        Pull the URL-path UniqID from Oracle response links[].
        Falls back to first *Id integer field if links not present.
        """
        for link in response_body.get("links", []):
            if link.get("rel") == "self":
                return link["href"].rstrip("/").split("/")[-1]
        # Fallback: first field ending in 'Id' that is an integer
        for key, val in response_body.items():
            if key.endswith("Id") and isinstance(val, int):
                return str(val)
        raise ValueError(f"Cannot extract UniqID from: {list(response_body.keys())}")

    # ── Audit helper ──────────────────────────────────────────────────────

    async def audit(self, action: str, detail: dict) -> None:
        await audit_log(self.txn_id, self.agent_id, action, detail)

    # ── Subclasses implement this ─────────────────────────────────────────

    async def run(self, inputs: dict) -> dict:
        raise NotImplementedError(f"{self.agent_id}.run() not implemented")
