import asyncio
import json
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from pydantic import ValidationError

from cloud_browser.config import Settings
from cloud_browser.models import BrowserError
from cloud_browser.output_models import ReadOutput
from cloud_browser.security import reader_safe_url


@pytest.fixture
def reader(service, monkeypatch):
    monkeypatch.setattr("cloud_browser.service.validate_url", lambda url, **kw: None)
    original = service.worker.call
    state = {"text": "A" * 2500, "error": None, "guard": None}

    async def invoke(method, **args):
        if method in ("navigation_begin", "navigation_poll", "navigation_cancel"):
            service.worker.calls.append((method, args))
            if method == "navigation_poll" and state["error"]:
                raise BrowserError(state["error"], "Navigation failed")
            return {
                "session_id": args["session_id"],
                "tab_id": args["tab_id"],
                "navigation": {"pending": method == "navigation_begin"},
            }
        if method == "observe" and state["guard"]:
            service.worker.calls.append((method, args))
            raise BrowserError(state["guard"], "Page guard", "blocked")
        result = await original(method, **args)
        if method == "open":
            profile = (
                service.cfg.data_dir / "profiles" / (args.get("profile") or args["session_id"])
            )
            profile.mkdir(parents=True, exist_ok=True)
            (profile / "retained").write_text("cookie data")
        if method == "observe":
            result["page"]["url"] = (
                "https://blog.naver.com/PostView.naver?blogId=someuser&logNo=223456789012"
            )
            result["observation"].update(
                semantic_snapshot=state["text"][: args["max_chars"]],
                semantic_truncated=len(state["text"]) > args["max_chars"],
                links=[{"text": "Article", "url": "https://example.com/article?blogId=user"}],
                links_truncated=False,
                protected_regions_omitted=True,
                frame_reading_truncated=False,
            )
        return result

    monkeypatch.setattr(service.worker, "call", invoke)
    return service, state


async def read(service, **args):
    return await service.call("read", _principal="alice", url="https://example.com/", **args)


async def test_read_and_cached_continuation(reader):
    service, _ = reader
    first = await read(service, max_chars=1000, selector="article")
    assert first["status"] == "ok", first
    ReadOutput.model_validate(first)
    assert first["session_id"] is first["tab_id"] is first["revision"] is None
    assert "lease_id" not in first and "operation_id" not in first
    assert "blogId=someuser" in first["page"]["url"]
    result = first["read"]
    assert result["complete"] is True and result["text"] == "A" * 1000
    assert result["links"] == [
        {"text": "Article", "url": "https://example.com/article?blogId=user"}
    ]
    assert result["next_offset"] == 1000 and result["total_chars"] == 2500
    assert result["protected_regions_omitted"] is True
    assert result["frame_reading_truncated"] is False
    observe = next(args for method, args in service.worker.calls if method == "observe")
    assert observe["mode"] == "semantic" and observe["max_chars"] == 60000
    assert observe["query"]["selector"] == "article"
    count = len(service.worker.calls)
    second = await service.call(
        "read", _principal="alice", read_id=result["read_id"], offset=1000, max_chars=1000
    )
    assert second["read"]["text"] == "A" * 1000
    assert second["read"]["links"] == [] and second["read"]["next_offset"] == 2000
    last = await service.call("read", _principal="alice", read_id=result["read_id"], offset=2000)
    assert last["read"]["text"] == "A" * 500 and last["read"]["next_offset"] is None
    assert len(service.worker.calls) == count


