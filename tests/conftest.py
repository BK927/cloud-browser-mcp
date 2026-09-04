import copy
import secrets

import pytest
from argon2 import PasswordHasher

from cloud_browser.config import Settings
from cloud_browser.models import BrowserError
from cloud_browser.service import BrowserService
from cloud_browser.store import Store


class FakeWorker:
    def __init__(self):
        self.sessions = {}
        self.calls = []
        self.executions = 0
        self.uncertain = False

    async def call(self, method, **args):
        self.calls.append((method, args))
        sid = args["session_id"]
        tid = args.get("tab_id")
        if method == "open":
            session = self.sessions.setdefault(sid, {})
            if args.get("new_tab", True) or not session:
                tid = "tab_" + secrets.token_hex(6)
                session[tid] = {
                    "revision": 1,
                    "page": {"url": "https://example.com/", "title": "Example"},
                }
            else:
                tid = next(iter(session))
        if sid not in self.sessions:
            raise BrowserError("SESSION_EXPIRED", "Worker stopped")
        session = self.sessions[sid]
        if method == "list_tabs":
            return {
                "session_id": sid,
                "selected_tab_id": next(iter(session), None),
                "tabs": [{"tab_id": t, **state["page"]} for t, state in session.items()],
            }
        if method == "close" and args["scope"] == "session":
            del self.sessions[sid]
            return {"session_id": sid}
        if tid not in session:
            raise BrowserError("TAB_NOT_FOUND", "Tab closed")
        state = session[tid]
        result = {"session_id": sid, "tab_id": tid, **copy.deepcopy(state)}
        if method in ("prepare", "act"):
            if args["expected_revision"] != state["revision"]:
                raise BrowserError("STALE_NODE", "Page changed")
            if method == "prepare":
                result.update(
                    target="Submit", requires_confirmation=args["action"]["type"] != "scroll"
                )
            else:
                self.executions += 1
                if self.uncertain:
                    raise BrowserError("RESULT_UNCERTAIN", "Connection lost after dispatch")
                state["revision"] += 1
                result["revision"] = state["revision"]
                result["action_result"] = {"performed": True, "page_changed": True}
        if method == "resume":
            state["revision"] += 1
            result["revision"] = state["revision"]
        if method == "close":
            del session[tid]
        if method == "observe":
            result["observation"] = {
                "semantic_snapshot": "Example",
                "interactive_snapshot": "button node_1",
                "truncated": False,
            }
            if args.get("mode") == "visual":
                result["_image"] = {
                    "data": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jWZkAAAAASUVORK5CYII=",
                    "mimeType": "image/png",
                }
        return result

    async def shutdown(self):
        self.sessions.clear()


@pytest.fixture
def cfg(tmp_path):
    return Settings(
        development=True,
        data_dir=tmp_path,
        admin_password_hash=PasswordHasher().hash("test administrator password"),
        public_origin="https://browser.example",
        control_origin="https://control.example:8443",
        oauth_redirect_uris=["https://chatgpt.com/connector_platform_oauth_redirect"],
        manual_control_enabled=True,
    )


@pytest.fixture
def service(cfg):
    store = Store(cfg.data_dir / "test.sqlite3")
    instance = BrowserService(cfg, store, FakeWorker())
    yield instance
    store.close()
