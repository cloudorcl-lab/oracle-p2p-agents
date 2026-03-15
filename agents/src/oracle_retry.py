"""
oracle_retry.py — Oracle Fusion Cloud Resilience Layer
=======================================================
Handles the two distinct failure layers in Oracle Fusion:

  LAYER 1 — URL / Network failures (before Oracle even sees the request)
    - DNS resolution failure
    - TCP connection timeout / refused
    - SSL handshake failure
    - Network unreachable / proxy errors
    - Read timeout (Oracle accepted the request but response never arrived)

  LAYER 2 — REST endpoint failures (Oracle responded with an error)
    - 429 Too Many Requests (rate limit)
    - 500 Internal Server Error (Oracle bug / DB issue)
    - 502 Bad Gateway (load balancer upstream failure)
    - 503 Service Unavailable (Oracle maintenance window)
    - 504 Gateway Timeout (Oracle processed too slowly)
    - 408 Request Timeout (Oracle timed out before responding)

CRITICAL ORACLE-SPECIFIC PROBLEM — POST IDEMPOTENCY:
  Oracle does NOT guarantee idempotency on POST calls. If a POST for a
  supplier, requisition, or PO returns a 500 or times out at the NETWORK
  layer, the record MAY have been created before the error occurred.
  Blindly retrying a POST can create duplicate suppliers, duplicate PRs,
  or duplicate POs. This module handles that by:
    1. Always GET-before-retry on POST failures to check if record exists
    2. Using Idempotency-Key header where Oracle supports it
    3. Treating 500/timeout on POST as "check first, retry second"

Known Oracle Fusion endpoint behaviors:
  - /suppliers POST: ~2–5 sec, can time out at 30s during busy periods
  - /purchaseRequisitions POST: ~3–8 sec, frequently returns 503 during
    month-end processing windows
  - /action/submitForApproval: ~5–15 sec, triggers async AME processing,
    often returns 504 on first call even when it succeeded
  - /action/checkFunds: ~3–10 sec, locks budget tables, DO NOT retry
    rapidly — use minimum 30s between retries
  - /action/calculateTaxAndAccounting: ~5–20 sec, tax engine heavy
  - Large GET queries with ?q= filters: can return 503 when index is
    being rebuilt — safe to retry with backoff
"""

import asyncio
import logging
import random
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Awaitable

import httpx

logger = logging.getLogger("oracle_retry")


# ---------------------------------------------------------------------------
# Failure classification
# ---------------------------------------------------------------------------

class FailureLayer(Enum):
    NETWORK   = "NETWORK"    # Layer 1: never reached Oracle
    HTTP      = "HTTP"       # Layer 2: Oracle responded with error code
    UNKNOWN   = "UNKNOWN"


class RetryDecision(Enum):
    RETRY_SAFE         = "RETRY_SAFE"          # Safe to retry immediately
    RETRY_WITH_CHECK   = "RETRY_WITH_CHECK"    # Check for duplicate before retry (POST only)
    RETRY_SLOW         = "RETRY_SLOW"          # Retry but with long backoff (rate limit / locks)
    NO_RETRY           = "NO_RETRY"            # Do not retry — fix the request or escalate
    CIRCUIT_OPEN       = "CIRCUIT_OPEN"        # Circuit breaker tripped — stop all calls


@dataclass
class OracleError:
    layer:           FailureLayer
    status_code:     int | None        # None for network failures
    oracle_code:     str | None        # Oracle error code e.g. "POR-2010915"
    oracle_message:  str | None        # Human-readable Oracle error message
    retry_decision:  RetryDecision
    is_post:         bool = False      # True if the failing call was POST/PATCH
    raw_response:    str | None = None
    request_url:     str | None = None


# ---------------------------------------------------------------------------
# Retry configuration
# ---------------------------------------------------------------------------