async def test_read_cache_principal_expiry_and_limit(reader):
    service, _ = reader
    rid = (await read(service))["read"]["read_id"]
    count = len(service.worker.calls)
    for principal, read_id in [("bob", rid), ("alice", "unknown")]:
        result = await service.call("read", _principal=principal, read_id=read_id)
        assert result["status"] == "error" and result["error"]["code"] == "READ_NOT_FOUND"
        assert result["error"]["suggested_tool"] == "browser_read"
    service.read_cache[rid]["expires"] = time.time() - 1
    expired = await service.call("read", _principal="alice", read_id=rid)
    assert expired["error"]["code"] == "READ_NOT_FOUND"
    assert len(service.worker.calls) == count
    ids = [(await read(service))["read"]["read_id"] for _ in range(5)]
    assert list(service.read_cache) == ids[1:]


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"url": "https://example.com", "read_id": "read_test"},
        {"url": "https://example.com", "offset": -1},
        {"read_id": "x", "max_chars": 999},
        {"url": "https://example.com", "selector": "x" * 1001},
    ],
)
async def test_invalid_read_inputs(service, args):
    result = await service.call("read", _principal="alice", **args)
    assert result["status"] == "error" and result["error"]["code"] == "INVALID_INPUT"
    assert service.worker.calls == []


LEASE_TOOLS = [
    "open",
    "list_tabs",
    "navigate",
    "observe",
    "act",
    "auth_request",
    "handoff",
    "close",
    "status",
    "configure",
    "list_page_tools",
    "call_page_tool",
    "wait",
    "dialog",
    "logs",
    "clipboard",
    "artifacts",
]


@pytest.mark.parametrize("method", LEASE_TOOLS)
async def test_reader_refuses_every_session_tool(reader, method):
    service, _ = reader
    await read(service)
    sid = service.reader_sid
    assert sid not in service.owners and service.store.get("session", sid) is None
    count = len(service.worker.calls)
    for principal in ("alice", None):
        result = await service.call(
            method, _principal=principal, lease_id="invented", session_id=sid
        )
        assert result["error"]["code"] == "LEASE_INVALID"
    assert len(service.worker.calls) == count
    with pytest.raises(BrowserError, match="reserved"):
        await service.stage_upload(None, session_id=sid)


async def test_reader_profile_status_capacity_and_idle_close(reader):
    service, _ = reader
    await read(service)
    sid = service.reader_sid
    assert (
        next(args for method, args in service.worker.calls if method == "open")["profile"]
        == "reader"
    )
    work = await service.call("open", _principal="alice")
    assert work["status"] == "ok"
    ordinary = [args for method, args in service.worker.calls if method == "open"][-1]
    assert ordinary.get("profile") is None
    assert (service.cfg.data_dir / "profiles" / work["session_id"] / "retained").exists()
    tabs = await service.call(
        "list_tabs", _principal="alice", session_id=work["session_id"], lease_id=work["lease_id"]
    )
    assert all(tab["tab_id"] != service.reader_tid for tab in tabs["tabs"])
    for principal in ("alice", None):
        status = await service.call("status", _principal=principal)
        assert status["reader"]["active"] is True
        assert sid not in json.dumps(status) and service.reader_tid not in json.dumps(status)
        assert status["scheduler"]["active_sessions"] == 2
    service.sessions[sid]["expires"] = time.time() - 1
    async with service.lock:
        await service._reap_expired()
    assert service.reader_sid is None and sid not in service.sessions
    assert ("close", {"session_id": sid, "scope": "session"}) in service.worker.calls
    assert (service.cfg.data_dir / "profiles" / "reader" / "retained").read_text() == "cookie data"
    await read(service)
    assert service.reader_sid != sid


async def test_reader_capacity_full(reader):
    service, _ = reader
    for _ in range(service.cfg.max_sessions):
        assert (await service.call("open", _principal="alice"))["status"] == "ok"
    count = len(service.worker.calls)
    result = await read(service)
    assert result["error"]["code"] == "BROWSER_BUSY" and result["busy_reason"] == "session_capacity"
    assert len(service.worker.calls) == count and service.reader_sid is None


