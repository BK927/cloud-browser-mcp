import os

import pytest

from cloud_browser.config import Settings
from cloud_browser.worker import Worker


@pytest.mark.browser
async def test_separate_process_keeps_browser_between_calls(tmp_path):
    executable = os.getenv("CB_TEST_CHROMIUM")
    if not executable:
        pytest.skip("Set CB_TEST_CHROMIUM for actual worker test")
    worker = Worker(
        Settings(
            development=True,
            data_dir=tmp_path,
            chromium_path=executable,
            headless=True,
            browser_proxy="",
        )
    )
    try:
        opened = await worker.call("open", session_id="ses_worker", url=None, new_tab=True)
        tabs = await worker.call("list_tabs", session_id="ses_worker")
        assert tabs["tabs"][0]["tab_id"] == opened["tab_id"]
        observed = await worker.call(
            "observe", session_id="ses_worker", tab_id=opened["tab_id"], mode="visual"
        )
        assert observed["_image"]["mimeType"] == "image/jpeg"
        assert worker.process.pid != os.getpid()
    finally:
        await worker.shutdown()
