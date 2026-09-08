"""Native Chromium WebMCP bridge. No page API shims or arbitrary script execution."""

import json
import threading
import time

from jsonschema import Draft202012Validator
from referencing import Registry

from .models import BrowserError
from .security import SENSITIVE, redact_tree, safe_url


def scrub_result(value, depth=0):
    if depth > 24:
        return "[TRUNCATED]"
    if isinstance(value, dict):
        return {
            str(k): (
                "[REDACTED]"
                if SENSITIVE.search(str(k)) or str(k).lower() in ("cookie", "cookies", "token")
                else scrub_result(v, depth + 1)
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [scrub_result(v, depth + 1) for v in value[:1000]]
    if isinstance(value, str) and value.lstrip().startswith(("{", "[")):
        try:
            decoded = json.loads(value)
        except (ValueError, RecursionError):
            return "[REDACTED: uninspectable structured text]"
        return json.dumps(scrub_result(decoded, depth + 1), ensure_ascii=False)
    if isinstance(value, str) and value.startswith(("https://", "http://")):
        return safe_url(value)
    return redact_tree(value)


def validate_arguments(schema, arguments):
    if len(json.dumps(arguments)) > 64000 or len(json.dumps(schema)) > 64000:
        raise BrowserError("INVALID_INPUT", "Page tool input or schema exceeds the safe size limit")

    def inspect(value, depth=0):
        if depth > 24:
            raise BrowserError("INVALID_INPUT", "Page tool schema nesting is too deep")
        if isinstance(value, dict):
            for key, item in value.items():
                if key in ("$ref", "$dynamicRef") and not str(item).startswith("#"):
                    raise BrowserError(
                        "UNSUPPORTED_OPERATION", "External schema references are not fetched"
                    )
                inspect(item, depth + 1)
        elif isinstance(value, list):
            for item in value:
                inspect(item, depth + 1)

    inspect(schema)
    try:
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema, registry=Registry()).validate(arguments)
    except Exception as exc:
        raise BrowserError(
            "INVALID_INPUT", "Arguments do not match the advertised page-tool schema"
        ) from exc


class PageTools:
    EVENTS = ("WebMCP.toolsAdded", "WebMCP.toolsRemoved", "WebMCP.toolResponded")

    def __init__(self, tab):
        self.tab = tab
        self.lock = threading.Lock()
        self.tools = {}
        self.responses = {}
        self.generation = 0
        self.enabled = False
        self.armed = False
        self.overflow = False

    def enable(self):
        if self.enabled:
            return
        for event, callback in zip(
            self.EVENTS, (self.added, self.removed, self.responded), strict=True
        ):
            self.tab._driver.set_callback(event, callback)
        try:
            self.tab.run_cdp("WebMCP.enable", _timeout=3)
        except Exception as exc:
            self.disable()
            raise BrowserError(
                "UNSUPPORTED_OPERATION", "This Chromium does not expose the native WebMCP interface"
            ) from exc
        self.enabled = True
        # Native registration events arrive asynchronously after enable.
        time.sleep(0.1)

    def added(self, tools, **kwargs):
        with self.lock:
            for tool in tools:
                if len(self.tools) >= 128 or len(json.dumps(tool)) > 64000:
                    self.overflow = True
                    self.generation += 1
                    continue
                key = (tool["frameId"], tool["name"])
                self.tools[key] = {
                    k: tool.get(k)
                    for k in ("frameId", "name", "description", "inputSchema", "annotations")
                }
                self.generation += 1

    def removed(self, tools, **kwargs):
        with self.lock:
            for tool in tools:
                self.tools.pop((tool["frameId"], tool["name"]), None)
                self.generation += 1

    def responded(self, invocationId, status, output=None, **kwargs):
        with self.lock:
            if self.armed and len(self.responses) < 8:
                if len(json.dumps(output)) > 100000:
                    self.responses[invocationId] = ("Oversized", None)
                else:
                    self.responses[invocationId] = (status, scrub_result(output))

    def snapshot(self, frame_id):
        with self.lock:
            return self.generation, [
                dict(tool) for (frame, _), tool in self.tools.items() if frame == frame_id
            ]

    def invoke(self, frame_id, name, arguments, timeout=15):
        with self.lock:
            self.responses.clear()
            self.armed = True
        invocation = None
        try:
            invocation = self.tab.run_cdp(
                "WebMCP.invokeTool",
                frameId=frame_id,
                toolName=name,
                input=arguments,
                _timeout=timeout,
            )["invocationId"]
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                with self.lock:
                    result = self.responses.pop(invocation, None)
                if result:
                    status, output = result
                    if status != "Completed":
                        raise BrowserError(
                            "RESULT_UNCERTAIN",
                            "Page tool failed/cancelled or returned an oversized result after dispatch; do not repeat",
                        )
                    return output
                time.sleep(0.02)
            raise BrowserError("RESULT_UNCERTAIN", "Page tool result timed out; do not repeat")
        except BrowserError:
            raise
        except Exception as exc:
            raise BrowserError("RESULT_UNCERTAIN", "Page tool may have run; do not retry") from exc
        finally:
            with self.lock:
                self.armed = False
                self.responses.clear()
            if invocation:
                try:
                    self.tab.run_cdp("WebMCP.cancelInvocation", invocationId=invocation, _timeout=1)
                except Exception:
                    pass

    def disable(self):
        for event in self.EVENTS:
            self.tab._driver.set_callback(event, None)
        if self.enabled:
            try:
                self.tab.run_cdp("WebMCP.disable", _timeout=1)
            except Exception:
                pass
        with self.lock:
            self.enabled = False
            self.armed = False
            self.tools.clear()
            self.responses.clear()
            self.overflow = False
            self.generation += 1
