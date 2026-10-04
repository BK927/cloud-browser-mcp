from types import SimpleNamespace

import pytest

from cloud_browser.models import BrowserError
from cloud_browser.navigation_job import NavigationJob


class NavigationProbe:
    def __init__(self, loader="old", entry=1, ready="complete", unreachable=False):
        self.loader, self.entry, self.ready, self.unreachable = loader, entry, ready, unreachable

    def run_cdp(self, command, **kwargs):
        if command == "Page.getFrameTree":
            return {
                "frameTree": {
                    "frame": {
                        "url": "https://example.com/",
                        "id": "main",
                        "loaderId": self.loader,
                        "unreachableUrl": "https://example.com/" if self.unreachable else "",
                    }
                }
            }
        if command == "Page.getNavigationHistory":
            return {"currentIndex": 0, "entries": [{"id": self.entry}]}
        if command == "Page.navigate":
            return {}
        if command == "Page.createIsolatedWorld":
            return {"executionContextId": 1}
        assert command == "Runtime.evaluate"
        return {
            "result": {
                "value": self.ready if kwargs["expression"] == "document.readyState" else "Title"
            }
        }


def probe(tab):
    clock = [0]
    task = NavigationJob(tab, "Page.navigate", {}, 1000, clock=lambda: clock[0])
    assert task.ack.wait(1)
    before = {
        "document": "main:old",
        "sequence": 0,
        "state": SimpleNamespace(same_document_sequence=0),
    }
    return task, clock, before


@pytest.mark.parametrize(
    "expectation",
    [
        {"expected_loader": "new"},
        {"previous_loader": "old"},
        {"expected_entry": 2},
    ],
)
def test_old_complete_document_is_not_navigation_success(expectation):
    task, clock, before = probe(NavigationProbe())
    if "expected_loader" in expectation:
        task.reply["loaderId"] = expectation["expected_loader"]
    operation = "reload" if "previous_loader" in expectation else "goto"
    assert not task.poll(
        before=before, operation=operation, expected_entry=expectation.get("expected_entry")
    )
    clock[0] = 1
    with pytest.raises(BrowserError) as exc:
        task.poll(before=before)
    assert exc.value.code == "NAVIGATION_TIMEOUT"


def test_target_document_must_finish_loading():
    tab = NavigationProbe(loader="new", ready="interactive")
    task, clock, before = probe(tab)
    task.reply["loaderId"] = "new"
    assert not task.poll(before=before)
    clock[0] = 1
    with pytest.raises(BrowserError) as exc:
        task.poll(before=before)
    assert exc.value.code == "NAVIGATION_TIMEOUT"
    tab.ready = "complete"
    task, _, before = probe(tab)
    task.reply["loaderId"] = "new"
    assert task.poll(before=before)


def test_unreachable_document_is_failure_not_success():
    task, _, before = probe(NavigationProbe(unreachable=True))
    with pytest.raises(BrowserError) as exc:
        task.poll(before=before)
    assert exc.value.code == "NAVIGATION_FAILED"


def test_interactive_readiness_is_opt_in_and_still_settles():
    tab = NavigationProbe(loader="new", ready="interactive")
    task, clock, before = probe(tab)
    task.reply["loaderId"] = "new"
    assert not task.poll(before=before)
    assert not task.poll(before=before, readiness="interactive", settle_ms=500)
    clock[0] = 0.5
    assert task.poll(before=before, readiness="interactive", settle_ms=500)


def test_interactive_readiness_rechecks_loader():
    class Replaced(NavigationProbe):
        def run_cdp(self, command, **kwargs):
            result = super().run_cdp(command, **kwargs)
            if command == "Runtime.evaluate" and kwargs["expression"] == "document.readyState":
                self.loader = "replacement"
            return result

    task, _, before = probe(Replaced(loader="new", ready="interactive"))
    task.reply["loaderId"] = "new"
    assert not task.poll(before=before, readiness="interactive")
