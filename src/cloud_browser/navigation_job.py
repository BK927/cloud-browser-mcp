"""Single-dispatch navigation with bounded progress probes.

Only the CDP transport runs on a daemon thread, not adapter state or observation.
DrissionPage's websocket driver correlates each request using a separate queue
and a multithread-enabled websocket. The serialized worker owns every probe and
all final state changes. An ACK timeout never causes another navigation command.
"""

import threading
import time

from .models import BrowserError


class NavigationJob:
    def __init__(self, tab, command, arguments, timeout_ms, *, clock=time.monotonic):
        self.tab, self.clock = tab, clock
        self.started = clock()
        self.timeout_ms = timeout_ms
        self.deadline = self.started + timeout_ms / 1000
        self.phase = "command_response"
        self.ack = threading.Event()
        self.reply = None
        self.failure = None
        self.settled_at = None
        self.cancelled = False
        self.frame = None
        self.page = None

        def dispatch():
            try:
                self.reply = tab.run_cdp(command, **arguments, _timeout=timeout_ms / 1000)
            except TimeoutError:
                self.failure = "NAVIGATION_TIMEOUT"
            except Exception:
                self.failure = "NAVIGATION_FAILED"
            finally:
                self.ack.set()

        self.thread = threading.Thread(target=dispatch, daemon=True, name="cb-navigation-ack")
        self.thread.start()

    def progress(self):
        return {
            "pending": not self.cancelled,
            "phase": self.phase,
            "elapsed_ms": max(0, int((self.clock() - self.started) * 1000)),
            "timeout_ms": self.timeout_ms,
        }

    def fail(self, code):
        raise BrowserError(
            code,
            "Navigation did not complete; it was not automatically retransmitted",
            navigation=self.progress() | {"pending": False},
            current_page=self.page,
        )

    def poll(self, *, before, expected_entry=None, operation="goto", settle_ms=0):
        if self.cancelled:
            self.fail("NAVIGATION_CANCELLED")
        if self.failure:
            self.fail(self.failure)
        if self.clock() >= self.deadline:
            self.fail("NAVIGATION_TIMEOUT")
        if not self.ack.is_set():
            return False
        if self.reply.get("errorText") or self.reply.get("isDownload"):
            self.fail("NAVIGATION_FAILED")
        self.phase = "document_transition"
        # Each probe is bounded independently; a renderer stall is not a worker
        # watchdog event and must not terminate unrelated works.
        timeout = min(0.2, max(0.01, self.deadline - self.clock()))
        try:
            frame = self.tab.run_cdp("Page.getFrameTree", _timeout=timeout)["frameTree"]["frame"]
            self.frame = frame
            from .security import safe_url

            self.page = {"url": safe_url(frame["url"]), "title": None}
            if frame.get("unreachableUrl") or frame["url"].startswith("chrome-error:"):
                self.fail("NAVIGATION_FAILED")
            document = frame["id"] + ":" + frame.get("loaderId", "")
            loader = self.reply.get("loaderId")
            matches = document == frame["id"] + ":" + loader if loader else True
            if operation == "reload":
                matches = matches and document != before["document"]
            if not loader and operation == "goto":
                matches = matches and (
                    document != before["document"]
                    or before["sequence"] != before["state"].same_document_sequence
                )
            if expected_entry is not None:
                history = self.tab.run_cdp("Page.getNavigationHistory", _timeout=timeout)
                matches = (
                    matches and history["entries"][history["currentIndex"]]["id"] == expected_entry
                )
            if not matches:
                return False
            self.phase = "loading"
            world = self.tab.run_cdp(
                "Page.createIsolatedWorld",
                frameId=frame["id"],
                worldName="cloud-browser-observer",
                _timeout=timeout,
            )["executionContextId"]
            ready = self.tab.run_cdp(
                "Runtime.evaluate",
                expression="document.readyState",
                contextId=world,
                returnByValue=True,
                _timeout=timeout,
            )
            if ready.get("result", {}).get("value") != "complete":
                self.settled_at = None
                return False
            self.phase = "final_verification"
            if self.settled_at is None:
                self.settled_at = self.clock()
            if (self.clock() - self.settled_at) * 1000 < settle_ms:
                return False
            # Recheck loader after readiness, so a replacement document cannot
            # inherit the readyState proof of the document that disappeared.
            final = self.tab.run_cdp("Page.getFrameTree", _timeout=timeout)["frameTree"]["frame"]
            if final.get("loaderId") != frame.get("loaderId") or final["id"] != frame["id"]:
                self.settled_at = None
                return False
            from .security import redact

            title = (
                self.tab.run_cdp(
                    "Runtime.evaluate",
                    expression="document.title.substring(0,1000)",
                    contextId=world,
                    returnByValue=True,
                    _timeout=timeout,
                )
                .get("result", {})
                .get("value")
            )
            self.page = {"url": safe_url(final["url"]), "title": redact(title or "") or None}
            self.history = self.tab.run_cdp("Page.getNavigationHistory", _timeout=timeout)
            if self.clock() >= self.deadline:
                self.fail("NAVIGATION_TIMEOUT")
            return True
        except BrowserError:
            raise
        except Exception:
            # Context loss or an individual probe timeout is not a load success.
            return False

    def cancel(self):
        self.cancelled = True
        self.phase = "cancelled"
        try:
            self.tab.run_cdp("Page.stopLoading", _timeout=0.2)
        except Exception:
            # Closing the exact tab/session is still possible; never kill worker.
            pass
