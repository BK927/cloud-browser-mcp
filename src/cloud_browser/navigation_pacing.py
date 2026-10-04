"""In-memory navigation spacing shared by reader and interactive works."""

import asyncio
import math
import time
from collections import deque
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from .models import BrowserError


class NavigationPacer:
    def __init__(self, settings):
        self.cfg = settings
        self.hosts = {}
        self.clock = time.monotonic
        self.sleep = asyncio.sleep

    def expire(self):
        now = self.clock()
        for host, state in list(self.hosts.items()):
            while state["dispatches"] and state["dispatches"][0] <= now - 60:
                state["dispatches"].popleft()
            if not state["dispatches"] and not state["users"]:
                del self.hosts[host]

    @staticmethod
    def _busy(wait):
        return BrowserError(
            "BROWSER_BUSY",
            "Navigation host rate limit reached; retry after the indicated delay",
            busy_reason="host_rate_limit",
            retry_after_seconds=max(1, math.ceil(wait)),
        )

    @asynccontextmanager
    async def pace(self, url, deadline=None):
        self.expire()
        try:
            host = (urlsplit(url).hostname or "").lower() if url else ""
        except ValueError:
            host = ""  # Reloads use only available cached page metadata.
        host = host.removeprefix("www.")
        if not host:
            yield None
            return
        state = self.hosts.setdefault(
            host, {"dispatches": deque(), "lock": asyncio.Lock(), "users": 0}
        )
        state["users"] += 1
        acquired = False
        wait_deadline = self.clock() + 5
        if deadline is not None:
            # Leave time to enter the command queue before call()'s outer timer.
            wait_deadline = min(wait_deadline, deadline - 0.05)
        try:
            if state["lock"].locked():
                budget = wait_deadline - self.clock()
                if budget <= 0:
                    raise self._busy(1)
                try:
                    await asyncio.wait_for(state["lock"].acquire(), budget)
                except TimeoutError as exc:
                    raise self._busy(1) from exc
            else:
                await state["lock"].acquire()
            acquired = True
            self.expire()
            dispatches = state["dispatches"]
            now = self.clock()
            if len(dispatches) >= self.cfg.navigation_per_host_per_minute:
                raise self._busy(dispatches[0] + 60 - now)
            wait = (
                max(0, dispatches[-1] + self.cfg.navigation_min_interval_ms / 1000 - now)
                if dispatches
                else 0
            )
            if wait > 0:
                if wait > wait_deadline - now:
                    raise self._busy(wait)
                await self.sleep(wait)

            def dispatched():
                nonlocal acquired
                dispatches.append(self.clock())
                # Release at dispatch, so loading/observation never holds a host turn.
                state["lock"].release()
                acquired = False

            yield dispatched
        finally:
            if acquired:
                state["lock"].release()
            state["users"] -= 1
            self.expire()
