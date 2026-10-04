import http.server
import os
import threading
import time
from unittest.mock import Mock

import pytest

from cloud_browser.config import Settings
from cloud_browser.drission import DrissionAdapter
from cloud_browser.models import BrowserError


@pytest.fixture
def fake_adapter(tmp_path, monkeypatch):
    def create(*, profile=None, **settings):
        adapter = object.__new__(DrissionAdapter)
        adapter.cfg = Settings(
            _env_file=None,
            _env_prefix="LOCALE_TEST_",
            development=True,
            data_dir=tmp_path,
            **settings,
        )
        adapter.sessions, adapter.runtimes = {}, {}
        options = Mock(arguments=[], prefs={})
        for method in (
            "set_browser_path",
            "set_local_port",
            "set_user_data_path",
            "set_timeouts",
            "set_retry",
        ):
            getattr(options, method).return_value = options
        options.set_argument.side_effect = lambda arg, value=None: options.arguments.append(
            arg if value is None else f"{arg}={value}"
        )
        options.set_pref.side_effect = lambda name, value: options.prefs.update({name: value})
        adapter.Options = Mock(return_value=options)
        browser = Mock(process_id=0)
        browser.get_tabs.return_value = [Mock(tab_id="initial")]
        adapter.Chromium = Mock(return_value=browser)
        adapter._start_artifacts = Mock()
        adapter._watch_document = Mock()
        adapter._capture_state = Mock()
        adapter._result = Mock(return_value={})
        monkeypatch.setattr("cloud_browser.drission.DisplayRuntime", Mock())
        adapter.open("ses_test", profile=profile)
        adapter.Options.assert_called_once_with(read_file=False)
        adapter.Chromium.assert_called_once_with(options)
        return adapter, options, browser

    return create


@pytest.mark.parametrize("profile", [None, "reader"])
def test_explicit_reader_profile_and_cache_limit(fake_adapter, profile):
    adapter, options, _ = fake_adapter(profile=profile)
    directory = adapter.cfg.data_dir / "profiles" / (profile or "ses_test")
    options.set_user_data_path.assert_called_once_with(str(directory))
    assert directory.is_dir()
    assert ("--disk-cache-size=67108864" in options.arguments) == (profile == "reader")


def test_reader_observation_uses_snapshot_page_without_sdk_load_wait(fake_adapter):
    adapter, _, _ = fake_adapter(profile="reader")
    adapter._result.reset_mock()  # Ignore the initial blank-page open result.
    tid = adapter.sessions["ses_test"]["selected"]
    adapter._capture_page = Mock(
        return_value={
            "url": "https://blog.naver.com/PostView.naver?blogId=someuser&logNo=223456789012",
            "title": "Public article",
            "semantic_text": "Article text",
            "viewport": {"width": 1024, "height": 768},
            "interactive_truncated": False,
            "semantic_source": "main",
            "semantic_source_truncated": False,
            "accessibility_source": "dom-fallback",
            "scroll_scan_truncated": False,
            "readable_frames": 0,
            "frame_reading_truncated": False,
            "frames": [],
            "reader_links": [],
            "protected": False,
        }
    )
    result = adapter.observe(
        "ses_test", tid, mode="semantic", reader_options={"collect_links": True}
    )
    assert "blogId=someuser" in result["page"]["url"]
    assert result["page"]["title"] == "Public article"
    assert result["observation"]["semantic_snapshot"] == "Article text"
    adapter._result.assert_not_called()


@pytest.mark.parametrize(
    "settings,language,accept_languages",
    [
        ({}, None, None),
        ({"browser_language": "ko-KR"}, "ko-KR", "ko-KR,ko"),
        (
            {"browser_language": "ko-KR", "browser_accept_language": "ko-KR,ko,en-US,en"},
            "ko-KR",
            "ko-KR,ko,en-US,en",
        ),
        ({"browser_accept_language": "en-US,en"}, None, "en-US,en"),
    ],
)
def test_browser_launch_locale_options(fake_adapter, settings, language, accept_languages):
    _, options, browser = fake_adapter(**settings)
    assert [arg for arg in options.arguments if arg.startswith("--lang=")] == (
        [] if language is None else [f"--lang={language}"]
    )
    assert options.prefs == (
        {} if accept_languages is None else {"intl.accept_languages": accept_languages}
    )
    assert not any(
        call.args[0] == "Emulation.setTimezoneOverride"
        for call in browser.get_tabs.return_value[0].run_cdp.call_args_list
    )


@pytest.mark.parametrize("paused", [False, True])
def test_timezone_applies_to_each_discovered_tab_and_popup(fake_adapter, paused):
    adapter, _, browser = fake_adapter(browser_timezone="Asia/Seoul")
    tabs = browser.get_tabs.return_value
    adapter.sessions["ses_test"]["paused"] = paused
    for name in ("new_tab", "popup"):
        tabs.append(Mock(tab_id=name))
        adapter._sync("ses_test")
    adapter._sync("ses_test")
    for tab in tabs:
        overrides = [
            call
            for call in tab.run_cdp.call_args_list
            if call.args[0] == "Emulation.setTimezoneOverride"
        ]
        assert len(overrides) == 1
        assert overrides[0].kwargs == {"timezoneId": "Asia/Seoul"}


@pytest.mark.browser
def test_real_browser_language_timezone_and_accept_language(tmp_path, monkeypatch):
    executable = os.getenv("CB_TEST_CHROMIUM")
    if not executable:
        pytest.skip("Set CB_TEST_CHROMIUM to a Chromium executable for real browser tests")
    headers = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            headers[self.path] = self.headers.get("Accept-Language")
            body = b"<!doctype html><title>Locale</title><p>Locale test</p>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"

    def fixture_url_only(url, *, dns_proxy=None):
        assert dns_proxy is None
        if not url.startswith(base + "/"):
            raise BrowserError("INVALID_URL", "Test fixture only")

    monkeypatch.setattr("cloud_browser.drission.validate_url", fixture_url_only)
    adapter = DrissionAdapter(
        Settings(
            _env_file=None,
            _env_prefix="LOCALE_TEST_",
            development=True,
            data_dir=tmp_path,
            headless=True,
            chromium_path=executable,
            browser_proxy="",
            browser_language="ko-KR",
            browser_timezone="Asia/Seoul",
        )
    )
    try:
        sid = "ses_test"
        first = adapter.open(sid, base + "/locale")
        adapter.open(sid, base + "/new_tab")
        adapter._tab(sid, first["tab_id"]).tab.run_js(
            "window.open(arguments[0], '_blank')", base + "/popup"
        )
        deadline = time.monotonic() + 5
        while len(adapter.list_tabs(sid)["tabs"]) < 3 and time.monotonic() < deadline:
            time.sleep(0.05)
        tabs = adapter.list_tabs(sid)["tabs"]
        assert len(tabs) == 3
        for tab in tabs:
            actual = adapter._tab(sid, tab["tab_id"]).tab.run_js(
                "return [navigator.language, navigator.languages[0], "
                "Intl.DateTimeFormat().resolvedOptions().timeZone, new Date().getTimezoneOffset()]"
            )
            assert actual == ["ko-KR", "ko-KR", "Asia/Seoul", -540]
        for path in ("/locale", "/new_tab", "/popup"):
            assert headers[path].split(",")[0] == "ko-KR"
    finally:
        adapter.shutdown()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
