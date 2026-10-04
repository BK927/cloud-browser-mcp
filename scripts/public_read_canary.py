#!/usr/bin/env python3
"""Read-only, disposable-profile public-site regression, not a Pi benchmark.

Run from an installed checkout, for example:
  python scripts/public_read_canary.py --chromium-path /usr/bin/chromium \
      --repeats 2 --visual --output public-read.json

This deliberately runs a local headless DEVELOPMENT adapter without production
network isolation or OAuth. It neither starts an MCP listener nor changes a
deployment. Results cannot establish Pi, headed-browser or ChatGPT parity.
Only fixed public routes and one strictly checked observed DC article are requested.
No credentials, clicks, form submission,
page content, console logs, response URLs or image bytes are written to reports.
"""

import argparse
import json
import math
import platform
import re
import tempfile
import time
from collections.abc import Callable
from contextlib import contextmanager
from functools import partial
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from cloud_browser.config import Settings
from cloud_browser.drission import DrissionAdapter
from cloud_browser.models import BrowserError
from cloud_browser.resources import memory_state

TARGETS = (
    ("dc_list", "https://gall.dcinside.com/board/lists/?id=programming", None),
    (
        "dc_article",
        "https://gall.dcinside.com/board/view/?id=programming&no=2940536",
        ".write_div",
    ),
    ("youtube_search", "https://www.youtube.com/results?search_query=raspberrypi", None),
    ("youtube_video", "https://www.youtube.com/watch?v=jNQXAC9IVRw", "#description"),
)
ERROR_CODES = frozenset(
    {
        "AUTH_REQUIRED",
        "BOT_BLOCKED",
        "CAPTCHA_REQUIRED",
        "INVALID_URL",
        "NAVIGATION_FAILED",
        "NAVIGATION_TIMEOUT",
        "PRIVACY_INSPECTION_INCOMPLETE",
        "RESOURCE_PRESSURE",
        "SCREEN_CHANGED",
        "CAPTURE_TIMEOUT",
        "SENSITIVE_SCREEN",
        "SESSION_EXPIRED",
        "TAB_NOT_FOUND",
        "FRAME_STALE",
        "TOKEN_VISIBLE",
        "POLICY_BLOCKED",
    }
)
EMPTY_REASONS = frozenset(
    {"missing", "hidden", "protected", "empty", "scan_budget", "scan_budget_exceeded"}
)
CAPTURE_REASONS = frozenset(
    {
        "document",
        "viewport",
        "scroll",
        "frame_document",
        "public_frame_geometry",
        "protected_geometry",
        "privacy_history",
        "privacy_unbounded",
    }
)


def capture_metrics(details):
    """Never copy arbitrary engine/page-provided diagnostics into reports."""
    if not isinstance(details, dict):
        return {}
    attempts = details.get("capture_attempts")
    reasons = details.get("capture_reasons")
    return {
        "capture_attempts": attempts if type(attempts) is int and 1 <= attempts <= 2 else None,
        "capture_reasons": [
            reason
            for reason in reasons[:8]
            if isinstance(reason, str) and reason in CAPTURE_REASONS
        ]
        if isinstance(reasons, list)
        else [],
    }


