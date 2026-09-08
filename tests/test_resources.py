from pathlib import Path
from types import SimpleNamespace

import pytest

from cloud_browser import resources

MIB = resources.MIB


def counters(**changes):
    values = dict(
        inactive_file=300 * MIB,
        file=400 * MIB,
        shmem=10 * MIB,
        file_dirty=30 * MIB,
        file_writeback=0,
        unevictable=0,
    )
    values.update(changes)
    return "\n".join(f"{key} {value}" for key, value in values.items())


@pytest.fixture
def cgroup(monkeypatch):
    files = {
        "memory.max": str(1024 * MIB),
        "memory.current": str(880 * MIB),
        "memory.stat": counters(),
    }
    original = Path.read_text
    root = Path("test-cgroup-memory")
    monkeypatch.setattr(resources, "CGROUP_ROOT", root)
    monkeypatch.setattr(
        resources.psutil, "virtual_memory", lambda: SimpleNamespace(available=824 * MIB)
    )

    def read(path, *args, **kwargs):
        if path.parent != root:
            return original(path, *args, **kwargs)
        value = files.get(path.name, FileNotFoundError())
        if isinstance(value, Exception):
            raise value
        return value() if callable(value) else value

    monkeypatch.setattr(Path, "read_text", read)
    return files


def test_warm_cache_retains_raw_usage_but_allows_observation(cgroup):
    state = resources.memory_state(256)
    assert state["cgroup_used_mb"] == 880
    assert state["cgroup_raw_headroom_mb"] == 144
    assert state["cgroup_inactive_file_mb"] == 300
    assert state["cgroup_reclaimable_estimate_mb"] == 270
    assert state["available_mb"] == state["cgroup_estimated_headroom_mb"] == 414
    assert state["reserve_mb"] == 256 and state["can_admit"] is True


def test_reported_pi_snapshot(cgroup):
    cgroup["memory.current"] = str(878 * MIB)
    cgroup["memory.stat"] = counters(
        inactive_file=322920448, file=427593728, shmem=11821056, file_dirty=31227904
    )
    state = resources.memory_state(256)
    assert state["cgroup_raw_headroom_mb"] == 146
    assert state["cgroup_reclaimable_estimate_mb"] == 278
    assert state["can_admit"] is True


@pytest.mark.parametrize(
    "changes,credit",
    [
        ({"inactive_file": 0}, 0),
        ({"file_dirty": 200 * MIB, "file_writeback": 60 * MIB, "unevictable": 40 * MIB}, 0),
        ({"shmem": 350 * MIB}, 20),
        ({"file": 100 * MIB, "shmem": 0}, 70),
        ({"shmem": 500 * MIB}, 0),
        ({"active_file": 900 * MIB, "anon": 900 * MIB, "slab_reclaimable": 900 * MIB}, 270),
    ],
)
def test_only_clean_inactive_file_receives_credit(cgroup, changes, credit):
    cgroup["memory.stat"] = counters(**changes)
    state = resources.memory_state(256)
    assert state["cgroup_reclaimable_estimate_mb"] == credit
    assert state["can_admit"] is (144 + credit >= 256)


def test_host_pressure_is_not_overridden_by_container_cache(cgroup, monkeypatch):
    monkeypatch.setattr(
        resources.psutil, "virtual_memory", lambda: SimpleNamespace(available=200 * MIB)
    )
    state = resources.memory_state(256)
    assert state["cgroup_estimated_headroom_mb"] == 414
    assert state["available_mb"] == 200 and state["can_admit"] is False


def test_admission_budget_is_in_addition_to_unchanged_reserve(cgroup):
    assert resources.memory_state(256, 158)["can_admit"] is True
    state = resources.memory_state(256, 159)
    assert state["admission_mb"] == 159 and state["can_admit"] is False