async def test_reader_timeout_partial_and_navigation_errors(reader):
    service, state = reader
    state["error"] = "NAVIGATION_TIMEOUT"
    result = await read(service)
    assert result["status"] == "ok" and result["read"]["complete"] is False
    assert result["read"]["text"] == state["text"]
    assert any("partial" in notice for notice in result["notices"])
    state["error"] = "NAVIGATION_FAILED"
    result = await read(service)
    assert result["status"] == "error" and result["error"]["code"] == "NAVIGATION_FAILED"
    assert "read" not in result


@pytest.mark.parametrize(
    "guard", ["CAPTCHA_REQUIRED", "BOT_BLOCKED", "PRIVACY_INSPECTION_INCOMPLETE"]
)
async def test_reader_preserves_page_guards(reader, guard):
    service, state = reader
    state["guard"] = guard
    result = await read(service)
    assert result["status"] == "blocked" and result["error"]["code"] == guard
    assert any("human check later" in notice for notice in result["notices"])


async def test_reader_admission_capping_and_global_control(reader, monkeypatch):
    service, state = reader
    admitted = []
    admit = service._admit
    monkeypatch.setattr(
        service,
        "_admit",
        lambda cost, operation: (admitted.append((cost, operation)), admit(cost, operation))[1],
    )
    service.cfg.reader_max_text_chars = 4000
    state["text"] = "A" * 5000
    result = await read(service)
    assert result["read"]["total_chars"] == 4000 and result["read"]["text_capped"] is True
    assert admitted == [(service.cfg.memory_per_session_mb, "new_session"), (64, "navigation")]
    count = len(service.worker.calls)
    monkeypatch.setattr(
        service, "_active_control", lambda: {"kind": "manual", "session_id": "other"}
    )
    for args in ({"url": "https://example.com"}, {"read_id": result["read"]["read_id"]}):
        paused = await service.call("read", _principal="alice", **args)
        assert paused["error"]["code"] == "USER_CONTROL_ACTIVE"
    assert len(service.worker.calls) == count


async def test_reader_releases_global_lock_and_serializes_reads(reader, monkeypatch):
    service, _ = reader
    original = service.worker.call
    reached, resume = asyncio.Event(), asyncio.Event()

    async def invoke(method, **args):
        if method == "navigation_begin":
            reached.set()
        if method == "navigation_poll":
            assert service.lock.locked()
            assert args["readiness"] == "interactive"
            if not resume.is_set():
                return {"navigation": {"pending": True}}
        return await original(method, **args)

    monkeypatch.setattr(service.worker, "call", invoke)
    first = asyncio.create_task(read(service))
    await reached.wait()
    second = asyncio.create_task(read(service))
    work = await service.call("open", _principal="bob")
    assert work["status"] == "ok" and not first.done()
    assert len([c for c in service.worker.calls if c[0] == "navigation_begin"]) == 1
    resume.set()
    assert all(result["status"] == "ok" for result in await asyncio.gather(first, second))
    assert len([c for c in service.worker.calls if c[0] == "open" and c[1].get("profile")]) == 1


async def test_reader_keeps_lightweight_observation_under_pressure(reader, monkeypatch):
    service, state = reader
    state["text"] = "A" * 6000
    resources = service.resources
    monkeypatch.setattr(
        service,
        "resources",
        lambda admission=0: (
            resources(admission)
            if admission
            else {
                "available_mb": 100,
                "host_available_mb": 100,
                "memory_pressure": {"some": 0, "full": 0},
            }
        ),
    )
    result = await read(service)
    assert result["status"] == "ok", result
    assert result["read"]["resource_limited"] is True
    observed = next(args for method, args in service.worker.calls if method == "observe")
    assert observed["max_chars"] == 4000 and observed["lightweight"] is True


