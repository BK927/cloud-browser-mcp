import json
import os

import pytest

from cloud_browser.config import Settings
from cloud_browser.drission import DrissionAdapter


@pytest.mark.browser
def test_live_w3c_accordion(tmp_path):
    executable = os.getenv("CB_TEST_CHROMIUM")
    if not executable or os.getenv("CB_TEST_LIVE") != "true":
        pytest.skip("Set CB_TEST_CHROMIUM and CB_TEST_LIVE=true for opt-in W3C network test")
    adapter = DrissionAdapter(
        Settings(
            development=True,
            data_dir=tmp_path,
            chromium_path=executable,
            headless=True,
            browser_proxy="",
        )
    )
    sid = "ses_w3c"
    try:
        opened = adapter.open(
            sid, "https://www.w3.org/WAI/ARIA/apg/patterns/accordion/examples/accordion/"
        )
        tid = opened["tab_id"]
        observed = adapter.observe(sid, tid, mode="interactive")
        nodes = [
            json.loads(line)
            for line in observed["observation"]["interactive_snapshot"].splitlines()
            if line
        ]
        button = next(
            n for n in nodes if n["tag"] == "button" and n["name"] == "Personal Information"
        )
        before = button["expanded"]
        assert before == "true"
        adapter.act(sid, tid, observed["revision"], {"type": "click", "node_id": button["node_id"]})
        observed = adapter.observe(sid, tid, mode="interactive")
        nodes = [
            json.loads(line)
            for line in observed["observation"]["interactive_snapshot"].splitlines()
            if line
        ]
        assert next(n for n in nodes if n["name"] == "Personal Information")["expanded"] == "false"
    finally:
        adapter.shutdown()
