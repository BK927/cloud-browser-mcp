"""Read-only CDP deadline shared by all capture verification commands."""

import time

from .models import BrowserError


class DeadlineTab:
    def __init__(self, tab, deadline):
        self.tab, self.deadline = tab, deadline

    def __getattr__(self, name):
        return getattr(self.tab, name)

    def run_cdp(self, command, **arguments):
        if self.deadline is not None:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise BrowserError(
                    "CAPTURE_TIMEOUT", "Capture exceeded its 15-second processing budget"
                )
            arguments["_timeout"] = min(arguments.get("_timeout", 5), remaining)
        return self.tab.run_cdp(command, **arguments)
