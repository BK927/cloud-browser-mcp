#!/usr/bin/env python3
"""Bounded, operator-run real Chromium observation benchmark.

Run this SAME file with PYTHONPATH pointing at each source tree under comparison.
Use at least three independent processes per variant, alternating run order. This
measures the adapter, not network/MCP transport or a production security boundary.
Only a generated loopback fixture and a new temporary profile are accessed.
No DOM, screenshot, URL, credentials, or input values are retained in the report.
"""

import argparse
import contextlib
import hashlib
import http.server
import json
import os
import platform
import statistics
import sys
import tempfile
import threading
import time
from collections import Counter, defaultdict
from importlib.metadata import version
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

import psutil

FIXTURE_VERSION = 1
MEMORY_NOTE = (
    "RSS sum includes shared pages more than once and is NOT physical memory usage. "
    "USS sum counts private pages; PSS sum proportionally attributes shared pages. "
    "Unavailable USS/PSS remains null, never falls back to RSS. Samples can miss peaks."
)


def fixture_pages():
    controls = "".join(
        f'<button type="button">Fixture item {index:02d}</button>' for index in range(48)
    )
    paragraphs = "".join(
        f"<p>Section {index:04d}. The observatory keeps a catalogue of clear skies, "
        "measured distances, quiet gardens, and ordinary walking routes. "
        "This deterministic article contains no external resources.</p>"
        for index in range(1200)
    )
    main = f"""<!doctype html><html lang="en"><meta charset="utf-8">
<title>Deterministic observation fixture</title>
<style>body{{font:14px sans-serif;margin:16px}}button{{margin:2px;padding:4px}}
#controls{{max-width:900px}}iframe{{width:400px;height:100px}}p{{max-width:900px}}</style>
<h1>Observation catalogue</h1>
<section id="editor"><label>Draft text <input id="draft" aria-label="Draft text"></label>
<label><input id="toggle" type="checkbox">Show details</label></section>
<section id="controls">{controls}</section>
<iframe src="/frame.html" title="Fixture panel"></iframe>
<article>{paragraphs}</article></html>"""
    frame = """<!doctype html><html lang="en"><meta charset="utf-8">
<title>Fixture panel</title><label>Frame filter <input aria-label="Frame filter"></label>
<button type="button">Frame option</button><p>A stable local frame.</p></html>"""
    return {"/fixture.html": main.encode(), "/frame.html": frame.encode()}