@pytest.fixture
def paced_reader(reader, monkeypatch):
    service, _ = reader
    clock, waits = [100.0], []
    monkeypatch.setattr(
        "cloud_browser.service.time",
        SimpleNamespace(monotonic=lambda: clock[0], time=time.time),
    )
    service.navigation_pacer.clock = lambda: clock[0]

    async def sleep(delay):
        assert not service.lock.locked() and not service.reader_lock.locked()
        waits.append(delay)
        clock[0] += delay

    service.navigation_pacer.sleep = sleep
    original = service.worker.call

    async def immediate(method, **args):
        result = await original(method, **args)
        if method == "navigation_begin":
            result["navigation"]["pending"] = False
        return result

    monkeypatch.setattr(service.worker, "call", immediate)
    return service, clock, waits


async def test_reader_paces_same_host_but_not_different_hosts(paced_reader):
    service, _, waits = paced_reader
    assert (await read(service))["status"] == "ok"
    assert (await read(service))["status"] == "ok"
    assert waits == [pytest.approx(1.5)]
    assert (await service.call("read", url="https://other.example/"))["status"] == "ok"
    assert waits == [pytest.approx(1.5)]


async def test_read_open_goto_and_reload_share_normalized_host(paced_reader):
    service, _, waits = paced_reader
    assert (await read(service))["status"] == "ok"
    opened = await service.call("open", url="https://WWW.Example.com/path")
    assert opened["status"] == "ok"
    args = {key: opened[key] for key in ("session_id", "tab_id")}
    assert (await service.call("navigate", **args, operation="goto", url="https://example.com"))[
        "status"
    ] == "ok"
    assert (await service.call("navigate", **args, operation="reload"))["status"] == "ok"
    assert waits == [pytest.approx(1.5)] * 3
    assert list(service.navigation_pacer.hosts) == ["example.com"]
    for operation in ("back", "forward"):
        assert (await service.call("navigate", **args, operation=operation))["status"] == "ok"
    assert (await service.call("observe", **args, max_chars=4000))["status"] == "ok"
    assert (
        await service.call(
            "act", **args, expected_revision=1, action={"type": "click", "node_id": "node_1"}
        )
    )["status"] == "confirmation_required"
    service.tab_cache.clear()
    before = len(service.worker.calls)
    assert (await service.call("navigate", **args, operation="reload"))["status"] == "ok"
    assert [method for method, _ in service.worker.calls[before:]] == ["navigation_begin"]
    assert waits == [pytest.approx(1.5)] * 3


async def test_host_cap_and_cached_read_slices(paced_reader):
    service, clock, waits = paced_reader
    service.cfg.navigation_min_interval_ms = 0
    service.cfg.navigation_per_host_per_minute = 2
    first = await read(service)
    assert (await read(service))["status"] == "ok"
    count = len(service.worker.calls)
    limited = await read(service)
    assert limited["error"]["code"] == "BROWSER_BUSY"
    assert limited["busy_reason"] == "host_rate_limit"
    assert limited["retry_after_seconds"] == 60
    assert len(service.worker.calls) == count and not waits
    cached = await service.call(
        "read", _principal="alice", read_id=first["read"]["read_id"], offset=1000
    )
    assert cached["status"] == "ok" and len(service.worker.calls) == count and not waits
    clock[0] += 60
    assert (await service.call("read", url="https://other.example/"))["status"] == "ok"
    assert list(service.navigation_pacer.hosts) == ["other.example"]
    assert (await read(service))["status"] == "ok"


@pytest.mark.parametrize("method", ["read", "open", "navigate"])
@pytest.mark.parametrize("refusal", ["validation", "worker", "admission"])
async def test_refused_navigations_are_not_recorded(paced_reader, monkeypatch, method, refusal):
    service, _, waits = paced_reader
    args = {"url": "https://example.com/"}
    if method == "navigate":
        opened = await service.call("open")
        args.update(operation="goto", **{key: opened[key] for key in ("session_id", "tab_id")})
    if refusal == "validation":

        def invalid(url, **kwargs):
            raise BrowserError("INVALID_URL", "Refused URL")

        monkeypatch.setattr("cloud_browser.service.validate_url", invalid)
        expected = "INVALID_URL"
    elif refusal == "worker":
        original = service.worker.call

        async def refused(method, **args):
            if method == "navigation_begin":
                raise BrowserError("INVALID_URL", "Worker refused URL")
            return await original(method, **args)

        monkeypatch.setattr(service.worker, "call", refused)
        expected = "INVALID_URL"
    else:

        def unavailable(*args, **kwargs):
            raise BrowserError("RESOURCE_PRESSURE", "No navigation headroom")

        monkeypatch.setattr(service, "_admit", unavailable)
        expected = "RESOURCE_PRESSURE"
    for _ in range(2):
        assert (await service.call(method, **args))["error"]["code"] == expected
        assert not service.navigation_pacer.hosts and not waits