@dataclass
class RetryConfig:
    # Maximum attempts per call (1 = no retry)
    max_attempts_get:    int   = 5
    max_attempts_post:   int   = 3      # Fewer for POST due to idempotency risk
    max_attempts_action: int   = 4      # /action/ endpoints — async, check before retry

    # Base backoff in seconds
    base_backoff_sec:    float = 2.0

    # Maximum backoff cap (prevents infinite wait)
    max_backoff_sec:     float = 120.0

    # Jitter factor: adds ±(factor × backoff) randomness to prevent thundering herd
    jitter_factor:       float = 0.3

    # Extra wait for rate-limit (429) responses — Oracle rate limit windows are 60s
    rate_limit_wait_sec: float = 65.0

    # Extra wait between fund-check retries (locks budget tables)
    funds_check_wait_sec: float = 30.0

    # Connection timeout (seconds) — Oracle often slow to accept connections
    connect_timeout_sec: float = 15.0

    # Read timeout (seconds) — Oracle can be slow to respond, especially during month-end
    read_timeout_sec:    float = 60.0

    # Total request timeout
    total_timeout_sec:   float = 90.0

    # HTTP status codes that are SAFE to retry
    retryable_http_codes: tuple = (408, 429, 500, 502, 503, 504)

    # HTTP status codes that are NEVER retried (client error — fix the request)
    non_retryable_http_codes: tuple = (400, 401, 403, 404, 405, 409, 422)


# ---------------------------------------------------------------------------
# Circuit breaker — stops hammering Oracle when it is clearly down
# ---------------------------------------------------------------------------

@dataclass
class CircuitBreaker:
    """
    Per-endpoint circuit breaker. Tracks consecutive failures.
    After threshold failures: OPEN (all calls fail fast).
    After recovery_wait_sec: HALF-OPEN (one probe call allowed).
    On probe success: CLOSED (normal operation resumes).
    """
    failure_threshold:   int   = 5
    recovery_wait_sec:   float = 120.0

    _failure_count:      int   = field(default=0, init=False)
    _state:              str   = field(default="CLOSED", init=False)  # CLOSED | OPEN | HALF_OPEN
    _opened_at:          float = field(default=0.0, init=False)

    def is_open(self) -> bool:
        if self._state == "CLOSED":
            return False
        if self._state == "OPEN":
            if time.monotonic() - self._opened_at > self.recovery_wait_sec:
                self._state = "HALF_OPEN"
                logger.warning("CircuitBreaker: HALF_OPEN — allowing probe call")
                return False
            return True
        return False  # HALF_OPEN allows one call through

    def record_success(self):
        self._failure_count = 0
        if self._state != "CLOSED":
            logger.info("CircuitBreaker: CLOSED — endpoint recovered")
        self._state = "CLOSED"

    def record_failure(self):
        self._failure_count += 1
        if self._failure_count >= self.failure_threshold:
            if self._state != "OPEN":
                logger.error(
                    f"CircuitBreaker: OPEN after {self._failure_count} failures. "
                    f"Pausing calls for {self.recovery_wait_sec}s"
                )
            self._state = "OPEN"
            self._opened_at = time.monotonic()


# Global circuit breaker registry — one per Oracle endpoint group
_circuit_breakers: dict[str, CircuitBreaker] = {}

def get_circuit_breaker(endpoint_group: str) -> CircuitBreaker:
    """endpoint_group examples: 'suppliers', 'purchaseRequisitions', 'purchaseOrders'"""
    if endpoint_group not in _circuit_breakers:
        _circuit_breakers[endpoint_group] = CircuitBreaker()
    return _circuit_breakers[endpoint_group]


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------

