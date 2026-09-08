import asyncio
import io
import json
import logging
import re
import socket
from datetime import datetime

import httpx
import pytest
import uvicorn
from conftest import FakeWorker
from fastapi.testclient import TestClient

from cloud_browser import http_diagnostics as diagnostics
from cloud_browser.config import Settings
from cloud_browser.server import PublicGuard, create_apps

SECRET = "CANARY-never-log-password-cookie-token-ip-192.0.2.44"


def scope(**extra):
    return {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "query_string": SECRET.encode(),
        "raw_path": ("/" + SECRET).encode(),
        "headers": [
            (name, SECRET.encode())
            for name in (
                b"authorization",
                b"cookie",
                b"user-agent",
                b"x-request-id",
                b"x-forwarded-for",
            )
        ],
        "client": (SECRET, 1234),
        "server": (SECRET, 8443),
        "root_path": SECRET,
        "state": {"request_id": SECRET},
        **extra,
    }


def capture():
    output = io.StringIO()
    logger = logging.Logger("isolated-test-diagnostic", level=logging.INFO)
    logger.propagate = False
    handler = logging.StreamHandler(output)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    return logger, output


def records(output):
    text = output.getvalue()
    assert SECRET not in text
    result = [json.loads(line) for line in text.splitlines()]
    for row in result:
        assert set(row) == {"ts", "event", "request_id", "method", "path", "status", "elapsed_ms"}
        assert re.fullmatch(r"[0-9a-f]{12}", row["request_id"])
        assert row["ts"].endswith("Z") and len(row["ts"]) == 24
        assert datetime.fromisoformat(row["ts"]).utcoffset().total_seconds() == 0
        assert row["method"] in diagnostics.METHODS | {"unknown"}
        assert row["path"] in diagnostics.PATHS | {"unknown"}
        assert row["status"] is None or type(row["status"]) is int and 100 <= row["status"] <= 599
        assert (
            type(row["elapsed_ms"]) is int and 0 <= row["elapsed_ms"] <= diagnostics.MAX_ELAPSED_MS
        )
    assert all(len(line.encode()) <= diagnostics.MAX_RECORD_BYTES for line in text.splitlines())
    return result


async def receive():
    return {"type": "http.request", "body": SECRET.encode(), "more_body": False}


async def response_app(scope, receive, send):
    await receive()
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"set-cookie", SECRET.encode())],
        }
    )
    await send({"type": "http.response.body", "body": SECRET.encode(), "more_body": False})
    return "unchanged-result"


async def discard(message):
    pass


async def test_arrival_before_body_and_separate_streaming_stages():
    logger, output = capture()
    incoming, finish = asyncio.Event(), asyncio.Event()
    sent_start = asyncio.Event()
    original_scope = scope()
    original_body = {"type": "http.request", "body": SECRET.encode()}
    messages = [
        {"type": "http.response.start", "status": 201, "headers": [(b"location", SECRET.encode())]},
        {"type": "http.response.body", "body": SECRET.encode(), "more_body": True},
        {"type": "http.response.body", "body": b"", "more_body": False},
    ]
    forwarded = []

    async def source():
        await incoming.wait()
        return original_body

    async def destination(message):
        forwarded.append(message)

    async def app(actual_scope, actual_receive, send):
        assert actual_scope is original_scope and actual_receive is source
        assert await actual_receive() is original_body
        await send(messages[0])
        await send(messages[1])
        sent_start.set()
        await finish.wait()
        await send(messages[2])
        return "unchanged"

    task = asyncio.create_task(
        diagnostics.HTTPDiagnostics(app, logger=logger)(original_scope, source, destination)
    )
    try:
        await asyncio.sleep(0)
        assert [row["event"] for row in records(output)] == ["request_received"]
        assert not forwarded
        incoming.set()
        await asyncio.wait_for(sent_start.wait(), 2)
        assert [row["event"] for row in records(output)] == ["request_received", "response_started"]
        finish.set()
        assert await task == "unchanged"
        rows = records(output)
        assert [row["event"] for row in rows] == [
            "request_received",
            "response_started",
            "response_completed",
        ]
        assert [row["status"] for row in rows] == [None, 201, 201]
        assert len({row["request_id"] for row in rows}) == 1
        assert all(actual is expected for actual, expected in zip(forwarded, messages, strict=True))
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("stage", ["before-start", "after-start", "after-complete", "incomplete"])
async def test_failure_has_no_exception_message_traceback_or_locals(stage):
    logger, output = capture()
    failure = RuntimeError(SECRET)

    async def app(scope, receive, send):
        secret_local = SECRET
        assert secret_local
        if stage != "before-start":
            await send({"type": "http.response.start", "status": 502, "headers": []})
        if stage == "after-complete":
            await send({"type": "http.response.body", "body": SECRET.encode()})
        if stage != "incomplete":
            raise failure

    wrapped = diagnostics.HTTPDiagnostics(app, logger=logger)
    if stage == "incomplete":
        assert await wrapped(scope(), receive, discard) is None
    else:
        with pytest.raises(RuntimeError) as caught:
            await wrapped(scope(), receive, discard)
        assert caught.value is failure
    rows = records(output)
    assert rows[-1]["event"] == "request_failed"
    assert "Traceback" not in output.getvalue() and "RuntimeError" not in output.getvalue()