@pytest.mark.parametrize("method", ["open", "navigate"])
@pytest.mark.parametrize("elapsed,interval_ms", [(0, 6000), (3, 3000)])
async def test_pacing_rejects_waits_outside_initial_response_budget(
    paced_reader, monkeypatch, method, elapsed, interval_ms
):
    service, clock, waits = paced_reader
    assert (await read(service))["status"] == "ok"
    service.cfg.navigation_min_interval_ms = interval_ms
    args = {"url": "https://example.com/"}
    if method == "navigate":
        opened = await service.call("open")
        args.update(operation="goto", **{key: opened[key] for key in ("session_id", "tab_id")})
    original = service._validate_navigation_url

    async def delayed_validation(url):
        await original(url)
        clock[0] += elapsed
        # Dispatch history may be fresher than call start due to other work.
        if elapsed:
            service.navigation_pacer.hosts["example.com"]["dispatches"].append(clock[0])

    monkeypatch.setattr(service, "_validate_navigation_url", delayed_validation)
    count = len(service.worker.calls)
    result = await service.call(method, **args)
    assert result["error"]["code"] == "BROWSER_BUSY"
    assert result["busy_reason"] == "host_rate_limit"
    assert result["retry_after_seconds"] == interval_ms // 1000
    assert len(service.worker.calls) == count and not waits


async def test_wait_that_fits_initial_budget_is_dispatched(paced_reader):
    service, _, waits = paced_reader
    assert (await read(service))["status"] == "ok"
    service.cfg.navigation_min_interval_ms = 4000
    result = await service.call("open", url="https://example.com/")
    assert result["status"] == "ok" and waits == [pytest.approx(4)]


async def test_concurrent_reads_share_dispatch_spacing(paced_reader):
    service, _, waits = paced_reader
    results = await asyncio.gather(read(service), read(service))
    assert all(result["status"] == "ok" for result in results)
    assert waits == [pytest.approx(1.5)]


async def test_waiting_host_leaves_other_hosts_and_locks_available(paced_reader):
    service, clock, _ = paced_reader
    assert (await read(service))["status"] == "ok"
    waiting, resume = asyncio.Event(), asyncio.Event()

    async def sleep(delay):
        assert not service.lock.locked() and not service.reader_lock.locked()
        waiting.set()
        await resume.wait()
        clock[0] += delay

    service.navigation_pacer.sleep = sleep
    second = asyncio.create_task(read(service))
    try:
        await asyncio.wait_for(waiting.wait(), 1)
        other = await asyncio.wait_for(service.call("read", url="https://other.example/"), 1)
        assert other["status"] == "ok" and not second.done()
    finally:
        resume.set()
        result = await second
    assert result["status"] == "ok"


@pytest.mark.parametrize(
    "setting,value",
    [
        ("navigation_min_interval_ms", -1),
        ("navigation_min_interval_ms", 60001),
        ("navigation_per_host_per_minute", 0),
        ("navigation_per_host_per_minute", 601),
    ],
)
def test_navigation_pacing_setting_bounds(setting, value):
    with pytest.raises(ValidationError):
        Settings(development=True, **{setting: value})


