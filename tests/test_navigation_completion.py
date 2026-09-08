import time
from types import SimpleNamespace

import pytest

from cloud_browser.drission import DrissionAdapter
from cloud_browser.models import BrowserError


class NavigationProbe:
    def __init__(self, loader="old", entry=1, ready="complete", unreachable=False):
        self.loader, self.entry, self.ready, self.unreachable = loader, entry, ready, unreachable

    def run_cdp(self, command, **kwargs):
        if command == "Page.getFrameTree":
            return {
                "frameTree": {
                    "frame": {
                        "url": "https://example.com/",
                        "loaderId": self.loader,
                        "unreachableUrl": "https://example.com/" if self.unreachable else "",
                    }
                }
            }
        if command == "Page.getNavigationHistory":
            return {"currentIndex": 0, "entries": [{"id": self.entry}]}
        assert command == "Runtime.evaluate"
        return {"result": {"value": self.ready}}


@pytest.mark.parametrize(
    "expectation",
    [
        {"expected_loader": "new"},
        {"previous_loader": "old"},
        {"expected_entry": 2},
    ],
)
def test_old_complete_document_is_not_navigation_success(expectation):
    state = SimpleNamespace(tab=NavigationProbe())
    with pytest.raises(BrowserError) as exc:
        DrissionAdapter._wait_navigation(state, time.monotonic() + 0.01, **expectation)
    assert exc.value.code == "NAVIGATION_TIMEOUT"


def test_target_document_must_finish_loading():
    state = SimpleNamespace(tab=NavigationProbe(loader="new", ready="interactive"))
    with pytest.raises(BrowserError) as exc:
        DrissionAdapter._wait_navigation(state, time.monotonic() + 0.01, expected_loader="new")
    assert exc.value.code == "NAVIGATION_TIMEOUT"
    state.tab.ready = "complete"
    DrissionAdapter._wait_navigation(state, time.monotonic() + 0.01, expected_loader="new")


def test_unreachable_document_is_failure_not_success():
    state = SimpleNamespace(tab=NavigationProbe(unreachable=True))
    with pytest.raises(BrowserError) as exc:
        DrissionAdapter._wait_navigation(state, time.monotonic() + 0.01)
    assert exc.value.code == "NAVIGATION_FAILED"