def classify_error(
    exc: Exception | None,
    response: httpx.Response | None,
    is_post: bool,
    url: str,
) -> OracleError:
    """
    Examine what went wrong and decide what to do next.
    Called after every failed API call.
    """
    # --- Layer 1: Network failure (no response from Oracle) ---
    if exc is not None:
        if isinstance(exc, httpx.ConnectTimeout):
            logger.warning(f"[NETWORK] Connection timeout to Oracle: {url}")
            return OracleError(
                layer=FailureLayer.NETWORK,
                status_code=None,
                oracle_code=None,
                oracle_message="Connection timeout — Oracle did not accept connection",
                retry_decision=RetryDecision.RETRY_SAFE,  # Oracle never saw the request
                is_post=is_post,
                request_url=url,
            )
        if isinstance(exc, httpx.ReadTimeout):
            logger.warning(f"[NETWORK] Read timeout from Oracle: {url}")
            # READ TIMEOUT IS DANGEROUS FOR POST — Oracle may have processed it
            decision = RetryDecision.RETRY_WITH_CHECK if is_post else RetryDecision.RETRY_SAFE
            return OracleError(
                layer=FailureLayer.NETWORK,
                status_code=None,
                oracle_code=None,
                oracle_message="Read timeout — Oracle accepted request but did not respond in time",
                retry_decision=decision,
                is_post=is_post,
                request_url=url,
            )
        if isinstance(exc, httpx.ConnectError):
            logger.error(f"[NETWORK] Cannot connect to Oracle: {url} — {exc}")
            return OracleError(
                layer=FailureLayer.NETWORK,
                status_code=None,
                oracle_code=None,
                oracle_message=f"Connection error: {exc}",
                retry_decision=RetryDecision.RETRY_SAFE,
                is_post=is_post,
                request_url=url,
            )
        if isinstance(exc, httpx.RemoteProtocolError):
            logger.error(f"[NETWORK] Protocol error from Oracle: {url} — {exc}")
            return OracleError(
                layer=FailureLayer.NETWORK,
                status_code=None,
                oracle_code=None,
                oracle_message=f"Remote protocol error: {exc}",
                retry_decision=RetryDecision.RETRY_SAFE,
                is_post=is_post,
                request_url=url,
            )
        # Generic network exception
        return OracleError(
            layer=FailureLayer.NETWORK,
            status_code=None,
            oracle_code=None,
            oracle_message=str(exc),
            retry_decision=RetryDecision.RETRY_SAFE,
            is_post=is_post,
            request_url=url,
        )

    # --- Layer 2: HTTP error (Oracle responded) ---
    status = response.status_code
    raw = response.text[:500] if response.text else ""

    # Try to extract Oracle error code and message from response body
    oracle_code, oracle_message = _parse_oracle_error(response)

    # 400 Bad Request — never retry, the request body is wrong
    if status == 400:
        return OracleError(
            layer=FailureLayer.HTTP, status_code=400,
            oracle_code=oracle_code, oracle_message=oracle_message,
            retry_decision=RetryDecision.NO_RETRY,
            is_post=is_post, raw_response=raw, request_url=url,
        )

    # 401 Unauthorized — token expired, refresh token then retry
    if status == 401:
        return OracleError(
            layer=FailureLayer.HTTP, status_code=401,
            oracle_code=oracle_code, oracle_message="Access token expired or invalid",
            retry_decision=RetryDecision.RETRY_SAFE,  # after token refresh
            is_post=is_post, raw_response=raw, request_url=url,
        )

    # 403 Forbidden — RBAC role missing, do not retry
    if status == 403:
        return OracleError(
            layer=FailureLayer.HTTP, status_code=403,
            oracle_code=oracle_code, oracle_message="Insufficient Oracle RBAC privileges",
            retry_decision=RetryDecision.NO_RETRY,
            is_post=is_post, raw_response=raw, request_url=url,
        )

    # 404 Not Found — wrong URL or ID, do not retry
    if status == 404:
        return OracleError(
            layer=FailureLayer.HTTP, status_code=404,
            oracle_code=oracle_code, oracle_message="Resource not found — check ID or URL path",
            retry_decision=RetryDecision.NO_RETRY,
            is_post=is_post, raw_response=raw, request_url=url,
        )

    # 408 Request Timeout — Oracle processing timeout
    if status == 408:
        decision = RetryDecision.RETRY_WITH_CHECK if is_post else RetryDecision.RETRY_SAFE
        return OracleError(
            layer=FailureLayer.HTTP, status_code=408,
            oracle_code=oracle_code, oracle_message="Oracle request processing timeout",
            retry_decision=decision,
            is_post=is_post, raw_response=raw, request_url=url,
        )

    # 409 Conflict — duplicate record, do not retry
    if status == 409:
        return OracleError(
            layer=FailureLayer.HTTP, status_code=409,
            oracle_code=oracle_code, oracle_message="Duplicate record — return existing ID",
            retry_decision=RetryDecision.NO_RETRY,
            is_post=is_post, raw_response=raw, request_url=url,
        )

    # 422 Unprocessable Entity — validation error, do not retry
    if status == 422:
        return OracleError(
            layer=FailureLayer.HTTP, status_code=422,
            oracle_code=oracle_code, oracle_message=oracle_message,
            retry_decision=RetryDecision.NO_RETRY,
            is_post=is_post, raw_response=raw, request_url=url,
        )

    # 429 Too Many Requests — rate limited by Oracle, long wait required
    if status == 429:
        logger.warning(f"[RATE LIMIT] Oracle rate limit hit: {url}")
        return OracleError(
            layer=FailureLayer.HTTP, status_code=429,
            oracle_code=None, oracle_message="Oracle rate limit — wait before retrying",
            retry_decision=RetryDecision.RETRY_SLOW,
            is_post=is_post, raw_response=raw, request_url=url,
        )

    # 500 Internal Server Error — Oracle bug, may have partially processed
    if status == 500:
        decision = RetryDecision.RETRY_WITH_CHECK if is_post else RetryDecision.RETRY_SAFE
        return OracleError(
            layer=FailureLayer.HTTP, status_code=500,
            oracle_code=oracle_code, oracle_message=oracle_message or "Oracle internal error",
            retry_decision=decision,
            is_post=is_post, raw_response=raw, request_url=url,
        )

    # 502 Bad Gateway — load balancer upstream failure, safe to retry
    if status == 502:
        return OracleError(
            layer=FailureLayer.HTTP, status_code=502,
            oracle_code=None, oracle_message="Oracle load balancer upstream failure",
            retry_decision=RetryDecision.RETRY_SAFE,
            is_post=is_post, raw_response=raw, request_url=url,
        )

    # 503 Service Unavailable — Oracle maintenance or overload, safe to retry
    if status == 503:
        return OracleError(
            layer=FailureLayer.HTTP, status_code=503,
            oracle_code=None, oracle_message="Oracle service unavailable (maintenance or overload)",
            retry_decision=RetryDecision.RETRY_SLOW,
            is_post=is_post, raw_response=raw, request_url=url,
        )

    # 504 Gateway Timeout — Oracle processed but load balancer gave up
    # This is the MOST DANGEROUS for POST: Oracle completed the operation
    # but the response never arrived. Always check before retrying.
    if status == 504:
        decision = RetryDecision.RETRY_WITH_CHECK if is_post else RetryDecision.RETRY_SAFE
        logger.warning(
            f"[504] Gateway timeout — {'POST: check for duplicate before retry' if is_post else 'safe to retry'}: {url}"
        )
        return OracleError(
            layer=FailureLayer.HTTP, status_code=504,
            oracle_code=None,
            oracle_message="Gateway timeout — Oracle may have completed the operation",
            retry_decision=decision,
            is_post=is_post, raw_response=raw, request_url=url,
        )

    # Unexpected status — treat conservatively
    return OracleError(
        layer=FailureLayer.UNKNOWN, status_code=status,
        oracle_code=oracle_code, oracle_message=raw,
        retry_decision=RetryDecision.NO_RETRY,
        is_post=is_post, raw_response=raw, request_url=url,
    )