@contextlib.contextmanager
def fixture_server():
    pages = fixture_pages()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = pages.get(urlsplit(self.path).path)
            self.send_response(200 if body is not None else 404)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header(
                "Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'"
            )
            self.send_header("Content-Length", str(len(body or b"")))
            self.end_headers()
            self.wfile.write(body or b"")

        def log_message(self, *_args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def memory_totals(processes):
    """Never silently substitute RSS for USS/PSS or sum individual peak values."""
    result = {"process_count": len(processes)}
    for field in ("rss", "uss", "pss"):
        values = [item.get(field + "_bytes") for item in processes]
        result[field + "_sum_bytes"] = (
            sum(values) if values and all(value is not None for value in values) else None
        )
    return result


class ProcessSampler:
    """Sample only this benchmark process and its descendants, with PID reuse checks."""

    def __init__(self, interval=0.5):
        self.root = psutil.Process()
        self.interval = interval
        self.started = time.perf_counter()
        self.phase = "setup"
        self.known = {}
        self.samples = []
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.sampling_seconds = 0.0
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.sample()
        self.thread.start()

    def _run(self):
        while not self.stop_event.wait(self.interval):
            self.sample()

    def sample(self):
        with self.lock:
            started = time.perf_counter()
            try:
                live = [self.root, *self.root.children(recursive=True)]
            except psutil.Error:
                live = [self.root]
            for process in live:
                try:
                    key = (process.pid, process.create_time())
                    if key not in self.known:
                        cpu = process.cpu_times()
                        # New browser children were created during this trial: their
                        # CPU starts at zero. The already-running harness needs a baseline.
                        baseline = cpu.user + cpu.system if process.pid == self.root.pid else 0
                        role = "harness" if process.pid == self.root.pid else "browser_child"
                        self.known[key] = {
                            "process": process,
                            "role": role,
                            "cpu_baseline": baseline,
                            "cpu_last": baseline,
                            "rss_peak_bytes": 0,
                            "uss_peak_bytes": None,
                            "pss_peak_bytes": None,
                        }
                except psutil.Error:
                    continue
            records = []
            for (pid, created), item in self.known.items():
                process = item["process"]
                try:
                    if process.create_time() != created or not process.is_running():
                        continue
                    cpu = process.cpu_times()
                    item["cpu_last"] = cpu.user + cpu.system
                    memory = process.memory_info()
                    try:
                        full = process.memory_full_info()
                    except (psutil.Error, NotImplementedError):
                        full = None
                    record = {"pid": pid, "role": item["role"], "rss_bytes": memory.rss}
                    for field in ("uss", "pss"):
                        record[field + "_bytes"] = getattr(full, field, None)
                    record["cpu_seconds"] = max(0, item["cpu_last"] - item["cpu_baseline"])
                    for field in ("rss", "uss", "pss"):
                        value = record[field + "_bytes"]
                        if value is not None:
                            peak_key = field + "_peak_bytes"
                            item[peak_key] = max(item[peak_key] or 0, value)
                    records.append(record)
                except psutil.Error:
                    continue
            self.samples.append(
                {
                    "elapsed_seconds": time.perf_counter() - self.started,
                    "phase": self.phase,
                    "processes": records,
                    **memory_totals(records),
                }
            )
            self.sampling_seconds += time.perf_counter() - started

    def cpu_totals(self):
        with self.lock:
            values = defaultdict(float)
            for item in self.known.values():
                values[item["role"]] += max(0, item["cpu_last"] - item["cpu_baseline"])
            return dict(values)

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=5)
        self.sample()

    def report(self):
        with self.lock:
            per_process = [
                {
                    "pid": pid,
                    "role": item["role"],
                    "cpu_seconds": max(0, item["cpu_last"] - item["cpu_baseline"]),
                    **{
                        field + "_peak_bytes": item[field + "_peak_bytes"]
                        for field in ("rss", "uss", "pss")
                    },
                }
                for (pid, _created), item in self.known.items()
            ]
            peaks = {}
            for field in ("rss", "uss", "pss"):
                values = [
                    s[field + "_sum_bytes"]
                    for s in self.samples
                    if s[field + "_sum_bytes"] is not None
                ]
                peaks[field + "_sum_peak_bytes"] = max(values) if values else None
            return {
                "accounting": MEMORY_NOTE,
                "interval_seconds": self.interval,
                "sampler_wall_seconds": self.sampling_seconds,
                "cpu_note": "Observed CPU is a lower bound for children exiting between samples; harness CPU includes instrumentation and fixture serving.",
                "per_process": per_process,
                "samples": self.samples,
                **peaks,
            }


class CDPMeter:
    """Observe existing Driver calls without issuing additional browser requests."""

    def __init__(self, enabled=True):
        self.enabled = enabled
        self.methods = Counter()
        self.snapshot_count = 0
        self.snapshot_payload_bytes = 0
        self.instrumentation_seconds = 0.0
        self.lock = threading.Lock()
        self.patcher = None

    def __enter__(self):
        if not self.enabled:
            return self
        from DrissionPage._base.driver import Driver

        original = Driver._send

        def measured(driver, message, *args, **kwargs):
            method = message.get("method", "unknown")
            result = original(driver, message, *args, **kwargs)
            started = time.perf_counter()
            snapshot = None
            if method == "Runtime.callFunctionOn":
                value = result.get("result", {}).get("result", {}).get("value")
                # Match the snapshot shape, not an implementation's JS spelling.
                if isinstance(value, dict) and "nodes" in value and "semantic_text" in value:
                    snapshot = len(
                        json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
                    )
            with self.lock:
                self.methods[method] += 1
                if snapshot is not None:
                    self.snapshot_count += 1
                    self.snapshot_payload_bytes += snapshot
                self.instrumentation_seconds += time.perf_counter() - started
            return result

        self.patcher = patch.object(Driver, "_send", measured)
        self.patcher.start()
        return self

    def __exit__(self, *_args):
        if self.patcher is not None:
            self.patcher.stop()

    def snapshot(self):
        with self.lock:
            return {
                "calls": sum(self.methods.values()),
                "methods": dict(self.methods),
                "snapshot_count": self.snapshot_count,
                "snapshot_payload_bytes": self.snapshot_payload_bytes,
                "instrumentation_seconds": self.instrumentation_seconds,
            }