MEMORY_KEYS = (
    "host_available_mb",
    "available_mb",
    "cgroup_limit_mb",
    "cgroup_used_mb",
    "cgroup_raw_headroom_mb",
    "cgroup_inactive_file_mb",
    "cgroup_reclaimable_estimate_mb",
    "cgroup_estimated_headroom_mb",
    "reserve_mb",
    "admission_mb",
)
DESCRIPTION_TAGS = frozenset(
    {
        "DIV",
        "SECTION",
        "ARTICLE",
        "YTD-TEXT-INLINE-EXPANDER",
        "YTD-WATCH-METADATA",
        "YTD-VIDEO-SECONDARY-INFO-RENDERER",
    }
)
# Only retry deletion of this run's TemporaryDirectory. Browser.close is
# asynchronous on Windows; never force processes closed to make a canary green.
CLEANUP_RETRY_DELAYS = (0.2, 0.5, 1.0, 2.0)
# Internal fixed-selector diagnostics, not an externally exposed JS tool. This
# never reads text, form values, URLs or arbitrary attributes.
DESCRIPTION_GEOMETRY = """(() => {
  const matches=document.querySelectorAll('#description');
  const allowed=new Set(['DIV','SECTION','ARTICLE','YTD-TEXT-INLINE-EXPANDER',
    'YTD-WATCH-METADATA','YTD-VIDEO-SECONDARY-INFO-RENDERER']);
  const candidates=[];
  for(let index=0;index<Math.min(matches.length,8);index++) {
    const element=matches[index], style=getComputedStyle(element);
    const rect=element.getBoundingClientRect();
    let parent=element.parentElement, depth=0, ancestorHidden=false;
    for(;parent&&depth<64;depth++,parent=parent.parentElement) {
      const css=getComputedStyle(parent);
      if(css.display==='none'||css.visibility==='hidden'||css.visibility==='collapse'
         ||Number(css.opacity)===0) ancestorHidden=true;
    }
    const displayNone=style.display==='none';
    const visibilityHidden=style.visibility==='hidden'||style.visibility==='collapse';
    const opacityZero=Number(style.opacity)===0;
    const rendered=rect.width>0&&rect.height>0&&!displayNone&&!visibilityHidden
      &&!opacityZero&&!ancestorHidden&&element.isConnected;
    candidates.push({index,tag:allowed.has(element.tagName)?element.tagName:'OTHER',
      connected:element.isConnected,display_none:displayNone,
      visibility_hidden:visibilityHidden,opacity_zero:opacityZero,
      hidden_by_ancestor:ancestorHidden,ancestor_scan_truncated:!!parent,
      rendered,viewport_intersects:rendered&&rect.right>0&&rect.bottom>0
        &&rect.left<innerWidth&&rect.top<innerHeight,
      rect_width:Math.round(rect.width*100)/100,rect_height:Math.round(rect.height*100)/100});
  }
  return {match_count:matches.length,truncated:matches.length>8,candidates};
})()"""


def safe_number(value):
    if type(value) is int:
        return value if 0 <= value <= 2**63 - 1 else None
    return value if type(value) is float and math.isfinite(value) and value >= 0 else None


def numeric_memory(raw):
    """Do not copy cgroup paths, accounting text, host identity or arbitrary keys."""
    if not isinstance(raw, dict):
        return {}
    return {key: safe_number(raw.get(key)) for key in MEMORY_KEYS}


def exception_code(exc):
    if isinstance(exc, BrowserError) and exc.code in ERROR_CODES:
        return exc.code
    return "UNCLASSIFIED_ERROR"


def same_public_route(actual, expected):
    """Compare the fixed public fields, never report the actual redirected URL."""
    if not isinstance(actual, str):
        return False
    try:
        left, right = urlsplit(actual), urlsplit(expected)
        if (
            left.scheme != right.scheme
            or left.hostname != right.hostname
            or left.port not in (None, 443)
            or left.username is not None
            or left.password is not None
            or left.path != right.path
        ):
            return False
        actual_query = parse_qs(left.query)
        return all(actual_query.get(key) == values for key, values in parse_qs(right.query).items())
    except (ValueError, TypeError):
        return False


def dc_link_counts(snapshot):
    """Count eligible public article links, without returning their data or URLs."""
    total = preserved = 0
    if not isinstance(snapshot, str):
        return {"samples": 0, "numeric_no_preserved": 0, "all_preserved": None}
    for line in snapshot.splitlines():
        try:
            item = json.loads(line)
            if not isinstance(item, dict) or not isinstance(item.get("href"), str):
                continue
            parts = urlsplit(item["href"])
            query = parse_qs(parts.query)
            if (
                parts.scheme != "https"
                or parts.hostname != "gall.dcinside.com"
                or parts.port not in (None, 443)
                or parts.username is not None
                or parts.path != "/board/view/"
                or query.get("id") != ["programming"]
            ):
                continue
            total += 1
            values = query.get("no", [])
            preserved += len(values) == 1 and bool(re.fullmatch(r"[0-9]{1,12}", values[0]))
        except (ValueError, TypeError, json.JSONDecodeError):
            continue
    return {
        "samples": total,
        "numeric_no_preserved": preserved,
        "all_preserved": total == preserved if total else None,
    }


