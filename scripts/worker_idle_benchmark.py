"""Isolated real-worker lifecycle comparison; no production listeners or auth.

Run these identical bytes with PYTHONPATH selecting baseline/candidate source.
Only a temporary about:blank profile and its owned worker are created. Metrics
describe that worker tree, not total service RAM; RSS sums double-count sharing.
"""

import argparse
import asyncio
import hashlib
import json
import os
import platform
import tempfile
import time
from pathlib import Path

import psutil

from cloud_browser.config import Settings
from cloud_browser.service import BrowserService
from cloud_browser.store import Store


def sample(worker, known):
    values = []
    errors = 0
    if worker.process is not None:
        try:
            root = psutil.Process(worker.process.pid)
            for process in [root, *root.children(recursive=True)]:
                known[(process.pid, process.create_time())] = process
        except psutil.NoSuchProcess:
            pass
        except psutil.Error:
            errors += 1
    # Keep psutil's creation-time-bound handles after Worker clears its handle.
    # Check the actual owned processes, including any surviving descendants.
    for process in known.values():
        try:
            if not process.is_running():
                continue
            memory = process.memory_full_info()
            values.append(
                {
                    "rss": memory.rss,
                    "uss": getattr(memory, "uss", None),
                    "pss": getattr(memory, "pss", None),
                }
            )
        except psutil.NoSuchProcess:
            continue
        except psutil.Error:
            errors += 1
    return {
        "process_count": len(values),
        "inspection_errors": errors,
        **{
            key + "_bytes": sum(row[key] for row in values)
            if not errors
            and (key != "pss" or os.name != "nt")
            and all(row[key] is not None for row in values)
            else None
            for key in ("rss", "uss", "pss")
        },
    }


async def measure(args):
    temporary = tempfile.TemporaryDirectory(prefix="cb-idle-benchmark-")
    known = {}
    service = store = None
    clean = False
    try:
        cfg = Settings(
            _env_file=None,
            development=True,
            data_dir=Path(temporary.name),
            chromium_path=args.chromium,
            browser_proxy="",
            headless=True,
            managed_display=False,
            session_sweep_interval=1,
            worker_idle_timeout=args.grace_seconds,
        )
        store = Store(cfg.data_dir / "state.sqlite3")
        service = BrowserService(cfg, store)
        service.start()
        records = []
        try:
            for cycle in range(2):
                start = time.perf_counter()
                opened = await service.call("open")
                if opened["status"] != "ok":
                    raise RuntimeError("Isolated open failed")
                open_ms = (time.perf_counter() - start) * 1000
                await asyncio.sleep(0.3)
                active = sample(service.worker, known)
                closed = await service.call(
                    "close", session_id=opened["session_id"], scope="session"
                )
                if closed["status"] != "ok":
                    raise RuntimeError("Isolated close failed")
                # Browser shutdown may be asynchronous; use an identical settling
                # window for both sources before the grace-period sample.
                await asyncio.sleep(0.5)
                before = sample(service.worker, known)
                await asyncio.sleep(args.grace_seconds + 1.5)
                after = sample(service.worker, known)
                records.append(
                    {
                        "cycle": cycle + 1,
                        "open_ms": round(open_ms, 3),
                        "active": active,
                        "closed_before_grace": before,
                        "closed_after_grace": after,
                    }
                )
        finally:
            await service.shutdown()
            store.close()
            final = sample(service.worker, known)
            clean = final["process_count"] == 0 and final["inspection_errors"] == 0
        from cloud_browser import service as service_module

        return {
            "label": args.label,
            "platform": platform.platform(),
            "python": platform.python_version(),
            "grace_seconds": args.grace_seconds,
            "scope": "isolated Python browser worker and descendants, headless about:blank",
            "rss_warning": "Summed RSS double-counts shared pages; prefer USS/PSS where available",
            "samples": records,
            "cleanup_complete": clean,
            "final_process_check": final,
            "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "source_sha256": {
                name: hashlib.sha256(
                    Path(service_module.__file__).with_name(name).read_bytes()
                ).hexdigest()
                for name in ("config.py", "service.py", "worker.py", "drission.py", "snapshot.js")
            },
        }
    finally:
        if clean:
            temporary.cleanup()
        else:
            # Do not remove a profile when process cleanup is unverified.
            temporary._finalizer.detach()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chromium", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--grace-seconds", type=float, default=2)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.grace_seconds <= 60:
        parser.error("grace-seconds must be 1..60")
    for key in list(os.environ):
        if key.startswith("CB_"):
            os.environ.pop(key)
    result = asyncio.run(measure(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result))
    if not result["cleanup_complete"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