def meter_delta(before, after):
    return {
        key: after[key] - before[key]
        for key in ("calls", "snapshot_count", "snapshot_payload_bytes", "instrumentation_seconds")
    } | {
        "methods": {
            key: after["methods"].get(key, 0) - before["methods"].get(key, 0)
            for key in sorted(after["methods"])
            if after["methods"].get(key, 0) != before["methods"].get(key, 0)
        }
    }


def aggregate(records):
    grouped = defaultdict(list)
    for record in records:
        if record["measured"]:
            grouped[record["phase"]].append(record)
    result = {}
    for phase, items in sorted(grouped.items()):
        wall = [item["wall_seconds"] for item in items]
        result[phase] = {
            "count": len(items),
            "wall_total_seconds": sum(wall),
            "wall_median_seconds": statistics.median(wall),
            "wall_min_seconds": min(wall),
            "wall_max_seconds": max(wall),
            "cdp_calls": sum(item["cdp"]["calls"] for item in items),
            "snapshot_payload_bytes": sum(item["cdp"]["snapshot_payload_bytes"] for item in items),
            "harness_cpu_seconds": sum(item["cpu_seconds"].get("harness", 0) for item in items),
            "browser_cpu_seconds": sum(
                item["cpu_seconds"].get("browser_child", 0) for item in items
            ),
        }
    return result


def find_node(observation, name):
    nodes = [
        json.loads(line)
        for line in observation["observation"]["interactive_snapshot"].splitlines()
        if line
    ]
    return next(item for item in nodes if item.get("name") == name)


