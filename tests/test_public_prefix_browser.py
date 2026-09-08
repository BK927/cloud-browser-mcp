"""Real Chrome through the local stripped-prefix proxy, using dummy credentials."""

import asyncio
import http.server
import os
import threading
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest
from prefix_proxy import serve_prefix
from test_public_prefix import authorize_params


@pytest.mark.browser
@pytest.mark.parametrize("prefix", ["/browser", "/apps/browser"])
async def test_real_prefixed_form_cookie_origin_and_callback(cfg, tmp_path, prefix):
    executable = os.getenv("CB_TEST_CHROMIUM")
    if not executable:
        pytest.skip("Set CB_TEST_CHROMIUM for real prefixed OAuth browser checks")
    from DrissionPage import Chromium, ChromiumOptions

    arrivals = []
    arrived = threading.Event()

    class Callback(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if urlsplit(self.path).path == "/callback":
                arrivals.append((self.command, self.path, dict(self.headers)))
                arrived.set()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<title>Local prefixed callback received</title>")

        def log_message(self, *args):
            pass  # Never print even test-only codes or request headers.

    receiver = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Callback)
    receiver_thread = threading.Thread(target=receiver.serve_forever, daemon=True)
    receiver_thread.start()
    # Only this fixture overrides validation to use a local HTTP callback.
    # Production still requires exact HTTPS callbacks and a separate private origin.
    cfg.oauth_redirect_uris = [f"http://127.0.0.1:{receiver.server_port}/callback"]
    try:
        async with serve_prefix(cfg, prefix):
            params, verifier = authorize_params(cfg)

            def browser_form():
                options = ChromiumOptions(read_file=False).set_browser_path(executable)
                options.set_tmp_path(str(tmp_path / "prefix-test-profile")).headless().auto_port()
                browser = Chromium(options)
                try:
                    tab = browser.latest_tab
                    tab.set.timeouts(base=3, page_load=5, script=3)
                    auth_url = cfg.public_base + "/authorize"
                    tab.get(auth_url + "?" + urlencode(params))
                    assert tab.run_js("return document.querySelector('form').action") == auth_url
                    cookies = tab.run_cdp("Network.getCookies", urls=[auth_url])["cookies"]
                    oauth = [cookie for cookie in cookies if cookie["name"] == "cb_oauth"]
                    assert len(oauth) == 1
                    assert oauth[0]["path"] == cfg.authorization_path
                    assert oauth[0]["httpOnly"] and oauth[0]["sameSite"] == "Lax"
                    root_cookies = tab.run_cdp(
                        "Network.getCookies", urls=[cfg.public_origin + "/authorize"]
                    )["cookies"]
                    assert not any(cookie["name"] == "cb_oauth" for cookie in root_cookies)
                    tab.ele("css:input[name=password]").input("test administrator password")
                    tab.ele("css:button").click()
                    # Success also proves Chrome's native POST Origin matched the
                    # pure origin, not public_base; the server rejects all others.
                    assert arrived.wait(5), "Prefixed browser form did not reach local callback"
                    remaining = tab.run_cdp("Network.getCookies", urls=[auth_url])["cookies"]
                    assert not any(cookie["name"] == "cb_oauth" for cookie in remaining)
                finally:
                    browser.quit()

            # HTTP servers run on this event loop while Chrome uses a worker thread.
            await asyncio.to_thread(browser_form)
            assert len(arrivals) == 1
            method, path, headers = arrivals[0]
            assert method == "GET" and "Content-Length" not in headers
            assert "Referer" not in headers and "Cookie" not in headers
            result = parse_qs(urlsplit(path).query)
            assert set(result) == {"code", "state", "iss"}
            assert result["iss"] == [cfg.issuer] and result["state"] == [params["state"]]
            form = {
                "grant_type": "authorization_code",
                "code": result["code"][0],
                "code_verifier": verifier,
                "client_id": cfg.oauth_client_id,
                "resource": cfg.resource,
                "redirect_uri": params["redirect_uri"],
            }
            async with httpx.AsyncClient(trust_env=False) as client:
                token = await client.post(cfg.public_base + "/token", data=form)
                assert token.status_code == 200
                assert (await client.post(cfg.public_base + "/token", data=form)).status_code == 400
    finally:
        await asyncio.to_thread(receiver.shutdown)
        receiver.server_close()
        receiver_thread.join(timeout=3)
        assert not receiver_thread.is_alive()
