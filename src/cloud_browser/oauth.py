"""Single-user OAuth authorization server with pre-registered, exact callbacks."""

import asyncio
import base64
import hashlib
import hmac
import html
import re
import secrets
import time
from urllib.parse import urlencode

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .config import Settings
from .store import Store


class Auth:
    def __init__(self, settings: Settings, store: Store):
        self.cfg, self.store = settings, store
        self.hasher = PasswordHasher()
        # Login attempts survive a process restart through the SQLite store.

    def limited(self, peer: str) -> bool:
        with self.store.transaction():
            now = time.time()
            blocked = False
            for key, maximum in (("global", 40), (peer, 8)):
                record = self.store.get("throttle", key) or {"start": now, "count": 0}
                if now - record["start"] >= 300:
                    record = {"start": now, "count": 0}
                record["count"] += 1
                self.store.put("throttle", key, record, 300)
                blocked |= record["count"] > maximum
            return blocked

    async def password_ok(self, password: str) -> bool:
        if not self.cfg.admin_password_hash or len(password) > 1024:
            return False
        try:
            return await asyncio.to_thread(
                self.hasher.verify, self.cfg.admin_password_hash, password
            )
        except (VerificationError, InvalidHashError):
            return False

    def bearer(self, value: str):
        if not value.startswith("Bearer "):
            return None
        token = self.store.get("access", value[7:])
        if not token or token["resource"] != self.cfg.resource:
            return None
        if not self.store.get("grant", token["grant"]):
            return None
        return token

    def issue(self, grant: str):
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        record = {
            "grant": grant,
            "resource": self.cfg.resource,
            "client_id": self.cfg.oauth_client_id,
        }
        self.store.put("access", access, record, self.cfg.access_ttl)
        self.store.put("refresh", refresh, record, self.cfg.refresh_ttl)
        return {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": self.cfg.access_ttl,
            "refresh_token": refresh,
            "scope": "browser",
        }

    def install(self, app: FastAPI):
        cfg = self.cfg

        @app.get("/.well-known/oauth-protected-resource")
        @app.get("/.well-known/oauth-protected-resource/mcp")
        async def resource_metadata():
            return {
                "resource": cfg.resource,
                "authorization_servers": [cfg.public_origin],
                "scopes_supported": ["browser"],
                "bearer_methods_supported": ["header"],
            }

        @app.get("/.well-known/oauth-authorization-server")
        async def server_metadata():
            return {
                "issuer": cfg.public_origin,
                "authorization_endpoint": cfg.public_origin + "/authorize",
                "token_endpoint": cfg.public_origin + "/token",
                "revocation_endpoint": cfg.public_origin + "/revoke",
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": ["none"],
                "scopes_supported": ["browser"],
                "authorization_response_iss_parameter_supported": True,
            }

        @app.get("/authorize")
        async def authorize(request: Request):
            if self.limited(request.client.host if request.client else "unknown"):
                return JSONResponse({"error": "temporarily_unavailable"}, status_code=429)
            q = dict(request.query_params)
            valid = (
                q.get("client_id") == cfg.oauth_client_id
                and q.get("redirect_uri") in cfg.oauth_redirect_uris
                and q.get("response_type") == "code"
                and q.get("code_challenge_method") == "S256"
                and bool(re.fullmatch(r"[A-Za-z0-9_-]{43}", q.get("code_challenge", "")))
                and q.get("resource") == cfg.resource
                and q.get("scope", "browser") == "browser"
                and 0 < len(q.get("state", "")) <= 2048
            )
            if not valid:
                return JSONResponse({"error": "invalid_request"}, status_code=400)
            nonce = secrets.token_urlsafe(32)
            self.store.put("authorize", nonce, q, 300)
            body = f"""<!doctype html><meta charset=utf-8><title>Cloud Browser authorization</title>
            <h1>Authorize your personal browser</h1><p>This grants ChatGPT control of your server browser.
            Website actions still require separate approval in your private console.</p>
            <p>Client: {html.escape(cfg.oauth_client_id)}</p>
            <form method=post action=/authorize><input type=hidden name=nonce value='{nonce}'>
            <label>Administrator password <input type=password name=password required autocomplete=current-password maxlength=1024></label>
            <button>Authorize</button></form>"""
            result = HTMLResponse(body)
            result.set_cookie(
                "cb_oauth",
                nonce,
                secure=not cfg.development,
                httponly=True,
                samesite="lax",
                max_age=300,
                path="/authorize",
            )
            return result

        @app.post("/authorize")
        async def authorize_submit(request: Request):
            if request.headers.get("origin") != cfg.public_origin:
                return JSONResponse({"error": "invalid_request"}, status_code=403)
            if self.limited(request.client.host if request.client else "unknown"):
                return JSONResponse({"error": "temporarily_unavailable"}, status_code=429)
            form = await request.form()
            nonce = str(form.get("nonce", ""))
            if not nonce or not hmac.compare_digest(nonce, request.cookies.get("cb_oauth", "")):
                return JSONResponse({"error": "invalid_request"}, status_code=403)
            q = self.store.pop("authorize", nonce)
            if not q or not await self.password_ok(str(form.get("password", ""))):
                return JSONResponse({"error": "access_denied"}, status_code=403)
            code = secrets.token_urlsafe(32)
            self.store.put("code", code, q, 60)
            separator = "&" if "?" in q["redirect_uri"] else "?"
            result = RedirectResponse(
                q["redirect_uri"]
                + separator
                + urlencode({"code": code, "state": q["state"], "iss": cfg.public_origin}),
                status_code=303,
            )
            result.delete_cookie("cb_oauth", path="/authorize")
            return result

        @app.post("/token")
        async def token(request: Request):
            f = await request.form()
            bad = JSONResponse({"error": "invalid_grant"}, status_code=400)
            if f.get("client_id") != cfg.oauth_client_id or f.get("resource") != cfg.resource:
                return bad
            if f.get("grant_type") == "authorization_code":
                q = self.store.pop("code", str(f.get("code", "")))
                verifier = str(f.get("code_verifier", ""))
                challenge = (
                    base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                    .decode()
                    .rstrip("=")
                )
                if (
                    not q
                    or not re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", verifier)
                    or not hmac.compare_digest(challenge, q["code_challenge"])
                    or f.get("redirect_uri") != q["redirect_uri"]
                ):
                    return bad
                grant = secrets.token_urlsafe(32)
                self.store.put("grant", grant, {"active": True}, cfg.refresh_ttl)
            elif f.get("grant_type") == "refresh_token":
                q = self.store.pop("refresh", str(f.get("refresh_token", "")))
                if not q or not self.store.get("grant", q["grant"]):
                    return bad
                grant = q["grant"]
            else:
                return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)
            return JSONResponse(self.issue(grant), headers={"Cache-Control": "no-store"})

        @app.post("/revoke")
        async def revoke(request: Request):
            f = await request.form()
            if f.get("client_id") != cfg.oauth_client_id:
                return JSONResponse({"error": "invalid_client"}, status_code=400)
            value = str(f.get("token", ""))
            q = self.store.get("access", value) or self.store.get("refresh", value)
            if q:
                self.store.delete("grant", q["grant"])
            return JSONResponse({}, headers={"Cache-Control": "no-store"})
