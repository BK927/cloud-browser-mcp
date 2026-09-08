#!/usr/bin/env python3
"""Read-only Linux cgroup/PSS sampler. JSONL contains no argv, URLs or environment.

Run outside the measured group as root to read cross-UID smaps_rollup. Specify
one PID for each exact browser/ingress/egress service, never a host-wide wildcard.
"""

import argparse
import json
import os
import time
from pathlib import Path

ROOT = Path("/sys/fs/cgroup")


def counters(path, *, kib=False):
    values = {}
    for line in path.read_text().splitlines():
        parts = line.replace(":", "").split()
        if len(parts) >= 2 and parts[1].isdigit():
            values[parts[0]] = int(parts[1]) * (1024 if kib else 1)
    return values


def group_for(pid):
    raw = next(
        line[3:]
        for line in Path(f"/proc/{pid}/cgroup").read_text().splitlines()
        if line.startswith("0::")
    )
    path = ROOT / raw.lstrip("/")
    if (
        ".." in Path(raw).parts
        or path == ROOT
        or not path.is_dir()
        or not path.resolve().is_relative_to(ROOT)
    ):
        raise RuntimeError(
            "Refusing host-root/unresolved cgroup; choose the service's actual host PID"
        )
    return path


def sample_group(path):
    result = {"cgroup": str(path), "processes": [], "unreadable_pids": []}
    try:
        result["memory_current"] = int((path / "memory.current").read_text())
        result["memory_max"] = (path / "memory.max").read_text().strip()
        result["memory_swap_current"] = int((path / "memory.swap.current").read_text())
        result["memory_stat"] = counters(path / "memory.stat")
        result["memory_events"] = counters(path / "memory.events")
        groups = [path]
        for child in path.rglob("cgroup.procs"):
            if child.parent != path:
                groups.append(child.parent)
            if len(groups) > 64:
                raise RuntimeError("Group traversal budget exceeded")
        pids = {int(p) for group in groups for p in (group / "cgroup.procs").read_text().split()}
        if len(pids) > 512:
            raise RuntimeError("Process sampling budget exceeded")
        for pid in sorted(pids):
            try:
                proc = Path(f"/proc/{pid}")
                # Check membership around the read, so PID reuse cannot silently
                # report a different service's process.
                before = (proc / "stat").read_text().rsplit(")", 1)[1].split()[19]
                if not group_for(pid).is_relative_to(path):
                    continue
                memory = counters(proc / "smaps_rollup", kib=True)
                name = (proc / "comm").read_text().strip()[:32]
                after = (proc / "stat").read_text().rsplit(")", 1)[1].split()[19]
                if before == after and group_for(pid).is_relative_to(path):
                    result["processes"].append(
                        {
                            "pid": pid,
                            "name": name,
                            "rss": memory.get("Rss"),
                            "pss": memory.get("Pss"),
                            "swap": memory.get("Swap"),
                        }
                    )
            except (OSError, ValueError, StopIteration):
                result["unreadable_pids"].append(pid)
        result["rss_sum"] = sum(p["rss"] or 0 for p in result["processes"])
        result["pss_sum"] = sum(p["pss"] or 0 for p in result["processes"])
    except OSError:
        result["state"] = "unavailable_or_terminated"
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", type=int, action="append", required=True)
    parser.add_argument("--duration", type=int, choices=range(1, 3601), default=300)
    parser.add_argument("--interval", type=float, default=1)
    parser.add_argument("--label", required=True)
    args = parser.parse_args()
    if os.name != "posix" or not 0.5 <= args.interval <= 10 or len(args.pid) > 4:
        parser.error(
            "Linux, 0.5..10 second interval, and at most four dedicated service PIDs are required"
        )
    groups = list(dict.fromkeys(group_for(pid) for pid in args.pid))
    deadline = time.monotonic() + args.duration
    while time.monotonic() < deadline:
        host = counters(Path("/proc/meminfo"), kib=True)
        print(
            json.dumps(
                {
                    "unix_time": time.time(),
                    "label": args.label,
                    "host": {
                        key: host.get(key)
                        for key in ("MemTotal", "MemAvailable", "Cached", "SwapTotal", "SwapFree")
                    },
                    "groups": [sample_group(path) for path in groups],
                }
            ),
            flush=True,
        )
        time.sleep(min(args.interval, max(0, deadline - time.monotonic())))


if __name__ == "__main__":
    main()
