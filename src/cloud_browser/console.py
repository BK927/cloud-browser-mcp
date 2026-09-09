import asyncio
import contextlib
import hmac
import html
import json
import secrets
import time
from urllib.parse import quote, urlsplit

import websockets
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import UploadFile

from .models import BrowserError
from .uploads import BodyLimit


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
        if request.url.path not in ("/login", "/healthz", "/favicon.ico") and not identity(
            request.cookies
        ):
            return RedirectResponse("/login", status_code=303)
        if request.method == "POST" and request.headers.get("origin") != cfg.control_origin:
            return JSONResponse({"error": "Invalid origin"}, status_code=403)
        result = await call_next(request)
        result.headers["Cache-Control"] = "no-store"
        result.headers["X-Content-Type-Options"] = "nosniff"
        # Preserve a real Origin on same-origin form POSTs without accepting null.
        # Cross-origin referrers remain suppressed; non-documents keep no-referrer.
        result.headers["Referrer-Policy"] = (
            "same-origin"
            if result.headers.get("content-type", "").startswith("text/html")
            else "no-referrer"
        )
        result.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'"
        )
        return result

    @app.get("/healthz")
    async def health():
        return {"ok": True}

    @app.get("/favicon.ico")
    async def favicon():
        # A browser's automatic icon request must not redirect to /login and
        # replace the nonce cookie belonging to the already visible login form.
        return Response(status_code=204)

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
            await form.close()
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
        work_options = "".join(
            f"<option value='{html.escape(sid, quote=True)}'>{html.escape(sid)}</option>"
            for sid in service.sessions
        )
        blocks.append(
            f"<h2>Prepare a file for ChatGPT</h2><p>Selected files become available to the MCP by ID. Uploading to a website still requires a separate approval.</p>"
            f"<form method=post action='/uploads' enctype='multipart/form-data'><input type=hidden name=csrf value='{csrf}'>"
            f"<label>Destination work <select name=session_id required><option value=''>Choose work</option>{work_options}</select></label>"
            f"<input type=file name=file required><button>Prepare file (maximum {cfg.max_upload_mb} MB)</button></form>"
        )
        for sid in list(service.sessions):
            blocks.append(
                f"<h2>Isolated browser work</h2><p>{html.escape(sid)} — reclaim closes this work's tabs and ends its lease.</p>"
                f"<form method=post action='/sessions/{html.escape(sid, quote=True)}/reclaim'><input type=hidden name=csrf value='{csrf}'>"
                "<button>Close session and reclaim browser</button></form>"
                f"<p><a href='/sessions/{html.escape(sid, quote=True)}/artifacts'>This work's downloads and exports</a></p>"
            )
        for item in service.uploads.list():
            blocks.append(
                f"<p>{html.escape(item['display_name'])} ({item['size']} bytes)</p>"
                f"<form method=post action='/uploads/{item['upload_id']}/remove'><input type=hidden name=csrf value='{csrf}'><button>Remove prepared file</button></form>"
            )
        for review_id, item in list(service.pending.items()):
            if item["expires"] <= time.time():
                service.pending.pop(review_id, None)
                continue
            status = auth.store.get("approval", item["token"])
            if not status or status["state"] != "pending":
                continue
            display = {
                "current_page": item["confirmation"].get("current_page"),
                "declared_destination": item["confirmation"]["destination"],
                "destination_kind": item["confirmation"].get("destination_kind", "unknown"),
                "destination_verified": False,
                "data_sent": item["confirmation"]["data_sent"],
                "data_sent_truncated": item["confirmation"].get("data_sent_truncated", False),
                "files": item["confirmation"].get("files", []),
                "summary": item["confirmation"]["summary"],
                "exact_action": item["action"],
                "expires_at": item["confirmation"]["expires_at"],
            }
            blocks.append(
                f"<section><h2>Action approval</h2><pre>{html.escape(json.dumps(display, ensure_ascii=False, indent=2))}</pre>"
                "<p>The destination is declared by the page, not verified. Scripts and redirects may change it. Null means unknown.</p>"
                f"<form method=post action='/approval/{review_id}'><input type=hidden name=csrf value='{csrf}'>"
                "<button name=decision value=approve>Approve once</button><button name=decision value=deny>Deny</button></form></section>"
            )
        for lease in service.leases.values():
            if lease["state"] != "active":
                continue
            hid = html.escape(lease["handoff_id"], quote=True)
            if lease["kind"] == "auth":
                blocks.append(
                    f"<form method=post action='/handoff/{hid}/auth-result'><input type=hidden name=csrf value='{csrf}'>"
                    "<button name=outcome value=failed>Report login failed</button>"
                    "<button name=outcome value=unsupported>Only passkey/security key is available</button></form>"
                )
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
                f"<form method=post action='/handoff/{hid}/complete'><input type=hidden name=csrf value='{csrf}'><button>Finish control and return to ChatGPT</button></form>"
                f"<form method=post action='/handoff/{hid}/renew'><input type=hidden name=csrf value='{csrf}'><button>Extend private control access</button></form>"
                f"<form method=post action='/handoff/{hid}/cancel'><input type=hidden name=csrf value='{csrf}'><button>Cancel and close this browser session</button></form></section>"
            )
        blocks.append(
            f"<form method=post action=/revoke-all><input type=hidden name=csrf value='{csrf}'><button>Revoke all ChatGPT access</button></form>"
            f"<form method=post action=/logout><input type=hidden name=csrf value='{csrf}'><button>Sign out</button></form>"
        )
        return HTMLResponse("".join(blocks))

    @app.get("/sessions/{session_id}/artifacts")
    async def artifacts_index(session_id: str, request: Request):
        result = await service.call("artifacts", session_id=session_id)
        if result["status"] != "ok":
            return JSONResponse({"error": result["error"]}, status_code=409)
        csrf = html.escape(identity(request.cookies)["csrf"], quote=True)
        blocks = [
            "<!doctype html><meta charset=utf-8><h1>Private work artifacts</h1><p>Downloads are untrusted files. No executable preview is provided.</p>"
        ]
        for item in result["artifacts"]:
            blocks.append(
                f"<p>{html.escape(item['name'])} — {item['state']} — {item['size']} bytes</p>"
            )
            if item["state"] == "completed":
                blocks.append(
                    f"<form method=post action='/sessions/{html.escape(session_id, quote=True)}/artifacts/{item['artifact_id']}/download'><input type=hidden name=csrf value='{csrf}'><button>Download file to my device</button></form>"
                )
        blocks.append("<p><a href='/'>Console</a></p>")
        return HTMLResponse("".join(blocks))

    @app.post("/sessions/{session_id}/artifacts/{artifact_id}/download")
    async def artifact_download(session_id: str, artifact_id: str, request: Request):
        try:
            await checked_form(request)
            result = await service.private_download(session_id, artifact_id)
            return Response(
                result["bytes"],
                media_type="application/octet-stream",
                headers={
                    "Content-Disposition": "attachment; filename*=UTF-8''"
                    + quote(result["filename"], safe="")
                },
            )
        except BrowserError as exc:
            return JSONResponse({"error": exc.code}, status_code=409)

    @app.post("/uploads")
    async def upload(request: Request):
        form = None
        try:
            form = await checked_form(request)
            source = form.get("file")
            if not isinstance(source, UploadFile):
                raise BrowserError("INVALID_INPUT", "Choose one local file")
            await service.stage_upload(source, session_id=str(form.get("session_id", "")) or None)
            return RedirectResponse("/", status_code=303)
        except BrowserError as exc:
            code = (
                403 if exc.code == "CSRF_FAILED" else 413 if exc.code == "UPLOAD_TOO_LARGE" else 409
            )
            return JSONResponse({"error": exc.code}, status_code=code)
        finally:
            if form is not None:
                await form.close()

    @app.post("/uploads/{upload_id}/remove")
    async def remove_upload(upload_id: str, request: Request):
        try:
            await checked_form(request)
            await service.discard_upload(upload_id)
            return RedirectResponse("/", status_code=303)
        except BrowserError as exc:
            return JSONResponse({"error": exc.code}, status_code=409)

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
            completed = result["state"] == "completed"
            title = (
                "Control returned"
                if completed
                else (
                    "Control not returned — automation remains paused"
                    if result["automation_paused"]
                    else "Control not returned — browser session unavailable"
                )
            )
            return HTMLResponse(
                f"<h1>{title}</h1><pre>{html.escape(json.dumps(result, ensure_ascii=False, indent=2))}</pre><a href='/'>Console</a>",
                status_code=200 if completed else 409,
            )
        except BrowserError as exc:
            return JSONResponse({"error": exc.code}, status_code=409)

    @app.post("/handoff/{handoff_id}/auth-result")
    async def auth_result(handoff_id: str, request: Request):
        try:
            form = await checked_form(request)
            await service.report_auth_result(handoff_id, form.get("outcome"))
            return RedirectResponse("/", status_code=303)
        except BrowserError as exc:
            return JSONResponse({"error": exc.code}, status_code=409)

    @app.post("/handoff/{handoff_id}/renew")
    async def renew(handoff_id: str, request: Request):
        try:
            await checked_form(request)
            await service.renew_handoff(handoff_id)
            return RedirectResponse("/", status_code=303)
        except BrowserError as exc:
            return JSONResponse({"error": exc.code}, status_code=409)

    @app.post("/handoff/{handoff_id}/cancel")
    async def cancel(handoff_id: str, request: Request):
        try:
            await checked_form(request)
            await service.cancel_handoff(handoff_id)
            return RedirectResponse("/", status_code=303)
        except BrowserError as exc:
            return JSONResponse({"error": exc.code}, status_code=409)

    @app.post("/sessions/{session_id}/reclaim")
    async def reclaim(session_id: str, request: Request):
        try:
            await checked_form(request)
            await service.reclaim_session(session_id)
            return RedirectResponse("/", status_code=303)
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

    return BodyLimit(app, cfg.max_upload_mb * 1048576)