def test_navigation_pacing_environment_settings(monkeypatch):
    monkeypatch.setenv("CB_NAVIGATION_MIN_INTERVAL_MS", "0")
    monkeypatch.setenv("CB_NAVIGATION_PER_HOST_PER_MINUTE", "600")
    cfg = Settings(_env_file=None, development=True)
    assert cfg.navigation_min_interval_ms == 0 and cfg.navigation_per_host_per_minute == 600


@pytest.mark.parametrize(
    "key", ["token", "code", "state", "session", "accessToken", "X-Amz-Signature"]
)
def test_reader_secret_keys_keep_only_short_numeric_values(key):
    assert parse_qs(urlsplit(reader_safe_url(f"https://example.com/?{key}=123456")).query)[key] == [
        "123456"
    ]
    assert parse_qs(urlsplit(reader_safe_url(f"https://example.com/?{key}=1234567")).query)[
        key
    ] == ["[REDACTED]"]


PRESERVED = [
    "https://blog.naver.com/PostView.naver?blogId=someuser&logNo=223456789012",
    "https://n.news.naver.com/mnews/article/001/0014000000?sid=105",
    "https://news.naver.com/main/read.naver?oid=001&aid=0014000000&sid1=105",
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PLx0sYbCqOb8TBPRdmBHs5Iftvv9TPboYG",
    "https://www.google.com/search?q=home+server&start=10",
    "https://search.naver.com/search.naver?where=nexearch&query=%ED%99%88%EC%84%9C%EB%B2%84",
    "https://gall.dcinside.com/board/view/?id=programming&no=2940536",
    "https://example.com/?manage_code=MA01",
    "https://example.com/?sort_key=date",
    "https://example.com/?session_type=live",
    "https://example.com/?stateName=seoul",
]


@pytest.mark.parametrize("url", PRESERVED)
def test_reader_url_preserves_public_navigation(url):
    assert reader_safe_url(url) == url


@pytest.mark.parametrize(
    "key,value",
    [
        ("access_token", "abc"),
        ("code", "4/0AbCdEf123"),
        ("state", "abc"),
        ("X-Amz-Signature", "a" * 64),
        ("session", "abc123def456"),
        ("token", "abc"),
        ("v", "eyJabcdefghijk.abcdefghijk.abcdefghijk"),
        ("sig", "random" * 10),
        ("innocent", "a" * 40),
        ("q", "a" * 1001),
        ("q", "password=short"),
        ("accessToken", "abc"),
        ("user-session-id", "abc"),
        ("apiKey", "abc"),
        ("userPassword", "hunter2"),
        ("client_secret", "abc"),
    ],
)
def test_reader_url_redacts_secrets(key, value):
    cleaned = reader_safe_url(f"https://user:password@example.com:443/path?{key}={value}")
    parsed = urlsplit(cleaned)
    assert parsed.netloc == "example.com:443" and parsed.path == "/path"
    assert parse_qs(parsed.query)[key] == ["[REDACTED]"]


@pytest.mark.parametrize("fragment", ["article", "token=abc", "a" * 40, "route?custom=value"])
def test_reader_url_fragment_policy_unchanged(fragment):
    from cloud_browser.security import safe_url

    url = "https://example.com/#" + fragment
    assert urlsplit(reader_safe_url(url)).fragment == urlsplit(safe_url(url)).fragment


@pytest.mark.parametrize(
    "setting,value",
    [
        ("reader_idle_ttl", 59),
        ("reader_idle_ttl", 3601),
        ("reader_timeout", 4),
        ("reader_timeout", 41),
        ("reader_max_text_chars", 3999),
        ("reader_max_text_chars", 250001),
    ],
)
def test_reader_setting_bounds(setting, value):
    with pytest.raises(ValidationError):
        Settings(development=True, **{setting: value})


def test_reader_timeout_operator_ceiling():
    with pytest.raises(ValidationError, match="Reader timeout"):
        Settings(
            development=True, navigation_timeout=5, navigation_max_timeout=10, reader_timeout=11
        )
