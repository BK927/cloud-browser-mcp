"""Reader-only Fetch interception; callbacks never perform transport I/O."""

from queue import SimpleQueue
from threading import Thread

RESOURCE_TYPES = {"images": "Image", "video": "Media", "fonts": "Font"}


class ReaderResources:
    def __init__(self):
        self.skipped = frozenset(("Media", "Font"))
        self.count = {"blocked_requests": 0}
        self.queue = SimpleQueue()
        self.thread = Thread(target=self._dispatch, daemon=True, name="cb-reader-fetch")
        self.thread.start()

    def reset(self, loaded):
        self.skipped = frozenset(
            kind for name, kind in RESOURCE_TYPES.items() if name not in loaded
        )
        # Queued events retain their read's counter rather than charging the next read.
        self.count = {"blocked_requests": 0}

    def attach(self, tab):
        def paused(requestId=None, resourceType=None, **kwargs):
            try:
                self.queue.put_nowait((tab._driver, requestId, resourceType, self.count))
            except Exception:
                # Never propagate into DrissionPage's shared event callback thread.
                pass

        tab._driver.set_callback("Fetch.requestPaused", paused)
        self.apply(tab)

    def apply(self, tab):
        if self.skipped:
            tab.run_cdp(
                "Fetch.enable",
                patterns=[
                    {"resourceType": kind, "requestStage": "Request"}
                    for kind in RESOURCE_TYPES.values()
                    if kind in self.skipped
                ],
            )
        else:
            tab.run_cdp("Fetch.disable")

    def _dispatch(self):
        while (item := self.queue.get()) is not None:
            driver, request_id, kind, count = item
            if not request_id:
                continue
            # Consult the current policy even for an event queued before a change.
            blocked = kind in self.skipped
            try:
                result = driver.run(
                    "Fetch.failRequest" if blocked else "Fetch.continueRequest",
                    requestId=request_id,
                    **({"errorReason": "BlockedByClient"} if blocked else {}),
                    _timeout=1,
                )
                if blocked and "error" not in result:
                    count["blocked_requests"] += 1
            except Exception:
                pass  # Closing targets/disconnected transports must not kill the dispatcher.

    def close(self):
        self.queue.put_nowait(None)
        self.thread.join(timeout=2)
