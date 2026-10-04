import pytest

from cloud_browser.input_driver import NativeInput


class Target:
    def __init__(self, fail):
        self.events = []
        self.fail = fail

    def run_cdp(self, method, **args):
        self.events.append((method, args))
        if args.get("type") == self.fail:
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


def test_ascii_typing_has_keydown_character_keyup_and_releases_failure():
    target = Target("never")
    native = NativeInput(target)
    native.type_text("aA1!")
    assert [event[1]["type"] for event in target.events] == ["rawKeyDown", "char", "keyUp"] * 4
    assert target.events[3][1]["code"] == "KeyA" and target.events[3][1]["modifiers"] == 8
    assert target.events[9][1]["code"] == "Digit1" and target.events[9][1]["modifiers"] == 8
    target.fail = "char"
    with pytest.raises(RuntimeError):
        native.type_text("z")
    assert target.events[-1][1]["type"] == "keyUp"
    assert not native.held_key


def test_unicode_and_control_text_are_inserted_without_shortcut_activation():
    target = Target("never")
    native = NativeInput(target)
    native.type_text("한글\n\t")
    assert [method for method, _ in target.events] == ["Input.insertText"] * 4
    assert "".join(parameters["text"] for _, parameters in target.events) == "한글\n\t"