def dc_article_identity(url):
    """Validate a read-only DC route; return identity internally, never report it."""
    if not isinstance(url, str) or len(url) > 2048:
        return None
    try:
        parts = urlsplit(url)
        query = parse_qs(parts.query, keep_blank_values=True)
        if (
            parts.scheme != "https"
            or parts.hostname != "gall.dcinside.com"
            or parts.port not in (None, 443)
            or parts.username is not None
            or parts.password is not None
            or parts.path != "/board/view/"
            or parts.fragment
            or not {"id", "no"} <= set(query) <= {"id", "no", "page"}
            or query["id"] != ["programming"]
            or len(query["no"]) != 1
            or not re.fullmatch(r"[0-9]{1,12}", query["no"][0])
            or (
                "page" in query
                and (len(query["page"]) != 1 or not re.fullmatch(r"[0-9]{1,12}", query["page"][0]))
            )
        ):
            return None
        return parts.scheme, parts.hostname, parts.path, query["id"][0], query["no"][0]
    except (TypeError, ValueError):
        return None


def roundtrip_observed_dc_link(adapter, session_id, tab_id, result):
    """Follow one observed public link and verify same article plus rendered body.

    The URL and public article number remain ephemeral inputs. Only bounded
    counts, booleans and fixed reason codes are returned for the public report.
    """
    snapshot = result.get("observation", {}).get("interactive_snapshot")
    links = []
    if isinstance(snapshot, str):
        for line in snapshot.splitlines()[:20]:
            try:
                node = json.loads(line)
            except (ValueError, TypeError):
                continue
            if isinstance(node, dict) and dc_article_identity(node.get("href")) is not None:
                links.append(node["href"])
    row = {
        "ok": False,
        "reason": "ELIGIBLE_PUBLIC_ARTICLE_LINK_NOT_OBSERVED",
        "eligible_link_count": len(links),
        "observed_link_navigated": False,
        "same_article": False,
        "body_present": False,
        "body_chars": 0,
    }
    if not links:
        return row
    observed = links[0]
    navigation = adapter.navigate(session_id, tab_id, "goto", observed)
    row["observed_link_navigated"] = True
    row["same_article"] = dc_article_identity(
        navigation.get("page", {}).get("url")
    ) == dc_article_identity(observed)
    if not row["same_article"]:
        row["reason"] = "OBSERVED_LINK_DIFFERENT_ARTICLE"
        return row
    body = adapter.observe(
        session_id,
        tab_id,
        mode="semantic",
        max_chars=4000,
        query={"selector": ".write_div", "limit": 20},
    )
    metrics = observation_metrics(body, mode="semantic", scoped=True)
    row["body_present"] = metrics["ok"]
    row["body_chars"] = metrics["text_chars"]
    row["ok"] = metrics["ok"]
    row["reason"] = None if row["ok"] else "OBSERVED_LINK_BODY_NOT_READABLE"
    return row


def observation_metrics(result, *, mode, scoped=False):
    """Build an allowlisted report: never serialize a raw adapter result."""
    observation = result.get("observation", {})
    text = observation.get("semantic_snapshot")
    text_chars = len(text.strip()) if isinstance(text, str) else 0
    interactive = observation.get("interactive_snapshot")
    nodes = len(interactive.splitlines()) if isinstance(interactive, str) else 0
    frames = observation.get("frames")
    frames = frames if isinstance(frames, list) else []
    omitted = observation.get("screenshot_omitted")
    image_present = isinstance(result.get("_image"), dict) and bool(result["_image"].get("data"))
    empty_reason = observation.get("query_empty_reason")
    safe_empty_reason = (
        empty_reason
        if isinstance(empty_reason, str) and empty_reason in EMPTY_REASONS
        else ("unclassified" if empty_reason else None)
    )
    partial = any(
        bool(observation.get(key))
        for key in (
            "truncated",
            "query_scan_truncated",
            "semantic_source_truncated",
            "frame_reading_truncated",
        )
    )
    if mode == "visual":
        ok, reason = image_present, None if image_present else "IMAGE_MISSING"
    else:
        ok, reason = text_chars > 0, None if text_chars else "EMPTY_SEMANTIC_TEXT"
        if scoped and (safe_number(observation.get("query_match_count")) == 0 or empty_reason):
            ok, reason = False, "SCOPED_QUERY_EMPTY"
    row = {
        "ok": ok,
        "reason": reason,
        "partial": partial,
        "text_chars": text_chars,
        "interactive_nodes": nodes,
        "frame_count": len(frames),
        "unreadable_frame_count": sum(
            not bool(f.get("readable")) for f in frames if isinstance(f, dict)
        ),
        "truncated": bool(observation.get("truncated")),
        "query_scan_truncated": bool(observation.get("query_scan_truncated")),
        "semantic_source_truncated": bool(observation.get("semantic_source_truncated")),
        "frame_reading_truncated": bool(observation.get("frame_reading_truncated")),
        "protected_regions_omitted": bool(observation.get("protected_regions_omitted")),
        "query_match_count": safe_number(observation.get("query_match_count")),
        "query_empty_reason": safe_empty_reason,
        "image_returned": image_present,
        "optional_image_omitted": isinstance(omitted, dict),
        "optional_image_reason": (
            omitted.get("code")
            if isinstance(omitted.get("code"), str) and omitted.get("code") in ERROR_CODES
            else "UNCLASSIFIED_ERROR"
        )
        if isinstance(omitted, dict)
        else None,
    }
    if mode == "auto":
        row["dc_links"] = dc_link_counts(interactive)
    row.update(capture_metrics(observation.get("screenshot") or omitted))
    return row


