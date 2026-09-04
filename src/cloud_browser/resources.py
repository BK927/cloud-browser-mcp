from pathlib import Path

import psutil


def memory_state(reserve_mb: int, admission_mb: int = 0) -> dict:
    host = psutil.virtual_memory()
    available = host.available
    limit = usage = None
    # cgroup v2 is the Debian 13/Docker deployment target. Fall back on hosts.
    try:
        raw = Path("/sys/fs/cgroup/memory.max").read_text().strip()
        if raw != "max":
            limit = int(raw)
            usage = int(Path("/sys/fs/cgroup/memory.current").read_text())
            available = min(available, max(0, limit - usage))
    except (OSError, ValueError):
        pass
    return {
        "host_available_mb": host.available // 1048576,
        "available_mb": available // 1048576,
        "cgroup_limit_mb": limit // 1048576 if limit is not None else None,
        "cgroup_used_mb": usage // 1048576 if usage is not None else None,
        "reserve_mb": reserve_mb,
        "can_admit": available >= (reserve_mb + admission_mb) * 1048576,
    }
