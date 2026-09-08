import pytest

from cloud_browser.input_driver import NativeInput


class Target:
    def __init__(self, fail):
        self.events = []
        self.fail = fail

    def run_cdp(self, method, **args):
        self.events.append((method, args))
        if args["type"] == self.fail:
            raise RuntimeError("Injected dispatch failure")


def test_key_and_mouse_release_after_failure():
    target = Target("keyDown")
    native = NativeInput(target)
    with pytest.raises(RuntimeError):
        native.key("A", ["CONTROL"])
    assert target.events[-1][1]["type"] == "keyUp"
    target.fail = "mousePressed"
    with pytest.raises(RuntimeError):
        native.click(20, 20)
    assert target.events[-1][1]["type"] == "mouseReleased"
    assert not native.held_key and not native.held_button
