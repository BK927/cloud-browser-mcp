import asyncio
import contextlib
import hmac
import html
import json
import secrets
import time
from urllib.parse import urlsplit

import websockets
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .models import BrowserError


def control_app(cfg, auth, service):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    def identity(cookies):
        token = cookies.get("cb_control", "")
        return auth.store.get("control", token) if token else None

    @app.middleware("http")
    async def secure_control(request: Request, call_next):
        if (
            request.headers.get("host") != urlsplit(cfg.control_origin).netloc
            and request.url.path != "/healthz"
        ):
            return JSONResponse({"error": "Invalid host"}, status_code=421)
        if request.url.path not in ("/login", "/healthz") and not identity(request.cookies):
            return RedirectResponse("/login", status_code=303)
        if request.method == "POST" and request.headers.get("origin") != cfg.control_origin:
            return JSONResponse({"error": "Invalid origin"}, status_code=403)
        result = await call_next(request)
        result.headers["Cache-Control"] = "no-store"
        result.headers["X-Content-Type-Options"] = "nosniff"
        result.headers["Referrer-Policy"] = "no-referrer"
        result.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'"
        )
        return result

    @app.get("/healthz")
    async def health():
        return {"ok": True}

    @app.get("/login")
    async def login():
        nonce = secrets.token_urlsafe(32)
        auth.store.put("login", nonce, {"active": True}, 300)
        result = HTMLResponse(f"""<!doctype html><meta charset=utf-8><title>Private browser console</title>
        <h1>Private browser console</h1><form method=post><input type=hidden name=nonce value='{nonce}'>
        <label>Administrator password <input type=password name=password autocomplete=current-password required maxlength=1024></label><button>Sign in</button></form>""")
        result.set_cookie(
            "cb_login",
            nonce,
            secure=not cfg.development,
            httponly=True,
            samesite="strict",
            max_age=300,
            path="/login",
        )
        return result

    @app.post("/login")
    async def login_submit(request: Request):
        if auth.limited(request.client.host if request.client else "unknown"):
            return JSONResponse({"error": "Too many attempts; wait five minutes"}, status_code=429)
        form = await request.form()
        nonce = str(form.get("nonce", ""))
        if (
            not nonce
            or not hmac.compare_digest(nonce, request.cookies.get("cb_login", ""))
            or not auth.store.pop("login", nonce)
        ):
            return JSONResponse({"error": "Invalid login form"}, status_code=403)
        if not await auth.password_ok(str(form.get("password", ""))):
            return JSONResponse({"error": "Login denied"}, status_code=403)
        token = secrets.token_urlsafe(32)
        auth.store.put("control", token, {"csrf": secrets.token_urlsafe(32)}, 3600)
        result = RedirectResponse("/", status_code=303)
        result.set_cookie(
            "cb_control",
            token,
            secure=not cfg.development,
            httponly=True,
            samesite="strict",
            max_age=3600,
        )
        result.delete_cookie("cb_login", path="/login")
        return result

    async def checked_form(request):
        user = identity(request.cookies)
        form = await request.form()
        if not user or not hmac.compare_digest(str(form.get("csrf", "")), user["csrf"]):
            raise BrowserError("CSRF_FAILED", "Invalid console form")
        return form

    @app.get("/")
    async def index(request: Request):
        user = identity(request.cookies)
        csrf = html.escape(user["csrf"], quote=True)
        blocks = [
            "<!doctype html><meta charset=utf-8><title>Private browser console</title><h1>Private browser console</h1>",
            "<p>Only approve actions you recognize. Website text is untrusted. Refresh this page for updates.</p>",
        ]
        for review_id, item in list(service.pending.items()):
            if item["expires"] <= time.time():
                service.pending.pop(review_id, None)
                continue
            status = auth.store.get("approval", item["token"])
            if not status or status["state"] != "pending":
                continue
            display = {
                "destination": item["confirmation"]["destination"],
                "summary": item["confirmation"]["summary"],
                "exact_action": item["action"],
                "expires_at": item["confirmation"]["expires_at"],
            }
            blocks.append(
                f"<section><h2>Action approval</h2><pre>{html.escape(json.dumps(display, ensure_ascii=False, indent=2))}</pre>"
                f"<form method=post action='/approval/{review_id}'><input type=hidden name=csrf value='{csrf}'>"
                "<button name=decision value=approve>Approve once</button><button name=decision value=deny>Deny</button></form></section>"
            )
        for lease in service.leases.values():
            if lease["state"] != "active":
                continue
            hid = html.escape(lease["handoff_id"], quote=True)
            blocks.append(
                f"<section><h2>Manual control: {html.escape(lease['kind'])}</h2><p>{html.escape(lease['reason'])}</p>"
                "<p>Switch to the requested tab in the browser if needed. Close credential dialogs before finishing.</p>"
            )
            if cfg.manual_control_enabled and lease["expires"] > time.time():
                blocks.append(
                    "<p><a href='/novnc/vnc.html?autoconnect=true&amp;resize=scale&amp;path=desktop' target='_blank' rel='noopener'>Open private remote browser</a></p>"
                )
            else:
                blocks.append(
                    "<p>Remote-control access expired. Automation remains paused until you finish below.</p>"
                )
            blocks.append(
                f"<form method=post action='/handoff/{hid}/complete'><input type=hidden name=csrf value='{csrf}'><button>Finish control and return to ChatGPT</button></form></section>"
            )
        blocks.append(
            f"<form method=post action=/revoke-all><input type=hidden name=csrf value='{csrf}'><button>Revoke all ChatGPT access</button></form>"
            f"<form method=post action=/logout><input type=hidden name=csrf value='{csrf}'><button>Sign out</button></form>"
        )
        return HTMLResponse("".join(blocks))

    @app.post("/approval/{review_id}")
    async def approve(review_id: str, request: Request):
        try:
            form = await checked_form(request)
            if form.get("decision") not in ("approve", "deny"):
                raise BrowserError("INVALID_INPUT", "Choose approve or deny")
            await service.approve(review_id, form.get("decision") == "approve")
            return RedirectResponse("/", status_code=303)
        except BrowserError as exc:
            return JSONResponse({"error": exc.code}, status_code=409)

    @app.post("/handoff/{handoff_id}/complete")
    async def complete(handoff_id: str, request: Request):
        try:
            await checked_form(request)
            result = await service.complete_handoff(handoff_id)
            return HTMLResponse(
                f"<h1>Control returned</h1><pre>{html.escape(json.dumps(result, ensure_ascii=False, indent=2))}</pre><a href='/'>Console</a>"
            )
        except BrowserError as exc:
            return JSONResponse({"error": exc.code}, status_code=409)

    @app.post("/revoke-all")
    async def revoke_all(request: Request):
        try:
            await checked_form(request)
        except BrowserError:
            return JSONResponse({"error": "Invalid form"}, status_code=403)
        auth.store.delete_kind("grant")
        return RedirectResponse("/", status_code=303)

    @app.post("/logout")
    async def logout(request: Request):
        try:
            await checked_form(request)
        except BrowserError:
            return JSONResponse({"error": "Invalid form"}, status_code=403)
        auth.store.delete("control", request.cookies.get("cb_control", ""))
        result = RedirectResponse("/login", status_code=303)
        result.delete_cookie("cb_control")
        return result

    if cfg.manual_control_enabled and cfg.novnc_dir.is_dir():
        app.mount("/novnc", StaticFiles(directory=cfg.novnc_dir), name="novnc")

    @app.websocket("/desktop")
    async def desktop(ws: WebSocket):
        def allowed():
            return (
                cfg.manual_control_enabled
                and identity(ws.cookies)
                and ws.headers.get("origin") == cfg.control_origin
                and any(
                    lease["state"] == "active" and lease["expires"] > time.time()
                    for lease in service.leases.values()
                )
            )

        if not allowed():
            await ws.close(code=1008)
            return
        await ws.accept(subprotocol="binary")
        try:
            async with websockets.connect(
                cfg.vnc_websocket, subprotocols=["binary"], max_size=8 * 1024 * 1024
            ) as upstream:

                async def disconnect():
                    await upstream.close()
                    with contextlib.suppress(Exception):
                        await ws.close()

                service.control_disconnectors.add(disconnect)

                async def from_user():
                    while allowed():
                        message = await ws.receive_bytes()
                        if allowed():
                            await upstream.send(message)

                async def to_user():
                    async for message in upstream:
                        if not allowed():
                            return
                        if isinstance(message, bytes):
                            await ws.send_bytes(message)

                async def lease_guard():
                    while allowed():
                        await asyncio.sleep(0.5)

                tasks = [asyncio.create_task(fn()) for fn in (from_user, to_user, lease_guard)]
                try:
                    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    service.control_disconnectors.discard(disconnect)
        except Exception:
            pass  # Do not log remote desktop payloads or credentials.
        finally:
            with contextlib.suppress(Exception):
                await ws.close()

    return app
