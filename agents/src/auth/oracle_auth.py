"""
auth/oracle_auth.py — Oracle Fusion Basic Auth Client
======================================================
Uses HTTP Basic Authentication (username:password base64 encoded).
Works for development and PoC. Migrate to OAuth for production.

Oracle basic auth format:
  Authorization: Basic base64(username:password)

Every agent imports make_client() to get a pre-configured httpx.AsyncClient.
"""

import base64
import os
import logging
from dataclasses import dataclass

import httpx
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger("oracle_auth")


@dataclass
class OracleConfig:
    host:     str   # e.g. https://your-host.fa.us6.oraclecloud.com
    username: str   # Oracle service account username
    password: str   # Oracle service account password
    api_ver:  str = "11.13.18.05"

    @property
    def base_url(self) -> str:
        return f"{self.host.rstrip('/')}/fscmRestApi/resources/{self.api_ver}"

    @property
    def basic_token(self) -> str:
        creds = f"{self.username}:{self.password}"
        return base64.b64encode(creds.encode()).decode()

    @property
    def headers(self) -> dict:
        return {
            "Authorization":       f"Basic {self.basic_token}",
            "Content-Type":        "application/json",
            "REST-Framework-Version": "3",
        }


def load_config() -> OracleConfig:
    """Load Oracle config from environment variables."""
    host     = os.getenv("ORACLE_HOST")
    username = os.getenv("ORACLE_USERNAME")
    password = os.getenv("ORACLE_PASSWORD")

    missing = [k for k, v in {
        "ORACLE_HOST": host,
        "ORACLE_USERNAME": username,
        "ORACLE_PASSWORD": password,
    }.items() if not v]

    if missing:
        raise EnvironmentError(
            f"Missing required env vars: {', '.join(missing)}\n"
            f"Copy .env.example to .env and fill in your Oracle credentials."
        )

    return OracleConfig(host=host, username=username, password=password)


def make_client(config: OracleConfig | None = None,
                timeout_sec: float = 60.0) -> httpx.AsyncClient:
    """
    Return a configured httpx.AsyncClient with Basic Auth headers pre-set.
    Use as an async context manager in every agent:

        config = load_config()
        async with make_client(config) as client:
            response = await client.get(f"{config.base_url}/suppliers")
    """
    if config is None:
        config = load_config()

    timeout = httpx.Timeout(
        connect=15.0,
        read=timeout_sec,
        write=30.0,
        pool=10.0,
    )

    return httpx.AsyncClient(
        headers=config.headers,
        timeout=timeout,
        follow_redirects=True,
    )


async def test_connection(config: OracleConfig) -> bool:
    """
    Connectivity check with retry.

    Oracle Cloud DNS resolution sometimes fails transiently (errno 11001 /
    getaddrinfo failed). The host IS reachable — DNS just needs a moment.
    We retry up to 5 times with a 3-second gap before declaring failure.
    """
    import asyncio
    from oracle_retry import RetryConfig

    retry_cfg = RetryConfig(
        max_attempts_get=5,
        base_backoff_sec=3.0,
        jitter_factor=0.0,   # fixed 3s gaps so user can see predictable behaviour
    )
    url = f"{config.base_url}/suppliers?limit=1"

    async with make_client(config) as client:
        for attempt in range(1, retry_cfg.max_attempts_get + 1):
            try:
                r = await client.get(url)

                if r.status_code == 200:
                    logger.info(f"✅ Oracle connection OK (attempt {attempt})")
                    return True

                if r.status_code == 401:
                    # Wrong credentials — no point retrying
                    logger.error("Oracle 401 — check ORACLE_USERNAME / ORACLE_PASSWORD in .env")
                    return False

                # Any other non-200 (e.g. 503 during startup) — retry
                logger.warning(
                    f"[TEST-CONN] Attempt {attempt}/{retry_cfg.max_attempts_get}: "
                    f"HTTP {r.status_code} — retrying in {retry_cfg.base_backoff_sec}s"
                )

            except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                # DNS failure, TCP refused, or timeout — all transient on Oracle Cloud
                logger.warning(
                    f"[TEST-CONN] Attempt {attempt}/{retry_cfg.max_attempts_get}: "
                    f"Network error ({type(e).__name__}: {e}) — "
                    f"retrying in {retry_cfg.base_backoff_sec}s"
                )

            except httpx.ReadTimeout as e:
                logger.warning(
                    f"[TEST-CONN] Attempt {attempt}/{retry_cfg.max_attempts_get}: "
                    f"Read timeout — retrying in {retry_cfg.base_backoff_sec}s"
                )

            if attempt < retry_cfg.max_attempts_get:
                await asyncio.sleep(retry_cfg.base_backoff_sec)

    logger.error(
        f"❌ Oracle connection FAILED after {retry_cfg.max_attempts_get} attempts. "
        f"Check ORACLE_HOST in .env and that your network can reach Oracle Cloud."
    )
    return False