def run(args):
    from cloud_browser import drission
    from cloud_browser.config import Settings
    from cloud_browser.models import BrowserError

    report = {
        "schema_version": 1,
        "cdp_meter_enabled": not args.no_cdp_meter,
        "label": args.label,
        "result": "incomplete",
        "fixture_version": FIXTURE_VERSION,
        "iterations": args.iterations,
        "warmup_iterations": args.warmup,
        "records": [],
        "cleanup": {},
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "drissionpage": version("DrissionPage"),
            "psutil": psutil.__version__,
        },
        "source_sha256": {
            name: hashlib.sha256(Path(drission.__file__).with_name(name).read_bytes()).hexdigest()
            for name in ("drission.py", "snapshot.js", "config.py", "service.py")
        },
        "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "measurement_notes": [
            "Same instrumented harness is required for baseline and candidate; run at least three independent trials, alternating order.",
            "Direct adapter workload excludes MCP/auth/network transport and worker startup. It is not a production isolation test.",
            "CDP counts cover DrissionPage Driver._send, including background commands. Snapshot bytes are compact UTF-8 JSON payload estimates, not exact WebSocket wire bytes.",
            "When cdp_meter_enabled is false, no Driver wrapper or payload serialization is installed; all CDP numbers are zero placeholders for disabled measurement, not zero browser traffic.",
            "The recorded instrumenting overhead and process sampler are included in CPU/wall measurements. Short-lived processes and sub-interval memory peaks can be missed.",
            "url_wait_timeout is an expected 700 ms unsuccessful URL wait. Compare its CPU/CDP work; the specified timeout is not expected to become shorter.",
            MEMORY_NOTE,
        ],
    }
    sampler = ProcessSampler(args.sample_interval)
    sampler.start()
    started = time.perf_counter()
    adapter = None
    temporary = tempfile.TemporaryDirectory(prefix="cb-observation-benchmark-")

    try:
        with fixture_server() as base, CDPMeter(enabled=not args.no_cdp_meter) as meter:

            def fixture_only(url, *, dns_proxy=None):
                parsed = urlsplit(url)
                if (
                    dns_proxy is not None
                    or parsed.scheme != "http"
                    or parsed.netloc != urlsplit(base).netloc
                    or parsed.path not in fixture_pages()
                    or parsed.query
                    or parsed.fragment
                ):
                    raise BrowserError("INVALID_URL", "Benchmark fixture only")

            def measure(phase, iteration, measured, operation):
                if time.perf_counter() - started > args.time_budget:
                    raise RuntimeError("Benchmark time budget exceeded")
                sampler.phase = phase
                sampler.sample()
                cpu_before = sampler.cpu_totals()
                before = meter.snapshot()
                begin = time.perf_counter()
                value = operation()
                elapsed = time.perf_counter() - begin
                after = meter.snapshot()
                sampler.sample()
                cpu_after = sampler.cpu_totals()
                report["records"].append(
                    {
                        "phase": phase,
                        "iteration": iteration,
                        "measured": measured,
                        "wall_seconds": elapsed,
                        "cdp": meter_delta(before, after),
                        "cpu_seconds": {
                            key: max(0, cpu_after.get(key, 0) - cpu_before.get(key, 0))
                            for key in cpu_after
                        },
                    }
                )
                return value

            # Same test-only exact fixture exception as the repository's browser
            # tests. Production URL validation and all adapter safety logic stay unchanged.
            with patch.object(drission, "validate_url", fixture_only):
                adapter = drission.DrissionAdapter(
                    Settings(
                        _env_file=None,
                        development=True,
                        data_dir=Path(temporary.name),
                        headless=True,
                        chromium_path=str(args.chromium),
                        browser_proxy="",
                        managed_display=False,
                        browser_cleanup_command="",
                        approval_policy="balanced",
                        webmcp_testing=False,
                    )
                )
                opened = measure(
                    "open", -1, False, lambda: adapter.open("ses_benchmark", base + "/fixture.html")
                )
                sid, tid = "ses_benchmark", opened["tab_id"]
                browser = adapter.sessions[sid]["browser"]
                report["environment"]["browser_version"] = browser.version
                adapter.configure(
                    sid,
                    tid,
                    {
                        "viewport_width": 1024,
                        "viewport_height": 768,
                        "max_chars": 30000,
                        "screenshot_quality": 75,
                        "wait_ms": 100,
                    },
                )
                # Readiness is required once, before warmup. No fixed long sleep.
                ready = adapter.wait(
                    sid,
                    tid,
                    {"type": "element", "query": {"name": "Frame filter"}, "state": "visible"},
                    5000,
                )
                if not ready["wait"]["matched"]:
                    raise RuntimeError("Fixture frame did not become ready")

                for iteration in range(args.warmup + args.iterations):
                    measured = iteration >= args.warmup

                    def call(phase, operation, iteration=iteration, measured=measured):
                        return measure(phase, iteration, measured, operation)

                    seen = call(
                        "interactive",
                        lambda: adapter.observe(sid, tid, mode="interactive", max_chars=30000),
                    )
                    find_node(seen, "Draft text")
                    find_node(seen, "Show details")
                    if seen["observation"].get("frame_reading_truncated"):
                        raise RuntimeError("Fixture frame observation incomplete")
                    targeted = call(
                        "targeted",
                        lambda: adapter.observe(
                            sid,
                            tid,
                            mode="interactive",
                            max_chars=30000,
                            query={"selector": "#draft"},
                        ),
                    )
                    target = find_node(targeted, "Draft text")
                    semantic = call(
                        "semantic",
                        lambda: adapter.observe(sid, tid, mode="semantic", max_chars=30000),
                    )
                    if not semantic["observation"].get("semantic_snapshot"):
                        raise RuntimeError("Semantic observation is empty")
                    visual = call("visual", lambda: adapter.observe(sid, tid, mode="visual"))
                    if not visual.get("_image", {}).get("data"):
                        raise RuntimeError("Visual observation is missing")

                    waiting = call(
                        "url_wait_timeout",
                        lambda: adapter.wait(
                            sid, tid, {"type": "url", "value": base + "/never-target"}, 700
                        ),
                    )
                    if waiting["wait"]["matched"] or not waiting["wait"]["timed_out"]:
                        raise RuntimeError("Expected bounded URL wait did not time out")

                    def safe_action(name, action, expected_key, expected_value):
                        observed = adapter.observe(
                            sid, tid, mode="interactive", query={"name": name}
                        )
                        node = find_node(observed, name)
                        bound = action | {"node_id": node["node_id"]}
                        prepared = adapter.prepare(sid, tid, observed["revision"], bound)
                        if prepared.get("requires_confirmation"):
                            raise RuntimeError(
                                "Synthetic local action unexpectedly requires confirmation"
                            )
                        result = adapter.act(sid, tid, prepared["revision"], bound)
                        if not result["action_result"]["performed"]:
                            raise RuntimeError("Synthetic action was not performed")
                        after = adapter.observe(sid, tid, mode="interactive", query={"name": name})
                        if find_node(after, name).get(expected_key) != expected_value:
                            raise RuntimeError("Synthetic action result did not match")
                        return result

                    checked = iteration % 2 == 0
                    call(
                        "check_protocol",
                        lambda checked=checked: safe_action(
                            "Show details",
                            {"type": "check", "checked": checked},
                            "checked",
                            checked,
                        ),
                    )
                    value = f"fixture draft {iteration:03d}"
                    call(
                        "fill_protocol",
                        lambda value=value: safe_action(
                            "Draft text", {"type": "fill", "text": value}, "value", value
                        ),
                    )
                    # Retain only numeric aggregates, never observation or screenshot content.
                    del seen, targeted, target, semantic, visual, waiting
                report["cdp_total"] = meter.snapshot()
                report["result"] = "completed"
    except BaseException as exc:
        report["result"] = "failed"
        report["error"] = {"type": type(exc).__name__, "code": getattr(exc, "code", None)}
        # Exception strings can contain browser content; deliberately do not persist them.
    finally:
        sampler.phase = "cleanup"
        sampler.sample()
        if adapter is not None:
            try:
                adapter.shutdown()
            except Exception as exc:
                report["cleanup"]["shutdown_error_type"] = type(exc).__name__
        # No foreign processes are touched. Report surviving descendants instead
        # of using host-wide process termination or deleting a live profile.
        try:
            children = sampler.root.children(recursive=True)
            _, alive = psutil.wait_procs(children, timeout=5)
            report["cleanup"]["remaining_child_pids"] = [process.pid for process in alive]
        except psutil.Error:
            report["cleanup"]["remaining_child_pids"] = None
        sampler.stop()
        if report["cleanup"].get("remaining_child_pids") == []:
            try:
                temporary.cleanup()
                report["cleanup"]["temporary_profile_removed"] = True
            except OSError as exc:
                report["cleanup"]["temporary_profile_removed"] = False
                report["cleanup"]["profile_cleanup_error_type"] = type(exc).__name__
        else:
            # Avoid TemporaryDirectory finalizer removing files under a surviving browser.
            temporary._finalizer.detach()
            report["cleanup"]["temporary_profile_removed"] = False
        if not report["cleanup"].get("temporary_profile_removed"):
            report["result"] = "cleanup_required"
        report["wall_total_seconds"] = time.perf_counter() - started
        report["summary"] = aggregate(report["records"])
        report["resources"] = sampler.report()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--iterations", type=int, default=3, choices=range(1, 11))
    parser.add_argument("--warmup", type=int, default=1, choices=range(0, 4))
    parser.add_argument("--time-budget", type=int, default=180, choices=range(30, 601))
    parser.add_argument("--sample-interval", type=float, default=0.5)
    parser.add_argument(
        "--no-cdp-meter",
        action="store_true",
        help="Disable CDP command/payload instrumentation for independent wall/CPU measurements",
    )
    parser.add_argument(
        "--chromium",
        type=Path,
        default=Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    )
    args = parser.parse_args()
    if not args.chromium.is_file() or not 0.1 <= args.sample_interval <= 5:
        parser.error("Chromium must exist and sample interval must be between 0.1 and 5 seconds")
    # Avoid ambient deployment settings or .env loading. PYTHONPATH remains the
    # explicit source selector; no user credentials/profiles are consumed.
    for key in list(os.environ):
        if key.startswith("CB_"):
            os.environ.pop(key)
    report = run(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "result": report["result"],
                "label": args.label,
                "cdp_meter_enabled": report["cdp_meter_enabled"],
                "summary": report["summary"],
                "cleanup": report["cleanup"],
            }
        )
    )
    return 0 if report["result"] == "completed" else 1


if __name__ == "__main__":
    sys.exit(main())