@pytest.mark.parametrize(
    "bad",
    [
        FileNotFoundError(),
        PermissionError(),
        "inactive_file 9999999999",
        counters(file_dirty=-1),
        counters() + "\ninactive_file 1",
        "malformed",
    ],
)
def test_unavailable_or_malformed_stats_get_no_cache_credit(cgroup, bad):
    cgroup["memory.stat"] = bad
    state = resources.memory_state(256)
    assert state["cgroup_used_mb"] == 880
    assert state["cgroup_reclaimable_estimate_mb"] == 0
    assert state["available_mb"] == 144 and state["can_admit"] is False
    assert state["accounting"] == "cgroup_v2_raw"


@pytest.mark.parametrize(
    "name,bad",
    [
        ("memory.max", "invalid"),
        ("memory.max", "-1"),
        ("memory.max", PermissionError()),
        ("memory.current", FileNotFoundError()),
        ("memory.current", PermissionError()),
        ("memory.current", "invalid"),
        ("memory.current", "-1"),
    ],
)
def test_failed_container_budget_is_not_host_fallback(cgroup, name, bad):
    cgroup[name] = bad
    state = resources.memory_state(256)
    assert state["accounting"] == "cgroup_v2_unreadable"
    assert state["available_mb"] == 0 and state["can_admit"] is False


@pytest.mark.parametrize("raw", ["max", FileNotFoundError()])
def test_native_or_unlimited_falls_back_to_host(cgroup, raw):
    cgroup["memory.max"] = raw
    state = resources.memory_state(256)
    assert state["available_mb"] == 824 and state["can_admit"] is True
    assert state["accounting"] == "host"
    assert state["cgroup_limit_mb"] is None


def test_sampling_uses_larger_current_charge(cgroup):
    readings = iter([str(880 * MIB), str(1000 * MIB)])
    cgroup["memory.current"] = lambda: next(readings)
    state = resources.memory_state(256, 96)
    assert state["cgroup_used_mb"] == 1000
    assert state["available_mb"] == 294 and state["can_admit"] is False


def test_cache_credit_is_bounded_by_sampled_charge_and_limit(cgroup):
    cgroup["memory.current"] = str(10 * MIB)
    cgroup["memory.stat"] = counters(inactive_file=10000 * MIB, file=10000 * MIB, file_dirty=0)
    state = resources.memory_state(256)
    assert state["cgroup_reclaimable_estimate_mb"] == 10
    assert state["cgroup_estimated_headroom_mb"] == 1024
    assert state["available_mb"] == 824


def test_zero_hard_limit_never_admits(cgroup):
    cgroup["memory.max"] = "0"
    state = resources.memory_state(256)
    assert state["available_mb"] == 0 and state["can_admit"] is False


def test_failed_second_sample_does_not_admit_from_earlier_cache_credit(cgroup):
    readings = iter([str(880 * MIB), "bad sample"])
    cgroup["memory.current"] = lambda: next(readings)
    state = resources.memory_state(256)
    assert state["accounting"] == "cgroup_v2_unreadable"
    assert state["cgroup_reclaimable_estimate_mb"] == 0
    assert state["available_mb"] == 0 and state["can_admit"] is False


def test_admission_compares_bytes_before_rounding(cgroup, monkeypatch):
    monkeypatch.setattr(
        resources.psutil, "virtual_memory", lambda: SimpleNamespace(available=256 * MIB - 1)
    )
    assert resources.memory_state(256)["can_admit"] is False


async def test_observe_admission_uses_cache_estimate_without_bypassing_pressure(service, cgroup):
    opened = await service.call("open")
    assert opened["status"] == "ok"
    args = {key: opened[key] for key in ("session_id", "tab_id")}
    assert (await service.call("observe", **args))["status"] == "ok"
    cgroup["memory.stat"] = counters(inactive_file=0)
    calls = len(service.worker.calls)
    blocked = await service.call("observe", **args)
    assert blocked["error"]["code"] == "RESOURCE_PRESSURE"
    assert len(service.worker.calls) == calls
    # Pressure does not prevent explicit cleanup or silently expire the tab.
    assert (await service.call("list_tabs", session_id=opened["session_id"]))["tabs"]
    assert (await service.call("close", session_id=opened["session_id"], scope="session"))[
        "status"
    ] == "ok"