def link_metrics(result):
    row = observation_metrics(result, mode="interactive")
    links = dc_link_counts(result.get("observation", {}).get("interactive_snapshot"))
    row["dc_links"] = links
    row["ok"] = links["samples"] > 0 and links["all_preserved"] is True
    row["reason"] = (
        None
        if row["ok"]
        else (
            "PUBLIC_ARTICLE_LINK_NOT_OBSERVED"
            if not links["samples"]
            else "PUBLIC_ARTICLE_NUMBER_REDACTED"
        )
    )
    return row


def description_candidate_metrics(adapter, session_id, tab_id):
    """Inspect only geometry of fixed YouTube description candidates post-query."""
    if not callable(getattr(adapter, "_tab", None)):
        return {"available": False, "reason": "PROBE_UNAVAILABLE"}
    started = time.monotonic()
    try:
        tab = adapter._tab(session_id, tab_id).tab
        frame = tab.run_cdp("Page.getFrameTree", _timeout=3)["frameTree"]["frame"]["id"]
        world = tab.run_cdp(
            "Page.createIsolatedWorld",
            frameId=frame,
            worldName="cloud-browser-public-canary",
            _timeout=3,
        )["executionContextId"]
        result = tab.run_cdp(
            "Runtime.evaluate",
            expression=DESCRIPTION_GEOMETRY,
            contextId=world,
            returnByValue=True,
            _timeout=3,
        )
        raw = result.get("result", {}).get("value")
        if "exceptionDetails" in result or not isinstance(raw, dict):
            raise ValueError("Invalid geometry response")
        candidates = raw.get("candidates")
        if not isinstance(candidates, list):
            raise ValueError("Invalid geometry response")
        clean = []
        for candidate in candidates[:8]:
            if not isinstance(candidate, dict):
                raise ValueError("Invalid geometry response")
            row = {
                "index": len(clean),
                "tag": candidate.get("tag")
                if candidate.get("tag") in DESCRIPTION_TAGS
                else "OTHER",
            }
            for key in (
                "connected",
                "display_none",
                "visibility_hidden",
                "opacity_zero",
                "hidden_by_ancestor",
                "ancestor_scan_truncated",
                "rendered",
                "viewport_intersects",
            ):
                row[key] = candidate.get(key) if type(candidate.get(key)) is bool else None
            for key in ("rect_width", "rect_height"):
                row[key] = safe_number(candidate.get(key))
            clean.append(row)
        answer = {
            "available": True,
            "reason": None,
            "sample": "post-scoped-observation",
            "match_count": safe_number(raw.get("match_count")),
            "sampled_count": len(clean),
            "truncated": raw.get("truncated") is True,
            "candidates": clean,
        }
    except Exception:
        answer = {"available": False, "reason": "GEOMETRY_PROBE_FAILED"}
    answer["elapsed_ms"] = round((time.monotonic() - started) * 1000, 3)
    return answer


def measured(operation: Callable, summarize: Callable, memory_provider: Callable, cfg):
    started = time.monotonic()
    try:
        row = summarize(operation())
    except Exception as exc:
        # Error messages/details may contain URLs, values or stack paths.
        row = {"ok": False, "reason": exception_code(exc)}
        if isinstance(exc, BrowserError):
            row.update(capture_metrics(exc.details))
    row["elapsed_ms"] = round((time.monotonic() - started) * 1000, 3)
    try:
        row["memory"] = numeric_memory(memory_provider(cfg.memory_reserve_mb))
    except Exception:
        row["memory"] = {}
    return row


