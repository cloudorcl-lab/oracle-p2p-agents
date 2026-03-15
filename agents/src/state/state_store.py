"""
state/state_store.py — Agent State Store
=========================================
Redis for fast ID caching between agents.
PostgreSQL audit log for durable transaction history.

Every agent writes its output IDs here immediately after each API call.
If an agent crashes, IDs are recoverable from Redis.
"""

import json
import logging
import os
from datetime import datetime
from typing import Any

import redis.asyncio as aioredis
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger("state_store")

TTL_SECONDS = int(os.getenv("REDIS_TTL", 86400))  # 24 hours default


def get_redis() -> aioredis.Redis:
    url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    return aioredis.from_url(url, decode_responses=True)


class AgentStateStore:
    """
    Key-value store scoped to a transaction_id.
    All agent IDs for one P2P transaction live under:
        p2p:{transaction_id}

    Usage:
        store = AgentStateStore(transaction_id="TXN-2026-0042")
        await store.set("SupplierId", 300100099887766)
        supplier_id = await store.get("SupplierId")
        snapshot = await store.get_all()
    """

    def __init__(self, transaction_id: str):
        self.txn_id = transaction_id
        self.key    = f"p2p:{transaction_id}"
        self._redis = get_redis()

    async def set(self, field: str, value: Any) -> None:
        """Store one ID field. Resets TTL on every write."""
        async with self._redis as r:
            await r.hset(self.key, field, json.dumps(value))
            await r.expire(self.key, TTL_SECONDS)
        logger.debug(f"[STATE] {self.txn_id} | {field} = {value}")

    async def set_many(self, mapping: dict[str, Any]) -> None:
        """Store multiple fields at once."""
        async with self._redis as r:
            serialised = {k: json.dumps(v) for k, v in mapping.items()}
            await r.hset(self.key, mapping=serialised)
            await r.expire(self.key, TTL_SECONDS)
        logger.debug(f"[STATE] {self.txn_id} | bulk set {list(mapping.keys())}")

    async def get(self, field: str, default: Any = None) -> Any:
        """Retrieve one field. Returns default if not found."""
        async with self._redis as r:
            raw = await r.hget(self.key, field)
        if raw is None:
            return default
        return json.loads(raw)

    async def get_all(self) -> dict[str, Any]:
        """Return complete state snapshot for this transaction."""
        async with self._redis as r:
            raw = await r.hgetall(self.key)
        return {k: json.loads(v) for k, v in raw.items()}

    async def exists(self) -> bool:
        async with self._redis as r:
            return await r.exists(self.key) > 0

    async def delete(self) -> None:
        async with self._redis as r:
            await r.delete(self.key)


# ── Simple audit log (append-only list in Redis for now) ──────────────────

async def audit_log(transaction_id: str, agent: str,
                    action: str, detail: dict) -> None:
    """
    Append one audit record.
    In production replace with PostgreSQL INSERT.
    """
    record = {
        "timestamp":      datetime.utcnow().isoformat(),
        "transaction_id": transaction_id,
        "agent":          agent,
        "action":         action,
        "detail":         detail,
    }
    key = f"p2p_audit:{transaction_id}"
    async with get_redis() as r:
        await r.rpush(key, json.dumps(record))
        await r.expire(key, TTL_SECONDS * 7)   # keep audit 7× longer
    logger.info(f"[AUDIT] {agent} | {action}")


async def get_audit_trail(transaction_id: str) -> list[dict]:
    """Retrieve full audit trail for one transaction."""
    key = f"p2p_audit:{transaction_id}"
    async with get_redis() as r:
        raw = await r.lrange(key, 0, -1)
    return [json.loads(r) for r in raw]
