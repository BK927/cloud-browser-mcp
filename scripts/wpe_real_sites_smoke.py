"""WPE check against two fixed public sites; no accounts or credentials."""

import json
from types import SimpleNamespace
from urllib.parse import urlsplit

from cloud_browser.models import BrowserError
from cloud_browser.wpe import WPEAdapter

SITES = ("https://example.com/", "https://www.python.org/")


def main():
    adapter = WPEAdapter(
        SimpleNamespace(
            wpe_driver_path="/usr/bin/WPEWebDriver",
            wpe_cog_path="/usr/bin/cog",
            wpe_weston_path="/usr/bin/weston",
            browser_proxy="",
            network_isolated=False,
            managed_display=False,
            max_capture_pixels=8_000_000,
        )
    )
    results = []
    try:
        opened = adapter.open("ses_public_sites", SITES[0])
        tab_id = opened["tab_id"]
        for index, url in enumerate(SITES):
            if index:
                adapter.open("ses_public_sites", url, new_tab=False)
            observed = adapter.observe("ses_public_sites", tab_id, max_chars=4000)
            page = observed["page"]
            text = observed["observation"]["semantic_snapshot"]
            result = {
                "requested_host": urlsplit(url).hostname,
                "final_host": urlsplit(page["url"]).hostname,
                "text_chars": len(text),
                "observed_nodes": len(observed["observation"]["interactive_snapshot"].splitlines()),
                "same_tab": observed["tab_id"] == tab_id,
            }
            try:
                image = adapter.observe("ses_public_sites", tab_id, mode="visual")
                result["screenshot"] = image["observation"]["screenshot"] is not None
            except BrowserError as exc:
                result["screenshot"] = exc.code
            if urlsplit(url).hostname == "www.python.org":
                controls = adapter.observe(
                    "ses_public_sites", tab_id, mode="interactive", max_chars=30000
                )
                anchors = [
                    json.loads(line)
                    for line in controls["observation"]["interactive_snapshot"].splitlines()
                ]
                destination = next(
                    (
                        item
                        for item in anchors
                        if item.get("href", "").rstrip("/") == "https://www.python.org/psf"
                    ),
                    None,
                )
                assert destination is not None, "Python.org PSF link was not observed"
                prepared = adapter.prepare(
                    "ses_public_sites",
                    tab_id,
                    controls["revision"],
                    {"type": "click", "node_id": destination["node_id"]},
                )
                assert prepared["requires_confirmation"]
                try:
                    clicked = adapter.act(
                        "ses_public_sites",
                        tab_id,
                        controls["revision"],
                        {"type": "click", "node_id": destination["node_id"]},
                    )
                except BrowserError as exc:
                    result["click_error"] = {
                        "code": exc.code,
                        "driver_error": exc.details.get("driver_error"),
                    }
                    results.append(result)
                    print(json.dumps(results, ensure_ascii=False))
                    raise
                result["observed_click"] = clicked["action_result"]["performed"]
                result["same_tab_after_click"] = clicked["tab_id"] == tab_id
            results.append(result)
        print(json.dumps(results, ensure_ascii=False))
        assert all(item["text_chars"] > 0 and item["same_tab"] for item in results)
    finally:
        adapter.shutdown()


if __name__ == "__main__":
    main()