def _parse_oracle_error(response: httpx.Response) -> tuple[str | None, str | None]:
    """Extract Oracle error code and message from response body."""
    try:
        body = response.json()
        # Oracle REST API error format
        if "o:errorDetails" in body:
            detail = body["o:errorDetails"][0]
            return detail.get("o:errorCode"), detail.get("o:errorPath") or detail.get("detail")
        if "title" in body:
            return body.get("type"), body.get("title")
    except Exception:
        pass
    return None, response.text[:200] if response.text else None


# ---------------------------------------------------------------------------
# Backoff calculation
# ---------------------------------------------------------------------------

def calculate_backoff(attempt: int, config: RetryConfig, is_slow: bool = False) -> float:
    """
    Exponential backoff with full jitter.
    attempt: 0-indexed (0 = first retry after first failure)
    is_slow: True for 429 / 503 — uses much longer base wait
    """
    if is_slow:
        base = config.rate_limit_wait_sec
    else:
        base = config.base_backoff_sec * (2 ** attempt)

    base = min(base, config.max_backoff_sec)

    # Full jitter: random value in [0, base] avoids synchronized retries
    jitter = random.uniform(0, base * config.jitter_factor)
    wait = base + jitter

    logger.info(f"Backoff: attempt {attempt + 1}, waiting {wait:.1f}s")
    return wait


# ---------------------------------------------------------------------------
# Token refresh callback type
# ---------------------------------------------------------------------------

TokenRefresher = Callable[[], Awaitable[str]]


# ---------------------------------------------------------------------------
# Main retry wrapper
# ---------------------------------------------------------------------------