def diagnostic_settings(chromium_path, data_dir):
    return Settings(
        _env_file=None,
        development=True,
        data_dir=data_dir,
        engine="chromium",
        chromium_path=chromium_path,
        browser_proxy="",
        network_isolated=False,
        headless=True,
        managed_display=False,
        max_sessions=1,
        manual_control_enabled=False,
        webmcp_enabled=False,
        public_origin="http://127.0.0.1:8000",
        control_origin="http://127.0.0.1:8001",
        public_port=8000,
        control_port=8001,
        bind_host="127.0.0.1",
        admin_password_hash="",
        oauth_redirect_uris=[],
        auth_rules={},
        iframe_screenshot_policy="inspect",
        navigation_timeout=60,
    )


@contextmanager
def disposable_directory(cleanup):
    """Report exact owned-directory cleanup failure without discarding results.

    Windows can retain a Chromium model file lock briefly after quit. Never
    broaden cleanup, terminate unrelated processes or write the path/error text
    into the public report. A remaining directory is a failed cleanup, not a
    successful run. Retry for at most 3.7 seconds, without widening the target or
    claiming that adapter.shutdown() returning proves every process exited.
    """
    manager = tempfile.TemporaryDirectory(
        prefix="cloud-browser-public-read-", ignore_cleanup_errors=True
    )
    try:
        yield manager.name
    finally:
        cleanup_error = None
        attempts = 0
        for delay in (0, *CLEANUP_RETRY_DELAYS):
            if delay:
                time.sleep(delay)
            attempts += 1
            cleanup_error = None
            try:
                manager.cleanup()
            except Exception:
                cleanup_error = "TEMPORARY_CLEANUP_FAILED"
            try:
                if Path(manager.name).exists():
                    cleanup_error = cleanup_error or "TEMPORARY_DIRECTORY_STILL_PRESENT"
                else:
                    cleanup_error = None
            except Exception:
                cleanup_error = cleanup_error or "TEMPORARY_CLEANUP_STATUS_UNKNOWN"
            if cleanup_error is None:
                break
        cleanup["temporary_cleanup_attempts"] = attempts
        cleanup["temporary_cleanup_complete"] = cleanup_error is None
        cleanup["temporary_cleanup_error"] = cleanup_error
        cleanup["cleanup_complete"] = bool(cleanup.get("browser_shutdown_complete")) and (
            cleanup_error is None
        )
        cleanup["ok"] = cleanup["cleanup_complete"]
        cleanup["warning_count"] = int(cleanup_error is not None) + int(
            not cleanup.get("browser_shutdown_complete", False)
        )