@pytest.mark.parametrize("after_start", [False, True])
async def test_cancellation_is_re_raised_without_secret(after_start):
    logger, output = capture()
    pending = asyncio.Event()

    async def app(scope, receive, send):
        if after_start:
            await send({"type": "http.response.start", "status": 200})
        pending.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(
        diagnostics.HTTPDiagnostics(app, logger=logger)(scope(), receive, discard)
    )
    await asyncio.wait_for(pending.wait(), 2)
    task.cancel(SECRET)
    with pytest.raises(asyncio.CancelledError):
        await task
    rows = records(output)
    assert rows[-1]["event"] == "request_cancelled"
    assert not any(row["event"] == "response_completed" for row in rows)
    assert rows[-1]["status"] == (200 if after_start else None)


@pytest.mark.parametrize("failure_at", ["receive", "start", "body"])
async def test_io_failure_never_logs_payload_or_changes_exception(failure_at):
    logger, output = capture()
    error = OSError(SECRET)

    async def source():
        if failure_at == "receive":
            raise error
        return await receive()

    async def sink(message):
        if message["type"] == f"http.response.{failure_at}":
            raise error

    with pytest.raises(OSError) as caught:
        await diagnostics.HTTPDiagnostics(response_app, logger=logger)(scope(), source, sink)
    assert caught.value is error
    assert records(output)[-1]["event"] == "request_failed"
    assert not any(row["event"] == "response_completed" for row in records(output))


async def test_trailers_delay_completion_without_recording_trailer_values():
    logger, output = capture()

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "trailers": True})
        await send({"type": "http.response.body", "body": SECRET.encode()})
        assert len(records(output)) == 2
        await send(
            {
                "type": "http.response.trailers",
                "headers": [(b"secret", SECRET.encode())],
                "more_trailers": True,
            }
        )
        assert len(records(output)) == 2
        await send({"type": "http.response.trailers", "headers": [], "more_trailers": False})

    await diagnostics.HTTPDiagnostics(app, logger=logger)(scope(), receive, discard)
    assert records(output)[-1]["event"] == "response_completed"


@pytest.mark.parametrize(
    "method,path",
    [
        (SECRET, "/mcp/" + SECRET),
        ("GET", "/authorize/" + SECRET),
        ("GET", "/.well-known/oauth-authorization-server/" + SECRET),
        ("POST", "/mcp?" + SECRET),
        ("X" * 100000, "/" + "X" * 100000),
        (None, None),
    ],
    ids=["method-and-path", "oauth-suffix", "metadata-suffix", "query-in-path", "long", "missing"],
)
async def test_untrusted_or_long_values_become_fixed_unknown(method, path):
    logger, output = capture()
    await diagnostics.HTTPDiagnostics(response_app, logger=logger)(
        scope(method=method, path=path), receive, discard
    )
    for row in records(output):
        assert row["path"] == "unknown"
        assert row["method"] == (method if method in ("GET", "POST") else "unknown")