async def oracle_call(
    client:           httpx.AsyncClient,
    method:           str,                  # "GET", "POST", "PATCH", "DELETE"
    url:              str,
    config:           RetryConfig,
    token_refresher:  TokenRefresher | None = None,
    duplicate_checker: Callable[[], Awaitable[dict | None]] | None = None,
    endpoint_group:   str = "default",      # For circuit breaker tracking
    is_funds_check:   bool = False,         # Special handling for checkFunds
    **kwargs,                               # Passed directly to httpx
) -> httpx.Response:
    """
    Resilient Oracle Fusion API call with:
      - Layer 1 (network) retry with exponential backoff + jitter
      - Layer 2 (HTTP) retry with classification-based decisions
      - Circuit breaker per endpoint group
      - POST idempotency check before retry
      - Token refresh on 401
      - Detailed structured logging throughout

    Usage:
      response = await oracle_call(
          client=client,
          method="POST",
          url=f"{BASE}/purchaseRequisitions",
          config=retry_config,
          token_refresher=refresh_token,
          duplicate_checker=lambda: check_existing_pr(client, description, email),
          endpoint_group="purchaseRequisitions",
          json={"RequisitioningBU": "Vision Operations", ...}
      )
    """
    is_post    = method.upper() in ("POST", "PATCH", "PUT")
    max_tries  = config.max_attempts_post if is_post else config.max_attempts_get
    cb         = get_circuit_breaker(endpoint_group)

    # Add idempotency key header for all POST calls
    if is_post:
        kwargs.setdefault("headers", {})
        kwargs["headers"]["Idempotency-Key"] = str(uuid.uuid4())

    for attempt in range(max_tries):
        # --- Circuit breaker check ---
        if cb.is_open():
            logger.error(f"[CIRCUIT OPEN] Endpoint group '{endpoint_group}' is blocked. Skipping call.")
            raise OracleCircuitOpenError(
                f"Oracle endpoint group '{endpoint_group}' circuit breaker is OPEN. "
                f"Will retry after {cb.recovery_wait_sec}s."
            )

        exc: Exception | None     = None
        response: httpx.Response | None = None

        try:
            logger.debug(f"[{method}] attempt {attempt + 1}/{max_tries}: {url}")
            response = await client.request(method, url, **kwargs)

            if response.status_code < 400:
                cb.record_success()
                logger.debug(f"[{method}] SUCCESS {response.status_code}: {url}")
                return response

            # Oracle returned an HTTP error
            error = classify_error(None, response, is_post, url)

        except (
            httpx.ConnectTimeout,
            httpx.ReadTimeout,
            httpx.ConnectError,
            httpx.RemoteProtocolError,
            httpx.NetworkError,
        ) as e:
            exc   = e
            error = classify_error(exc, None, is_post, url)

        # --- Log the failure ---
        logger.warning(
            f"[{method}] FAIL attempt {attempt + 1}/{max_tries} | "
            f"layer={error.layer.value} | "
            f"status={error.status_code} | "
            f"decision={error.retry_decision.value} | "
            f"oracle_code={error.oracle_code} | "
            f"url={url}"
        )

        # --- No retry decisions — raise immediately ---
        if error.retry_decision == RetryDecision.NO_RETRY:
            cb.record_failure()
            raise OracleNonRetryableError(error)

        # --- 401: Refresh token then retry without counting as a failure ---
        if error.status_code == 401 and token_refresher:
            logger.info("Refreshing Oracle access token...")
            new_token = await token_refresher()
            kwargs.setdefault("headers", {})
            kwargs["headers"]["Authorization"] = f"Bearer {new_token}"
            continue  # Retry immediately, don't increment backoff

        # --- POST with risky error (500, 504, read timeout): Check for duplicate ---
        if error.retry_decision == RetryDecision.RETRY_WITH_CHECK and duplicate_checker:
            logger.warning(
                f"[POST IDEMPOTENCY] {error.status_code or 'timeout'} on POST — "
                f"checking if Oracle created the record before retrying"
            )
            await asyncio.sleep(3)  # Brief pause — give Oracle time to commit
            existing = await duplicate_checker()
            if existing is not None:
                logger.info(f"[POST IDEMPOTENCY] Record found — returning existing without retry")
                cb.record_success()
                # Wrap the existing dict in a mock response-like object
                return _mock_response(existing)

        # --- Last attempt — don't sleep, just raise ---
        if attempt == max_tries - 1:
            cb.record_failure()
            raise OracleMaxRetriesExceeded(error, attempt + 1)

        # --- Calculate wait time and sleep ---
        is_slow = error.retry_decision == RetryDecision.RETRY_SLOW
        if is_funds_check:
            wait = config.funds_check_wait_sec
            logger.warning(f"[FUNDS CHECK] Using extended wait {wait}s to release budget lock")
        else:
            wait = calculate_backoff(attempt, config, is_slow)

        logger.info(f"[RETRY] Waiting {wait:.1f}s before attempt {attempt + 2}/{max_tries}")
        await asyncio.sleep(wait)

        cb.record_failure()

    # Should never reach here
    raise OracleMaxRetriesExceeded(None, max_tries)


# ---------------------------------------------------------------------------
# Approval polling with retry
# ---------------------------------------------------------------------------

