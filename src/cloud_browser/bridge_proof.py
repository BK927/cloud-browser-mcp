"""Short-lived, nonce- and audience-bound proof for the fixed Cloud Browser backend."""
import hashlib
import hmac
import re
import time


def sign(secret: str, nonce: str, audience: str, timestamp: int | None = None) -> str:
    timestamp = int(time.time()) if timestamp is None else timestamp
    if len(secret) < 32 or audience not in ("oauth", "control") or not re.fullmatch(r"[A-Za-z0-9_-]{43}", nonce):
        raise ValueError("invalid_bridge_configuration")
    message = f"cloud-browser-passkey-v1\n{audience}\n{nonce}\n{timestamp}".encode()
    signature = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    return f"{timestamp}.{signature}"


def verify(secret: str, nonce: str, audience: str, proof: str) -> bool:
    try:
        match = re.fullmatch(r"([0-9]{10})\.([a-f0-9]{64})", proof)
        if not match or abs(time.time() - int(match[1])) > 60:
            return False
        return hmac.compare_digest(proof, sign(secret, nonce, audience, int(match[1])))
    except (ValueError, TypeError):
        return False