async def test_rate_cap_is_bounded_and_never_rejects_or_retries_requests():
    logger, output = capture()
    now = [0.0]
    calls = []

    async def app(scope, receive, send):
        calls.append(1)
        return await response_app(scope, receive, send)

    wrapped = diagnostics.HTTPDiagnostics(app, logger=logger, clock=lambda: now[0])
    for _ in range(60):
        assert await wrapped(scope(), receive, discard) == "unchanged-result"
    assert len(calls) == 60
    assert len(records(output)) == diagnostics.MAX_EVENTS_PER_MINUTE
    assert len(wrapped.events) == diagnostics.MAX_EVENTS_PER_MINUTE
    now[0] = 59.999
    await wrapped(scope(), receive, discard)
    assert len(records(output)) == diagnostics.MAX_EVENTS_PER_MINUTE
    now[0] = 60.0
    await wrapped(scope(), receive, discard)
    assert len(records(output)) == diagnostics.MAX_EVENTS_PER_MINUTE + 3
    assert len(wrapped.events) == 3


async def test_rate_cap_can_omit_later_stages_without_changing_response():
    logger, output = capture()
    wrapped = diagnostics.HTTPDiagnostics(response_app, logger=logger, clock=lambda: 0)
    wrapped.events.extend([0] * (diagnostics.MAX_EVENTS_PER_MINUTE - 1))
    assert await wrapped(scope(), receive, discard) == "unchanged-result"
    assert [row["event"] for row in records(output)] == ["request_received"]


async def test_elapsed_and_status_fields_are_bounded():
    logger, output = capture()
    now = [0.0]

    async def app(scope, receive, send):
        now[0] = 1e9
        await send({"type": "http.response.start", "status": SECRET})
        await send({"type": "http.response.body", "body": b""})

    await diagnostics.HTTPDiagnostics(app, logger=logger, clock=lambda: now[0])(
        scope(), receive, discard
    )
    rows = records(output)
    assert all(row["status"] is None for row in rows)
    assert rows[-1]["elapsed_ms"] == diagnostics.MAX_ELAPSED_MS


async def test_logger_failure_does_not_replace_response_or_exception(capsys):
    class BrokenLogger:
        def info(self, value):
            raise RuntimeError(SECRET)

    assert (
        await diagnostics.HTTPDiagnostics(response_app, logger=BrokenLogger())(
            scope(), receive, discard
        )
        == "unchanged-result"
    )
    assert not capsys.readouterr().err


@pytest.mark.parametrize(
    "status",
    [99, 600, 10**100, True, SECRET],
    ids=["low", "high", "huge", "boolean", "secret-string"],
)
async def test_invalid_status_is_not_printed(status):
    logger, output = capture()
    original = {"type": "http.response.start", "status": status}
    seen = []

    async def app(scope, receive, send):
        await send(original)
        await send({"type": "http.response.body"})

    async def sink(message):
        seen.append(message)

    await diagnostics.HTTPDiagnostics(app, logger=logger)(scope(), receive, sink)
    assert seen[0] is original and seen[0]["status"] is status
    assert all(row["status"] is None for row in records(output))


async def test_logger_failure_preserves_original_application_error(capsys):
    class BrokenLogger:
        def info(self, value):
            raise OSError(SECRET)

    original = RuntimeError(SECRET)

    async def app(scope, receive, send):
        raise original

    with pytest.raises(RuntimeError) as caught:
        await diagnostics.HTTPDiagnostics(app, logger=BrokenLogger())(scope(), receive, discard)
    assert caught.value is original
    assert not capsys.readouterr().err


