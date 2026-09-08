"""Native CDP input with bounded cleanup. No global clipboard or OS shortcuts."""

import contextlib
import time

from .models import BrowserError

MODIFIERS = {"ALT": 1, "CONTROL": 2, "META": 4, "SHIFT": 8}
KEYS = {
    "ENTER": ("Enter", "Enter", 13, "\r"),
    "TAB": ("Tab", "Tab", 9, None),
    "ESCAPE": ("Escape", "Escape", 27, None),
    "SPACE": (" ", "Space", 32, " "),
    "ARROWUP": ("ArrowUp", "ArrowUp", 38, None),
    "ARROWDOWN": ("ArrowDown", "ArrowDown", 40, None),
    "ARROWLEFT": ("ArrowLeft", "ArrowLeft", 37, None),
    "ARROWRIGHT": ("ArrowRight", "ArrowRight", 39, None),
    "BACKSPACE": ("Backspace", "Backspace", 8, None),
    "DELETE": ("Delete", "Delete", 46, None),
    "HOME": ("Home", "Home", 36, None),
    "END": ("End", "End", 35, None),
    "PAGEUP": ("PageUp", "PageUp", 33, None),
    "PAGEDOWN": ("PageDown", "PageDown", 34, None),
}
KEYS.update({c: (c.lower(), "Key" + c, ord(c), c.lower()) for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"})


class NativeInput:
    def __init__(self, tab):
        self.tab = tab
        self.held_key = None
        self.held_button = None
        self.point = (0, 0)

    def key(self, name, modifiers=()):
        key, code, virtual, text = KEYS[name]
        bits = sum(MODIFIERS[m] for m in set(modifiers))
        if name in ("C", "V", "X") and bits & 6:
            raise BrowserError(
                "POLICY_BLOCKED",
                "Use browser_clipboard; global clipboard shortcuts are not exposed",
                "blocked",
            )
        if bits & 8 and len(key) == 1:
            key = key.upper()
            text = key
        parameters = dict(key=key, code=code, windowsVirtualKeyCode=virtual, modifiers=bits)
        self.held_key = parameters
        try:
            self.tab.run_cdp(
                "Input.dispatchKeyEvent",
                type="keyDown",
                **parameters,
                **({"text": text} if text and not bits & 7 else {}),
            )
        finally:
            self.tab.run_cdp("Input.dispatchKeyEvent", type="keyUp", **parameters, _timeout=1)
            self.held_key = None

    def mouse(self, event, x, y, *, button="left", count=1, modifiers=()):
        self.point = (x, y)
        if event == "mousePressed":
            self.held_button = button
        self.tab.run_cdp(
            "Input.dispatchMouseEvent",
            type=event,
            x=x,
            y=y,
            button=button,
            buttons=0
            if event == "mouseReleased"
            else {"left": 1, "right": 2, "middle": 4}.get(self.held_button, 0),
            clickCount=count,
            modifiers=sum(MODIFIERS[m] for m in set(modifiers)),
        )
        if event == "mouseReleased":
            self.held_button = None

    def click(self, x, y, *, button="left", count=1, modifiers=()):
        for n in range(1, count + 1):
            try:
                self.mouse("mousePressed", x, y, button=button, count=n, modifiers=modifiers)
            finally:
                self.mouse("mouseReleased", x, y, button=button, count=n, modifiers=modifiers)

    def drag(self, start, end, *, steps=12, modifiers=()):
        self.mouse("mouseMoved", *start, button="none", modifiers=modifiers)
        try:
            self.mouse("mousePressed", *start, modifiers=modifiers)
            for n in range(1, steps + 1):
                point = [a + (b - a) * n / steps for a, b in zip(start, end, strict=True)]
                self.mouse("mouseMoved", *point, modifiers=modifiers)
                time.sleep(0.015)
        finally:
            self.mouse("mouseReleased", *end, modifiers=modifiers)

    def release(self):
        # Best effort when a target disconnects. The caller still reports uncertainty.
        if self.held_key:
            with contextlib.suppress(Exception):
                self.tab.run_cdp(
                    "Input.dispatchKeyEvent", type="keyUp", **self.held_key, _timeout=1
                )
                self.held_key = None
        if self.held_button:
            with contextlib.suppress(Exception):
                self.tab.run_cdp(
                    "Input.dispatchMouseEvent",
                    type="mouseReleased",
                    x=self.point[0],
                    y=self.point[1],
                    button=self.held_button,
                    buttons=0,
                    _timeout=1,
                )
                self.held_button = None