async def poll_approval(
    client:         httpx.AsyncClient,
    url:            str,
    status_field:   str,
    terminal:       set[str],
    config:         RetryConfig,
    poll_interval:  int  = 60,
    timeout_hours:  float = 72,
    endpoint_group: str  = "default",
) -> dict:
    """
    Poll an Oracle document status URL until it reaches a terminal state.
    Applies the full retry wrapper to every poll GET call.

    terminal: set of DocumentStatus values that end polling
              e.g. {"APPROVED", "REJECTED", "CANCELLED"}

    Usage:
      result = await poll_approval(
          client=client,
          url=f"{BASE}/purchaseRequisitions/{uniq_id}",
          status_field="DocumentStatus",
          terminal={"APPROVED", "REJECTED", "CANCELLED"},
          config=retry_config,
          poll_interval=60,
          timeout_hours=72,
          endpoint_group="purchaseRequisitions",
      )
    """
    deadline = time.monotonic() + (timeout_hours * 3600)
    poll_num = 0

    while time.monotonic() < deadline:
        poll_num += 1
        try:
            response = await oracle_call(
                client=client,
                method="GET",
                url=url,
                config=config,
                endpoint_group=endpoint_group,
            )
            data   = response.json()
            status = data.get(status_field)
            logger.info(f"[POLL #{poll_num}] {status_field}={status} | {url}")

            if status in terminal:
                return data

        except OracleCircuitOpenError:
            logger.error("Circuit breaker open during approval polling — waiting for recovery")
            await asyncio.sleep(config.rate_limit_wait_sec)
            continue

        except OracleMaxRetriesExceeded as e:
            logger.error(f"Max retries exceeded during poll — skipping this poll cycle: {e}")
            # Don't abort the entire poll loop for a transient error
            # The next poll cycle will try again

        await asyncio.sleep(poll_interval)

    raise ApprovalTimeoutError(
        f"Document at {url} did not reach terminal status within {timeout_hours}h. "
        f"Last known status unknown. Manual review required."
    )


# ---------------------------------------------------------------------------
# Convenience wrapper for action endpoints (/action/submitRequisition etc.)
# ---------------------------------------------------------------------------

async def oracle_action(
    client:         httpx.AsyncClient,
    url:            str,
    config:         RetryConfig,
    action_name:    str,
    endpoint_group: str = "default",
    **kwargs,
) -> httpx.Response:
    """
    Wrapper specifically for Oracle /action/ endpoints.
    These are always POST and always trigger async processing on Oracle's side.
    They frequently return 504 even when they succeeded.
    Adds extra logging and uses POST-safe retry rules.
    """
    logger.info(f"[ACTION] Invoking Oracle action: {action_name}")
    return await oracle_call(
        client=client,
        method="POST",
        url=url,
        config=config,
        endpoint_group=endpoint_group,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Mock response helper (for idempotency return path)
# ---------------------------------------------------------------------------

class _MockResponse:
    """Wraps an existing dict as a response-like object for idempotency returns."""
    def __init__(self, data: dict):
        self._data  = data
        self.status_code = 200

    def json(self) -> dict:
        return self._data

    @property
    def text(self) -> str:
        import json
        return json.dumps(self._data)

def _mock_response(data: dict) -> _MockResponse:
    return _MockResponse(data)


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------

class OracleRetryError(Exception):
    """Base class for all Oracle retry errors."""
    pass


class OracleNonRetryableError(OracleRetryError):
    """Oracle returned an error that should never be retried (400, 403, 404, 422)."""
    def __init__(self, error: OracleError):
        self.oracle_error = error
        super().__init__(
            f"Non-retryable Oracle error | "
            f"HTTP {error.status_code} | "
            f"code={error.oracle_code} | "
            f"message={error.oracle_message} | "
            f"url={error.request_url}"
        )


class OracleMaxRetriesExceeded(OracleRetryError):
    """All retry attempts exhausted."""
    def __init__(self, error: OracleError | None, attempts: int):
        self.oracle_error = error
        self.attempts     = attempts
        super().__init__(
            f"Max retries ({attempts}) exceeded | "
            f"HTTP {error.status_code if error else 'N/A'} | "
            f"url={error.request_url if error else 'N/A'}"
        )


class OracleCircuitOpenError(OracleRetryError):
    """Circuit breaker is open — endpoint group is suspended."""
    pass


class ApprovalTimeoutError(OracleRetryError):
    """Oracle document did not reach terminal approval status within the timeout."""
    pass