def test_output_stream_failure_does_not_print_logging_traceback(capsys):
    class BrokenStream:
        def write(self, value):
            raise OSError(SECRET)

        def flush(self):
            raise OSError(SECRET)

    handler = diagnostics._QuietHandler(BrokenStream())
    try:
        logger = logging.Logger("stream-failure", level=logging.INFO)
        logger.propagate = False
        logger.addHandler(handler)
        logger.info("fixed-safe-message")
        assert not capsys.readouterr().err
    finally:
        handler.setStream(io.StringIO())


@pytest.mark.parametrize("kind", ["lifespan", "websocket"])
async def test_non_http_is_untouched_and_not_logged(kind):
    logger, output = capture()
    original = scope(type=kind)

    async def app(actual, source, destination):
        assert actual is original and source is receive and destination is discard
        return SECRET

    assert (
        await diagnostics.HTTPDiagnostics(app, logger=logger)(original, receive, discard) == SECRET
    )
    assert not output.getvalue()


def test_default_disabled_has_no_wrapper_or_logs(cfg, capsys):
    assert (
        Settings(_env_file=None, _env_prefix="TEST_ISOLATED_", development=True).http_diagnostics
        is False
    )
    assert cfg.http_diagnostics is False
    public, _, _, _ = create_apps(cfg, worker=FakeWorker())
    assert isinstance(public, PublicGuard)
    with TestClient(public, base_url=cfg.public_origin) as client:
        assert client.get("/mcp").status_code == 401
    assert not capsys.readouterr().err


def test_enabled_outer_guard_logs_denials_but_preserves_boundaries(cfg, capsys):
    results = []
    for enabled in (False, True):
        cfg.http_diagnostics = enabled
        public, control, _, _ = create_apps(cfg, worker=FakeWorker())
        if enabled:
            assert isinstance(public, diagnostics.HTTPDiagnostics)
            assert isinstance(public.app, PublicGuard)
        with TestClient(public, base_url=cfg.public_origin) as client:
            responses = [
                client.get("/mcp", headers={"Authorization": SECRET}),
                client.get("/.well-known/oauth-authorization-server", params={"secret": SECRET}),
                client.head("/.well-known/oauth-authorization-server"),
                client.get("/token", headers={"Host": "wrong.example"}),
                client.post("/authorize", headers={"Origin": "null"}),
                client.get("/login"),
            ]
            results.append([(r.status_code, dict(r.headers), r.content) for r in responses])
            assert [r.status_code for r in responses] == [401, 200, 404, 421, 403, 404]
            public_logs = capsys.readouterr().err
            with TestClient(control, base_url=cfg.control_origin) as private:
                assert private.get("/login").status_code == 200
            assert not capsys.readouterr().err
        if enabled:
            rows = records(io.StringIO(public_logs))
            assert len(rows) == 18
            assert len({row["request_id"] for row in rows}) == 6
            assert rows[-1]["path"] == "unknown"
        else:
            assert not public_logs
    assert results[0] == results[1]


async def test_real_uvicorn_warning_without_access_log_still_emits_json(cfg, capsys):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    cfg.public_origin = f"http://127.0.0.1:{port}"
    cfg.http_diagnostics = True
    public, _, _, _ = create_apps(cfg, worker=FakeWorker())
    server = uvicorn.Server(
        uvicorn.Config(
            public,
            host="127.0.0.1",
            port=port,
            log_level="warning",
            access_log=False,
            proxy_headers=False,
        )
    )
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.02)
        assert server.started
        async with httpx.AsyncClient(base_url=cfg.public_origin, trust_env=False) as client:
            response = await client.get(
                "/mcp",
                params={"secret": SECRET},
                headers={"Authorization": SECRET, "User-Agent": SECRET},
            )
        assert response.status_code == 401
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)
        listener.close()
    captured = capsys.readouterr()
    assert not captured.out
    rows = records(io.StringIO(captured.err))
    assert [row["event"] for row in rows] == [
        "request_received",
        "response_started",
        "response_completed",
    ]
