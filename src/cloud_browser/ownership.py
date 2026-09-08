"""Public request identity and exclusive work leases (not human-control leases).

An OAuth grant identifies a connection, not a ChatGPT conversation. Possession
of a server-issued lease is therefore required in addition to that identity.
Never infer ownership from a session ID or a caller-supplied task label.
"""

import hashlib
import hmac
import secrets
from contextvars import ContextVar

from .models import BrowserError

request_principal: ContextVar[str | None] = ContextVar("browser_principal", default=None)


def principal_for(record: dict) -> str:
    return hashlib.sha256(
        (str(record["client_id"]) + "\0" + str(record["grant"])).encode()
    ).hexdigest()


def new_ownership(principal: str) -> dict:
    return {"principal": principal, "lease_id": "work_" + secrets.token_urlsafe(32)}


def durable_owner(owner: dict | None):
    if not owner:
        return None
    return {
        "principal": owner["principal"],
        "lease_digest": hashlib.sha256(owner["lease_id"].encode()).hexdigest(),
    }


def check_ownership(owner: dict | None, principal: str, lease_id: str | None):
    if not lease_id:
        raise BrowserError(
            "LEASE_REQUIRED",
            "Pass lease_id returned by browser_open; refresh the MCP tool schema for this contract",
        )
    expected = (
        owner.get("lease_digest") or hashlib.sha256(owner.get("lease_id", "").encode()).hexdigest()
        if owner
        else ""
    )
    if (
        not owner
        or not hmac.compare_digest(owner["principal"], principal)
        or not hmac.compare_digest(expected, hashlib.sha256(lease_id.encode()).hexdigest())
    ):
        raise BrowserError("LEASE_INVALID", "Work lease is invalid for this connection")
