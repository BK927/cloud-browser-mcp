"""Compare isolated WPE/Chromium public-page reading without production state.

Run as an unprivileged Linux user with both browsers installed. This is a
small canary, not a statistically meaningful performance benchmark.
"""

import json
import statistics
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

import psutil

from cloud_browser.config import Settings
from cloud_browser.drission import DrissionAdapter
from cloud_browser.wpe import WPEAdapter

URLS = (
    "https://example.com/",
    "https://www.python.org/",
    "https://example.com/",
)


def process_memory():
    children = psutil.Process().children(recursive=True)
    rss = uss = 0
    for child in children:
        try:
            info = child.memory_full_info()
        except psutil.Error:
            continue
        rss += info.rss
        uss += getattr(info, "uss", 0)
    return {"processes": len(children), "rss_mib": round(rss / 1048576, 1), "uss_mib": round(uss / 1048576, 1)}


def run_engine(engine):
    with tempfile.TemporaryDirectory(prefix=f"cb-{engine}-canary-") as directory:
        cfg = Settings(
            _env_file=None,
            engine=engine,
            development=True,
            max_sessions=1,
            webmcp_enabled=False,
            browser_proxy="",
            network_isolated=False,
            managed_display=False,
            headless=True,
            data_dir=Path(directory),
        )
        adapter = WPEAdapter(cfg) if engine == "wpe" else DrissionAdapter(cfg)
        result = {"engine": engine, "startup_ms": None, "pages": []}
        started = time.monotonic()
        try:
            opened = adapter.open("ses_canary", new_tab=False)
            tab_id = opened["tab_id"]
            result["startup_ms"] = round((time.monotonic() - started) * 1000)
            result["startup_memory"] = process_memory()
            for url in URLS:
                page_started = time.monotonic()
                adapter.navigate("ses_canary", tab_id, "goto", url)
                observed = adapter.observe("ses_canary", tab_id, mode="semantic", max_chars=4000)
                text = observed["observation"]["semantic_snapshot"]
                page = {
                    "host": urlsplit(url).hostname,
                    "elapsed_ms": round((time.monotonic() - page_started) * 1000),
                    "text_chars": len(text),
                    "memory": process_memory(),
                }
                result["pages"].append(page)
                if not text:
                    raise RuntimeError(f"No readable text from {page['host']}")
            result["median_page_ms"] = round(
                statistics.median(item["elapsed_ms"] for item in result["pages"])
            )
            result["peak_sampled_uss_mib"] = max(
                [result["startup_memory"]["uss_mib"]]
                + [item["memory"]["uss_mib"] for item in result["pages"]]
            )
            result["ok"] = True
        except Exception as exc:
            result["ok"] = False
            result["error_type"] = type(exc).__name__
            result["error"] = str(exc)[:240]
        finally:
            adapter.shutdown()
        time.sleep(0.2)
        result["children_after_shutdown"] = process_memory()["processes"]
        return result


def main():
    engines = ("chromium", "wpe") if "--reverse" in sys.argv[1:] else ("wpe", "chromium")
    results = [run_engine(engine) for engine in engines]
    print(json.dumps(results, ensure_ascii=False))
    if not all(item["ok"] for item in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
