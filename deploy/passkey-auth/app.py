"""Passkey front door; existing MCP OAuth providers retain token issuance and PKCE."""
from __future__ import annotations

import argparse
import base64
import hmac
import json
import os
import re
import secrets
import shlex
import time
from collections import defaultdict, deque
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import httpx
from bridge_proof import sign as bridge_sign
from starlette.applications import Starlette
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route
from store import Store, digest
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers.structs import (
    AuthenticatorAttachment,
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

COOKIE = "__Host-personal-mcp-passkey"
OWNER = b"home-mcp-personal-owner-v1"
HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
           "X-Content-Type-Options": "nosniff", "Permissions-Policy":
           "publickey-credentials-create=(self), publickey-credentials-get=(self)"}


def encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def read_environment(path: str) -> dict[str, str]:
    """Read systemd EnvironmentFile assignments without executing shell text."""
    values = {}
    for raw in Path(path).read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError("invalid_environment_file")
        key, value = line.split("=", 1)
        parts = shlex.split(value, comments=False, posix=True)
        values[key.strip()] = " ".join(parts)
    return values


def allowed_callback(location: str) -> bool:
    try:
        url = urlsplit(location)
        return (len(location) <= 8192 and "\r" not in location and "\n" not in location
                and url.scheme == "https" and url.netloc == "chatgpt.com"
                and (url.path == "/connector_platform_oauth_redirect"
                     or re.fullmatch(r"/connector/oauth/[A-Za-z0-9_-]+", url.path) is not None))
    except ValueError:
        return False