def run_canary(
    chromium_path,
    *,
    repeats=2,
    visual=False,
    adapter_factory=DrissionAdapter,
    memory_provider=memory_state,
):
    if not 1 <= repeats <= 3:
        raise ValueError("repeats must be between 1 and 3")
    report = {
        "schema_version": 1,
        "kind": "public-read-regression-not-benchmark",
        "environment": {
            "platform": "windows"
            if platform.system() == "Windows"
            else ("linux" if platform.system() == "Linux" else "other"),
            "engine": "drission-chromium",
            "headless": True,
            "development": True,
            "production_network_isolation_verified": False,
            "production_settings_modified": False,
            "reuses_profiles": False,
            "pi_performance_verified": False,
            "chatgpt_connection_verified": False,
            "browser_shutdown_metric": "adapter-method-return-only",
            "viewport_width": 1024,
            "viewport_height": 768,
        },
        "repeats": [],
    }
    for number in range(1, repeats + 1):
        iteration = {
            "repeat": number,
            "pages": [],
            "cleanup": {
                "ok": False,
                "browser_shutdown_returned": False,
                # No PID/child lifetime check is performed by this harness.
                "process_exit_verified": None,
            },
        }
        with disposable_directory(iteration["cleanup"]) as temporary:
            cfg = diagnostic_settings(chromium_path, Path(temporary))
            adapter = None
            try:
                adapter = adapter_factory(cfg)
                opened = {}

                def open_disposable(adapter=adapter, opened=opened):
                    opened.update(adapter.open("ses_public_canary"))
                    return opened

                iteration["startup"] = measured(
                    open_disposable,
                    lambda _: {"ok": True, "reason": None},
                    memory_provider,
                    cfg,
                )
                if iteration["startup"]["ok"]:
                    sid, tid = "ses_public_canary", opened["tab_id"]
                    for target, url, selector in TARGETS:
                        page = {"target": target, "requested_url": url}
                        observed_links = {}
                        page["navigation"] = measured(
                            partial(adapter.navigate, sid, tid, "goto", url),
                            lambda result, url=url: {
                                "ok": same_public_route(result.get("page", {}).get("url"), url),
                                "reason": None
                                if same_public_route(result.get("page", {}).get("url"), url)
                                else "UNEXPECTED_FINAL_ROUTE",
                            },
                            memory_provider,
                            cfg,
                        )
                        if page["navigation"]["ok"]:
                            for mode in ("semantic", "auto"):
                                page[mode] = measured(
                                    partial(adapter.observe, sid, tid, mode=mode, max_chars=4000),
                                    partial(observation_metrics, mode=mode),
                                    memory_provider,
                                    cfg,
                                )
                            if target == "dc_list":

                                def observe_links(
                                    observed_links=observed_links, adapter=adapter, sid=sid, tid=tid
                                ):
                                    observed_links.update(
                                        adapter.observe(
                                            sid,
                                            tid,
                                            mode="interactive",
                                            max_chars=8000,
                                            query={
                                                "selector": 'a[href*="/board/view/"]',
                                                "limit": 20,
                                            },
                                        )
                                    )
                                    return observed_links

                                page["links"] = measured(
                                    observe_links,
                                    link_metrics,
                                    memory_provider,
                                    cfg,
                                )
                            if selector:
                                page["scoped"] = measured(
                                    partial(
                                        adapter.observe,
                                        sid,
                                        tid,
                                        mode="semantic",
                                        max_chars=4000,
                                        query={"selector": selector, "limit": 20},
                                    ),
                                    lambda result: observation_metrics(
                                        result, mode="semantic", scoped=True
                                    ),
                                    memory_provider,
                                    cfg,
                                )
                                if target == "youtube_video":
                                    page["scoped"]["description_candidates"] = (
                                        description_candidate_metrics(adapter, sid, tid)
                                    )
                            if visual:
                                page["visual"] = measured(
                                    partial(
                                        adapter.observe, sid, tid, mode="visual", full_page=False
                                    ),
                                    lambda result: observation_metrics(result, mode="visual"),
                                    memory_provider,
                                    cfg,
                                )
                            if target == "dc_list":
                                # Capture the list before this navigates away. The
                                # next fixed target still gets its own navigation.
                                page["link_roundtrip"] = measured(
                                    partial(
                                        roundtrip_observed_dc_link,
                                        adapter,
                                        sid,
                                        tid,
                                        observed_links,
                                    ),
                                    lambda result: result,
                                    memory_provider,
                                    cfg,
                                )
                        page["ok"] = all(
                            step["ok"]
                            for key, step in page.items()
                            if key
                            in (
                                "navigation",
                                "semantic",
                                "auto",
                                "scoped",
                                "visual",
                                "links",
                                "link_roundtrip",
                            )
                        )
                        iteration["pages"].append(page)
            except Exception as exc:
                iteration["error"] = exception_code(exc)
            finally:
                if adapter is not None:
                    try:
                        adapter.shutdown()
                        iteration["cleanup"]["browser_shutdown_returned"] = True
                        # Compatibility field: return of the adapter method,
                        # not proof that all Chromium subprocesses have exited.
                        iteration["cleanup"]["browser_shutdown_complete"] = True
                    except Exception:
                        iteration["cleanup"]["browser_shutdown_complete"] = False
                        iteration["cleanup"]["reason"] = "BROWSER_SHUTDOWN_FAILED"
        report["repeats"].append(iteration)
    report["ok"] = all(
        iteration["cleanup"]["ok"]
        and iteration.get("startup", {}).get("ok", False)
        and len(iteration["pages"]) == len(TARGETS)
        and all(page["ok"] for page in iteration["pages"])
        for iteration in report["repeats"]
    )
    return report


def bounded_repeats(raw):
    value = int(raw)
    if not 1 <= value <= 3:
        raise argparse.ArgumentTypeError("repeats must be between 1 and 3")
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chromium-path", required=True)
    parser.add_argument("--repeats", type=bounded_repeats, default=2)
    parser.add_argument("--visual", action="store_true", help="Capture then discard image bytes")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    report = run_canary(args.chromium_path, repeats=args.repeats, visual=args.visual)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print("Read-only regression complete; sanitized report written. Pi/ChatGPT remain unverified.")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
