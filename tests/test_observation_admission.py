import pytest

from cloud_browser.models import BrowserError


def headroom(available):
    return {
        "available_mb": available,
        "host_available_mb": 1024,
        "can_admit": True,
        "memory_pressure": {"some": 0, "full": 0},
    }


@pytest.mark.parametrize("mode", [None, "auto", "semantic", "interactive"])
async def test_pressure_preserves_requested_observation_and_fresh_text(service, monkeypatch, mode):
    opened = await service.call("open")
    args = {key: opened[key] for key in ("session_id", "tab_id")}
    monkeypatch.setattr(service, "resources", lambda admission=0: headroom(200))
    service.worker.sessions[args["session_id"]][args["tab_id"]]["revision"] = 2
    original = service.worker.call

    async def fresh_observation(method, **options):
        result = await original(method, **options)
        if method == "observe":
            result["observation"]["semantic_snapshot"] = (
                "Public article after DOM update"
                if options.get("mode", "auto") in ("auto", "semantic")
                else ""
            )
        return result

    monkeypatch.setattr(service.worker, "call", fresh_observation)
    options = {"max_chars": 12000, "query": {"scope": "article"}}
    if mode:
        options["mode"] = mode
    observed = await service.call("observe", **args, **options)

    assert observed["status"] == "ok"
    assert observed["revision"] == 2
    assert observed["observation"]["resource_limited"] is True
    if mode != "interactive":
        assert observed["observation"]["semantic_snapshot"] == "Public article after DOM update"
    assert observed["observation"]["interactive_snapshot"]
    method, dispatched = service.worker.calls[-1]
    assert method == "observe"
    assert dispatched.get("mode", "auto") == (mode or "auto")
    assert dispatched["query"] == options["query"]
    assert dispatched["max_chars"] == 4000 and dispatched["lightweight"] is True
    assert "capture and broad scanning omitted" not in " ".join(observed["notices"])


async def test_possible_full_page_image_does_not_reduce_normal_text_budget(service, monkeypatch):
    opened = await service.call("open")
    args = {key: opened[key] for key in ("session_id", "tab_id")}
    service.cfg.memory_policy = "strict"
    monkeypatch.setattr(service, "resources", lambda admission=0: headroom(320))

    observed = await service.call("observe", **args, mode="auto", full_page=True, max_chars=12000)

    assert observed["status"] == "ok"
    assert observed["observation"]["semantic_snapshot"]
    assert "resource_limited" not in observed["observation"]
    _, dispatched = service.worker.calls[-1]
    assert dispatched["mode"] == "auto" and dispatched["full_page"] is True
    assert dispatched["max_chars"] == 12000 and not dispatched.get("lightweight", False)


async def test_visual_capture_failure_is_decided_and_reported_by_worker(service, monkeypatch):
    opened = await service.call("open")
    args = {key: opened[key] for key in ("session_id", "tab_id")}
    monkeypatch.setattr(service, "resources", lambda admission=0: headroom(200))
    original = service.worker.call

    async def actual_capture_gate(method, **options):
        result = await original(method, **options)
        if method == "observe":
            raise BrowserError(
                "RESOURCE_PRESSURE", "Observed image requires more than the capture budget"
            )
        return result

    monkeypatch.setattr(service.worker, "call", actual_capture_gate)
    observed = await service.call("observe", **args, mode="visual", full_page=True)

    assert observed["error"]["code"] == "RESOURCE_PRESSURE"
    assert observed["error"]["message"] == "Observed image requires more than the capture budget"
    method, dispatched = service.worker.calls[-1]
    assert method == "observe" and dispatched["mode"] == "visual"
    assert dispatched["full_page"] is True
    assert dispatched["lightweight"] is True
    assert (await service.call("close", session_id=args["session_id"], scope="session"))[
        "status"
    ] == "ok"
