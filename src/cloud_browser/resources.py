from pathlib import Path

import psutil

MIB = 1048576
CGROUP_ROOT = Path("/sys/fs/cgroup")


def _nonnegative(raw: str) -> int:
    value = int(raw)
    if value < 0:
        raise ValueError("Negative memory counter")
    return value


def _cache_estimate(usage: int) -> tuple[int, int, str]:
    """Estimate reclaimable clean, inactive file pages, never all file cache.

    cgroup counters are sampled, not an allocation guarantee. Subtracting all
    dirty/writeback/unevictable bytes may undercount (the sets overlap), which is
    intentional. Anonymous, active, shared memory and slab receive no credit.
    """
    required = {"inactive_file", "file", "shmem", "file_dirty", "file_writeback", "unevictable"}
    try:
        counters = {}
        for line in (CGROUP_ROOT / "memory.stat").read_text().splitlines():
            key, raw = line.split()
            if key in required:
                if key in counters:
                    raise ValueError("Duplicate memory counter")
                counters[key] = _nonnegative(raw)
        if not required <= counters.keys():
            raise ValueError("Incomplete cache accounting")
    except (OSError, ValueError):
        return 0, 0, "unavailable"
    inactive = min(counters["inactive_file"], usage)
    eligible = min(inactive, max(0, counters["file"] - counters["shmem"]))
    excluded = counters["file_dirty"] + counters["file_writeback"] + counters["unevictable"]
    return inactive, max(0, eligible - excluded), "ok"


def memory_state(reserve_mb: int, admission_mb: int = 0) -> dict:
    host = psutil.virtual_memory()
    host_available = max(0, host.available)
    available = host_available
    limit = usage = None
    raw_headroom = estimated_headroom = None
    inactive = reclaimable = 0
    accounting = "host"
    stat_status = "not_read"
    # Missing v2 memory controller is the native/non-Linux fallback, not the same
    # as failing to read an existing container budget.
    try:
        raw = (CGROUP_ROOT / "memory.max").read_text().strip()
    except FileNotFoundError:
        raw = "max"
    except OSError:
        raw = "unreadable"
    try:
        if raw != "max":
            limit = _nonnegative(raw)
            usage = _nonnegative((CGROUP_ROOT / "memory.current").read_text())
            inactive, reclaimable, stat_status = _cache_estimate(usage)
            # Use the larger charge around the stat read; allocations during the
            # sample must not make our previous headroom look more generous.
            usage = max(usage, _nonnegative((CGROUP_ROOT / "memory.current").read_text()))
            raw_headroom = max(0, limit - usage)
            estimated_headroom = max(0, limit - (usage - reclaimable))
            available = min(host_available, estimated_headroom)
            accounting = "cgroup_v2_clean_inactive_file" if stat_status == "ok" else "cgroup_v2_raw"
    except (OSError, ValueError):
        # A known/invalid container budget must not silently fall back to host RAM.
        available = 0
        reclaimable = 0
        estimated_headroom = 0
        accounting = "cgroup_v2_unreadable"
    return {
        "host_available_mb": host_available // MIB,
        "available_mb": available // MIB,
        # Preserve the original raw counter, not Docker CLI's cache-subtracted value.
        "cgroup_limit_mb": limit // MIB if limit is not None else None,
        "cgroup_used_mb": usage // MIB if usage is not None else None,
        "cgroup_raw_headroom_mb": raw_headroom // MIB if raw_headroom is not None else None,
        "cgroup_inactive_file_mb": inactive // MIB,
        "cgroup_reclaimable_estimate_mb": reclaimable // MIB,
        "cgroup_estimated_headroom_mb": estimated_headroom // MIB
        if estimated_headroom is not None
        else None,
        "accounting": accounting,
        "cgroup_stat_status": stat_status,
        "reserve_mb": reserve_mb,
        "admission_mb": admission_mb,
        "can_admit": available >= (reserve_mb + admission_mb) * MIB,
    }