class Portal:
    def __init__(self, config: dict, store: Store, transport=None):
        self.origin = config["origin"].rstrip("/")
        parsed = urlsplit(self.origin)
        if parsed.scheme != "https" or parsed.path or parsed.username or parsed.port:
            raise ValueError("a_fixed_https_origin_is_required")
        self.rp_id = parsed.hostname
        self.store = store
        self.transport = transport
        self.device_bound = config.get("device_bound", True)
        self.services = {}
        for item in config.get("services", []):
            url = urlsplit(item["login_url"])
            if url.scheme != "http" or url.hostname != "127.0.0.1" or not url.port:
                raise ValueError("only_fixed_loopback_backends_are_allowed")
            self.services[item["id"]] = dict(item)
        self.rate = defaultdict(deque)
        self.app = Starlette(routes=[
            Route("/healthz", self.health), Route("/", self.page),
            Route("/connect", self.page), Route("/enroll", self.page),
            Route("/portal.js", self.script),
            Route("/login/{service}", self.start),
            Route("/finish-control", self.finish_control),
            Route("/api/status", self.status),
            Route("/api/auth/options", self.authentication_options, methods=["POST"]),
            Route("/api/auth/verify", self.authentication_verify, methods=["POST"]),
            Route("/api/register/options", self.registration_options, methods=["POST"]),
            Route("/api/register/verify", self.registration_verify, methods=["POST"]),
            Route("/api/connect", self.connect, methods=["POST"]),
            Route("/api/enroll-ticket", self.enroll_ticket, methods=["POST"]),
            Route("/api/remove", self.remove, methods=["POST"]),
            Route("/api/logout", self.logout, methods=["POST"]),
        ])
        self.app.add_middleware(TrustedHostMiddleware, allowed_hosts=[self.rp_id])

    def limit(self, name: str, maximum: int = 120, request: Request | None = None):
        now = time.monotonic()
        if len(self.rate) > 512:
            for key in list(self.rate):
                if not self.rate[key] or self.rate[key][-1] <= now - 60:
                    del self.rate[key]
        if request:
            name += ":" + digest(request.client.host if request.client else "unknown")
        if len(self.rate) >= 1024 and name not in self.rate:
            name = "overflow"
        queue = self.rate[name]
        while queue and queue[0] <= now - 60:
            queue.popleft()
        if len(queue) >= maximum:
            raise ValueError("rate_limited")
        queue.append(now)

    def response(self, value: dict, status=200):
        return JSONResponse(value, status_code=status, headers=HEADERS)

    def session(self, request: Request, authenticated=False):
        token = request.cookies.get(COOKIE, "")
        session = self.store.session(token)
        if not session:
            raise ValueError("session_expired")
        if authenticated and (not session.get("credential") or session.get("verified", 0) < time.time() - 300):
            raise ValueError("authentication_required")
        return token, session

    async def body(self, request: Request, authenticated=False):
        self.limit("api", request=request)
        if request.headers.get("origin") != self.origin:
            raise ValueError("invalid_origin")
        if request.headers.get("content-type", "").split(";")[0] != "application/json":
            raise ValueError("json_required")
        token, session = self.session(request, authenticated)
        if not hmac.compare_digest(request.headers.get("x-passkey-csrf", ""), session["csrf"]):
            raise ValueError("invalid_csrf")
        chunks = []
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > 65536:
                raise ValueError("request_too_large")
            chunks.append(chunk)
        value = json.loads(b"".join(chunks))
        if not isinstance(value, dict):
            raise ValueError("invalid_json")
        return token, session, value

    def cookie(self, response: Response, credential: str = "", registered: bool = False):
        token = self.store.put("session", {"csrf": secrets.token_urlsafe(32), "credential": credential,
                                          "verified": int(time.time()) if credential else 0,
                                          "registered_at": int(time.time()) if registered else 0}, 1800)
        response.set_cookie(COOKIE, token, secure=True, httponly=True, samesite="strict", path="/", max_age=1800)
        return response

    async def health(self, request):
        return self.response({"ok": True, "version": "1.0.0"})

    async def page(self, request: Request):
        response = HTMLResponse(Path(__file__).with_name("portal.html").read_text(encoding="utf-8"),
                                headers={**HEADERS, "Content-Security-Policy":
                                         "default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; "
                                         "connect-src 'self'; img-src 'self'; frame-ancestors 'none'; "
                                         "base-uri 'none'; form-action 'none'"})
        if not self.store.session(request.cookies.get(COOKIE, "")):
            self.cookie(response)
        return response

    async def script(self, request):
        return Response(Path(__file__).with_name("portal.js").read_text(encoding="utf-8"),
                        media_type="text/javascript", headers=HEADERS)

    async def status(self, request):
        try:
            _, session = self.session(request)
            authenticated = bool(session.get("credential") and session.get("verified", 0) > time.time() - 300)
            credentials = self.store.credentials()
            flow = self.store.get("flow", request.query_params.get("flow", ""))
            target = self.services[flow["service"]]["name"] if flow else ""
            devices = [{k: row[k] for k in ("id", "label", "created", "last_used", "device_type")}
                       for row in credentials] if authenticated else []
            current = self.store.credential(session["credential"]) if authenticated else None
            return self.response({"csrf": session["csrf"], "authenticated": authenticated,
                                  "configured": bool(credentials), "devices": devices,
                                  "registration_completed": bool(authenticated and session.get("registered_at", 0) > time.time() - 300),
                                  "current_passkey": {"label": current["label"], "device_type": current["device_type"]} if current else None,
                                  "services": [{"id": s["id"], "name": s["name"]} for s in self.services.values()],
                                  "device_bound": self.device_bound, "target": target})
        except ValueError:
            return self.response({"error": "session_expired"}, 401)

    async def client(self, method: str, service: dict, **kwargs):
        async with httpx.AsyncClient(transport=self.transport, trust_env=False, timeout=15,
                                    follow_redirects=False) as client:
            headers = {"Host": service["host"], **kwargs.pop("headers", {})}
            return await client.request(method, service["login_url"], headers=headers, **kwargs)

    async def start(self, request: Request):
        try:
            self.limit("start", 60, request)
            service = self.services.get(request.path_params["service"])
            if service and service.get("kind", "").startswith("cloud-browser-"):
                params = dict(request.query_params) if service["kind"] == "cloud-browser-oauth" else {}
                response = await self.client("GET", service, params=params)
                nonce_match = re.search(r"name=nonce value='([A-Za-z0-9_-]{43})'", response.text)
                cookie_name = "cb_oauth" if service["kind"] == "cloud-browser-oauth" else "cb_login"
                if response.status_code != 200 or not nonce_match or response.cookies.get(cookie_name) != nonce_match[1]:
                    raise ValueError("invalid_authorization_request")
                flow = self.store.put("flow", {"service": service["id"], "nonce": nonce_match[1]}, 300)
                return RedirectResponse(f"{self.origin}/auth/connect?{urlencode({'flow': flow})}", status_code=303, headers=HEADERS)
            transaction = request.query_params.get("transaction", "")
            if not service or not transaction or len(transaction) > 16384:
                raise ValueError("invalid_authorization_request")
            # The existing provider validates its signed transaction before any flow is created.
            response = await self.client("GET", service, params={"transaction": transaction})
            if response.status_code != 200:
                raise ValueError("invalid_authorization_request")
            flow = self.store.put("flow", {"service": service["id"], "transaction": transaction}, 300)
            return RedirectResponse(f"{self.origin}/auth/connect?{urlencode({'flow': flow})}",
                                    status_code=303, headers=HEADERS)
        except (ValueError, httpx.HTTPError):
            return self.response({"error": "invalid_or_expired_authorization_request"}, 400)

    async def authentication_options(self, request: Request):
        try:
            token, _, _ = await self.body(request)
            credentials = self.store.credentials()
            if not credentials:
                raise ValueError("no_registered_device")
            options = generate_authentication_options(rp_id=self.rp_id,
                allow_credentials=[PublicKeyCredentialDescriptor(id=decode(row["id"])) for row in credentials],
                user_verification=UserVerificationRequirement.REQUIRED, timeout=120000)
            ceremony = self.store.put("ceremony", {"kind": "authentication", "session": digest(token),
                                                  "challenge": encode(options.challenge)}, 120)
            return self.response({"ceremony": ceremony, "options": json.loads(options_to_json(options))})
        except (ValueError, json.JSONDecodeError):
            return self.response({"error": "authentication_not_available"}, 403)

    async def authentication_verify(self, request: Request):
        try:
            token, _, body = await self.body(request)
            ceremony = self.store.get("ceremony", str(body.get("ceremony", "")), consume=True)
            if not ceremony or ceremony["kind"] != "authentication" or ceremony["session"] != digest(token):
                raise ValueError("invalid_ceremony")
            credential = body["credential"]
            stored = self.store.credential(credential["id"])
            if not stored:
                raise ValueError("unknown_credential")
            user_handle = credential.get("response", {}).get("userHandle")
            if user_handle and decode(user_handle) != OWNER:
                raise ValueError("invalid_owner")
            verified = verify_authentication_response(credential=credential,
                expected_challenge=decode(ceremony["challenge"]), expected_rp_id=self.rp_id,
                expected_origin=self.origin, credential_public_key=decode(stored["public_key"]),
                credential_current_sign_count=stored["sign_count"], require_user_verification=True)
            if self.device_bound and verified.credential_device_type.value != "single_device":
                raise ValueError("device_bound_required")
            if not self.store.update_counter(stored["id"], stored["sign_count"], verified.new_sign_count):
                raise ValueError("credential_changed")
            self.store.get("session", token, consume=True)
            return self.cookie(self.response({"ok": True}), stored["id"])
        except Exception:
            # Never echo library errors, challenge data or credential-bearing URLs.
            return self.response({"error": "authentication_failed"}, 403)

    async def registration_options(self, request: Request):
        try:
            token, _, body = await self.body(request)
            ticket = self.store.get("enroll", str(body.get("ticket", "")), consume=True)
            if not ticket:
                raise ValueError("invalid_enrollment_ticket")
            options = generate_registration_options(rp_id=self.rp_id, rp_name="Home MCP Login",
                user_id=OWNER, user_name="Home MCP owner", user_display_name="Home MCP owner",
                exclude_credentials=[PublicKeyCredentialDescriptor(id=decode(row["id"]))
                                     for row in self.store.credentials()],
                authenticator_selection=AuthenticatorSelectionCriteria(
                    authenticator_attachment=AuthenticatorAttachment.PLATFORM,
                    resident_key=ResidentKeyRequirement.REQUIRED,
                    user_verification=UserVerificationRequirement.REQUIRED), timeout=120000)
            ceremony = self.store.put("ceremony", {"kind": "registration", "session": digest(token),
                "challenge": encode(options.challenge), "label": ticket["label"]}, 120)
            return self.response({"ceremony": ceremony, "options": json.loads(options_to_json(options))})
        except Exception:
            return self.response({"error": "enrollment_link_expired"}, 403)

    async def registration_verify(self, request: Request):
        try:
            token, _, body = await self.body(request)
            ceremony = self.store.get("ceremony", str(body.get("ceremony", "")), consume=True)
            if not ceremony or ceremony["kind"] != "registration" or ceremony["session"] != digest(token):
                raise ValueError("invalid_ceremony")
            verified = verify_registration_response(credential=body["credential"],
                expected_challenge=decode(ceremony["challenge"]), expected_rp_id=self.rp_id,
                expected_origin=self.origin, require_user_verification=True)
            if self.device_bound and verified.credential_device_type.value != "single_device":
                return self.response({"error": "choose_windows_hello"}, 403)
            credential_id = encode(verified.credential_id)
            self.store.add_credential({"id": credential_id, "public_key": encode(verified.credential_public_key),
                "sign_count": verified.sign_count, "label": ceremony["label"],
                "device_type": verified.credential_device_type.value,
                "backed_up": int(verified.credential_backed_up), "created": int(time.time())})
            self.store.get("session", token, consume=True)
            return self.cookie(self.response({"ok": True}), credential_id, registered=True)
        except Exception:
            return self.response({"error": "registration_failed"}, 403)

    async def connect(self, request: Request):
        try:
            token, session, body = await self.body(request, authenticated=True)
            flow = self.store.get("flow", str(body.get("flow", "")), consume=True)
            if not flow:
                raise ValueError("flow_expired")
            service = self.services[flow["service"]]
            if service.get("kind", "").startswith("cloud-browser-"):
                environment = read_environment(service["environment_file"])
                secret = environment.get("CB_PASSKEY_BRIDGE_SECRET", "")
                audience = "oauth" if service["kind"] == "cloud-browser-oauth" else "control"
                cookie_name = "cb_oauth" if audience == "oauth" else "cb_login"
                origin = self.origin if audience == "oauth" else self.origin + ":9443"
                response = await self.client("POST", service,
                    headers={"Origin": origin, "Cookie": f"{cookie_name}={flow['nonce']}"},
                    data={"nonce": flow["nonce"], "passkey_assertion": bridge_sign(secret, flow["nonce"], audience)})
                location = response.headers.get("location", "")
                if audience == "oauth":
                    if response.status_code != 303 or not allowed_callback(location):
                        raise ValueError("authorization_failed")
                    return self.response({"ok": True, "redirect": location})
                control_cookie = response.cookies.get("cb_control", "")
                if response.status_code != 303 or location != "/" or not re.fullmatch(r"[A-Za-z0-9_-]{43}", control_cookie):
                    raise ValueError("authorization_failed")
                handoff = self.store.put("control_handoff", {"session": digest(token), "cookie": control_cookie}, 60)
                return self.response({"ok": True, "redirect": self.origin + ":9443/passkey/finish?" + urlencode({"ticket": handoff})})
            secret = read_environment(service["environment_file"]).get("MCP_OAUTH_LOGIN_SECRET", "")
            if len(secret) < 32 or not self.store.credential(session["credential"]):
                raise ValueError("service_not_ready")
            # Only the local provider receives its existing secret, after verified WebAuthn.
            response = await self.client("POST", service,
                data={"transaction": flow["transaction"], "access_key": secret})
            location = response.headers.get("location", "")
            if response.status_code not in (302, 303) or not allowed_callback(location):
                raise ValueError("authorization_failed")
            return self.response({"ok": True, "redirect": location})
        except Exception:
            return self.response({"error": "connection_failed_restart_from_chatgpt"}, 403)

    async def finish_control(self, request: Request):
        try:
            if request.headers.get("host") != self.rp_id + ":9443":
                raise ValueError("private_console_required")
            token, _ = self.session(request, authenticated=True)
            handoff = self.store.get("control_handoff", request.query_params.get("ticket", ""), consume=True)
            if not handoff or handoff["session"] != digest(token):
                raise ValueError("invalid_handoff")
            response = RedirectResponse(self.origin + ":9443/", status_code=303, headers=HEADERS)
            response.set_cookie("cb_control", handoff["cookie"], secure=True, httponly=True,
                                samesite="strict", path="/", max_age=28800)
            return response
        except Exception:
            return self.response({"error": "private_console_sign_in_failed"}, 403)

    async def enroll_ticket(self, request: Request):
        try:
            _, _, body = await self.body(request, authenticated=True)
            label = str(body.get("label", "")).strip()
            if not 1 <= len(label) <= 60:
                raise ValueError("invalid_label")
            ticket = self.store.put("enroll", {"label": label}, 900)
            return self.response({"url": f"{self.origin}/auth/enroll#{ticket}", "expires_in": 900})
        except Exception:
            return self.response({"error": "sign_in_and_supply_a_device_name"}, 403)

    async def remove(self, request: Request):
        try:
            _, _, body = await self.body(request, authenticated=True)
            self.store.delete_credential(str(body.get("id", "")))
            return self.response({"ok": True})
        except Exception:
            return self.response({"error": "cannot_remove_last_device_or_not_authenticated"}, 403)

    async def logout(self, request: Request):
        try:
            token, _, _ = await self.body(request)
            self.store.get("session", token, consume=True)
            response = self.response({"ok": True})
            response.delete_cookie(COOKIE, path="/", secure=True, httponly=True, samesite="strict")
            return response
        except Exception:
            return self.response({"error": "invalid_session"}, 403)


def load_config():
    return json.loads(Path(os.environ["MCP_PASSKEY_CONFIG"]).read_text(encoding="utf-8"))


def create_app():
    config = load_config()
    return Portal(config, Store(config["database"])).app


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["enroll", "status", "cancel-enrollments"])
    parser.add_argument("--label", default="관리 PC")
    args = parser.parse_args()
    config = load_config()
    store = Store(config["database"])
    if args.action == "status":
        print(json.dumps({"registered_devices": [{k: row[k] for k in ("label", "created", "last_used")}
                                                  for row in store.credentials()]}, ensure_ascii=False))
    elif args.action == "cancel-enrollments":
        with store.db() as db:
            count = db.execute("DELETE FROM temporary WHERE kind='enroll'").rowcount
        print(json.dumps({"cancelled_pending_enrollments": count}))
    else:
        if not 1 <= len(args.label.strip()) <= 60:
            raise SystemExit("invalid label")
        ticket = store.put("enroll", {"label": args.label.strip()}, 900)
        print(f"{config['origin'].rstrip('/')}/auth/enroll#{ticket}")
