"""Local public-page fixtures; no private URL override exists outside these tests."""

import asyncio
import base64
import http.server
import io
import os
import threading
import time
from urllib.parse import urlsplit

import pytest
from conftest import fake_worker_resources
from PIL import Image

from cloud_browser.drission import DrissionAdapter
from cloud_browser.models import BrowserError
from cloud_browser.service import BrowserService
from cloud_browser.store import Store

pytestmark = pytest.mark.browser


class ReaderPages(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        path = urlsplit(self.path).path
        content_type = "text/html; charset=utf-8"
        if path == "/hit.png":
            self.server.image_hits.append(self.path)
            body = base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jWZkAAAAASUVORK5CYII="
            )
            content_type = "image/png"
        elif path == "/images":
            body = (
                "<!doctype html><title>Image article</title><main><h1>Image article</h1>"
                f'<img src="/hit.png?{urlsplit(self.path).query}" width="64" height="64">'
                "<p>Text with image.</p></main>"
            ).encode()
        elif path == "/frame-parent":
            body = (
                "<!doctype html><title>Cross-site frame</title><main>Parent article"
                f'<iframe src="http://localhost:{self.server.server_port}/frame-child"></iframe></main>'
            ).encode()
        elif path == "/frame-child":
            body = b"<!doctype html><title>Child</title><main>Frame article</main>"
        elif path == "/slow.png":
            time.sleep(12)
            body = b""
        elif path == "/slow":
            body = b'<main><h1>Interactive article</h1><p>Text before load.</p><img src="/slow.png"></main>'
        elif path == "/cookie":
            body = b"<main>Cookie installed</main>"
        elif path == "/echo":
            cookie = "reader_fixture=retained" in self.headers.get("Cookie", "")
            body = f"<main>Cookie present: {cookie}</main>".encode()
        elif path == "/many":
            body = (
                "<main>"
                + "".join(f'<a href="/article?no={i}">Link {i}</a>' for i in range(205))
                + "</main>"
            ).encode()
        else:
            body = b"""<title>Public article</title><header>Excluded header</header><main>
            <h1>Reader article</h1><p id="scope">Scoped text <a href="/article?blogId=someuser&logNo=223456789012">Blog</a></p>
            <a href="/article?list=PLx0sYbCqOb8TBPRdmBHs5Iftvv9TPboYG">Playlist</a>
            <a href="/article?access_token=privatevalue">Secret link</a>
            <a href="/article?list=PLx0sYbCqOb8TBPRdmBHs5Iftvv9TPboYG">Duplicate</a>
            <a style="display:none" href="/hidden">Hidden link</a>
            <form><input type="password"><a href="/protected">Protected link text</a></form>
            <footer><a href="/footer">Excluded footer</a></footer></main>"""
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if path == "/cookie":
            self.send_header(
                "Set-Cookie", "reader_fixture=retained; Max-Age=3600; Path=/; SameSite=Lax"
            )
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


@pytest.fixture
async def reader_browser(cfg, monkeypatch, request):
    executable = os.getenv("CB_TEST_CHROMIUM")
    if not executable:
        pytest.skip("Set CB_TEST_CHROMIUM to run reader Chromium tests")
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ReaderPages)
    server.image_hits = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"

    def fixture_url_only(url, *, dns_proxy=None):
        parsed = urlsplit(url)
        if (
            dns_proxy is not None
            or parsed.scheme != "http"
            or parsed.hostname not in ("127.0.0.1", "localhost")
            or parsed.port != server.server_port
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise BrowserError("INVALID_URL", "Local reader fixture only")

    monkeypatch.setattr("cloud_browser.service.validate_url", fixture_url_only)
    monkeypatch.setattr("cloud_browser.drission.validate_url", fixture_url_only)
    cfg = cfg.model_copy(
        update={
            "chromium_path": executable,
            "headless": True,
            "browser_proxy": "",
            "reader_timeout": 5,
            "reader_site_isolation": getattr(request, "param", True),
        }
    )
    adapter = DrissionAdapter(cfg)

    class AdapterWorker:
        def __init__(self):
            self.adapter = adapter
            self.fixture_server = server

        async def call(self, method, **args):
            return await asyncio.to_thread(getattr(adapter, method), **args)

        async def shutdown(self):
            await asyncio.to_thread(adapter.shutdown)

    store = Store(cfg.data_dir / "reader-browser.sqlite")
    service = BrowserService(cfg, store, AdapterWorker())
    service.resources = fake_worker_resources
    try:
        yield service, base
    finally:
        await service.shutdown()
        store.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


async def test_reader_text_links_selector_and_cap(reader_browser):
    service, base = reader_browser
    result = await service.call("read", _principal="fixture", url=base + "/article?blogId=someuser")
    assert result["status"] == "ok", result
    assert result["page"]["url"] == base + "/article?blogId=someuser"
    assert "Reader article" in result["read"]["text"]
    assert "Excluded header" not in result["read"]["text"]
    assert "Protected link text" not in result["read"]["text"]
    assert result["read"]["protected_regions_omitted"] is True
    links = result["read"]["links"]
    assert links == [
        {"text": "Blog", "url": base + "/article?blogId=someuser&logNo=223456789012"},
        {"text": "Playlist", "url": base + "/article?list=PLx0sYbCqOb8TBPRdmBHs5Iftvv9TPboYG"},
        {"text": "Secret link", "url": base + "/article?access_token=%5BREDACTED%5D"},
    ]
    scoped = await service.call(
        "read", _principal="fixture", url=base + "/article", selector="#scope"
    )
    assert scoped["status"] == "ok", scoped
    assert (
        "Scoped text" in scoped["read"]["text"] and "Reader article" not in scoped["read"]["text"]
    )
    assert scoped["read"]["links"] == links[:1]
    invalid = await service.call("read", _principal="fixture", url=base + "/article", selector="[")
    assert invalid["error"]["code"] == "INVALID_SELECTOR"
    many = await service.call("read", _principal="fixture", url=base + "/many")
    assert len(many["read"]["links"]) == 200 and many["read"]["links_truncated"] is True


async def test_reader_cookie_persists_after_idle_close_and_isolated_from_work(reader_browser):
    service, base = reader_browser
    installed = await service.call("read", _principal="fixture", url=base + "/cookie")
    assert installed["status"] == "ok", installed
    sid = service.reader_sid
    service.sessions[sid]["expires"] = time.time() - 1
    async with service.lock:
        await service._reap_expired()
    assert service.reader_sid is None and (service.cfg.data_dir / "profiles" / "reader").is_dir()
    echoed = await service.call("read", _principal="fixture", url=base + "/echo")
    assert echoed["status"] == "ok", echoed
    assert "Cookie present: True" in echoed["read"]["text"]
    opened = await service.call("open", _principal="fixture", url=base + "/echo")
    if opened.get("navigation", {}).get("pending"):
        await service.navigations[opened["session_id"]]["done"].wait()
    observed = await service.call(
        "observe",
        _principal="fixture",
        session_id=opened["session_id"],
        tab_id=opened["tab_id"],
        lease_id=opened["lease_id"],
        mode="semantic",
    )
    assert observed["status"] == "ok", observed
    assert "Cookie present: False" in observed["observation"]["semantic_snapshot"]


async def test_reader_interactive_readiness_avoids_delayed_load(reader_browser):
    service, base = reader_browser
    warmed = await service.call("read", _principal="fixture", url=base + "/article")
    assert warmed["status"] == "ok", warmed
    started = time.monotonic()
    result = await service.call("read", _principal="fixture", url=base + "/slow")
    elapsed = time.monotonic() - started
    assert result["status"] == "ok", result
    assert result["read"]["complete"] is True
    assert "Text before load." in result["read"]["text"]
    assert elapsed < service.cfg.reader_timeout


async def test_reader_images_load_by_default_and_can_be_disabled_between_reads(reader_browser):
    service, base = reader_browser
    hits = service.worker.fixture_server.image_hits
    disabled = await service.call("read", url=base + "/images?disabled", images=False)
    assert disabled["status"] == "ok", disabled
    assert "Text with image." in disabled["read"]["text"]
    assert hits == [] and disabled["read"]["loaded"] == []
    assert disabled["read"]["blocked_requests"] >= 1
    default = await service.call("read", url=base + "/images?default")
    assert default["status"] == "ok", default
    assert default["read"]["loaded"] == ["images"]
    assert hits == ["/hit.png?default"]
    again = await service.call("read", url=base + "/images?disabled-again", images=False)
    assert again["status"] == "ok", again
    assert hits == ["/hit.png?default"] and again["read"]["blocked_requests"] >= 1


async def test_reader_screenshot_is_viewport_jpeg(reader_browser):
    service, base = reader_browser
    result = await service.call("read", url=base + "/images?screenshot", screenshot=True)
    assert result["status"] == "ok", result
    assert "Text with image." in result["read"]["text"]
    assert result["read"]["loaded"] == ["fonts", "images"]
    assert "screenshot_omitted" not in result["read"]
    assert result["_image"]["mimeType"] == "image/jpeg"
    with Image.open(io.BytesIO(base64.b64decode(result["_image"]["data"]))) as shot:
        assert shot.format == "JPEG" and shot.size == (1024, 768)
    cached = await service.call("read", read_id=result["read"]["read_id"], screenshot=True)
    assert cached["status"] == "ok" and "_image" not in cached


@pytest.mark.parametrize("reader_browser", [True, False], indirect=True)
async def test_reader_cross_site_iframe_target_depends_on_isolation(reader_browser):
    service, base = reader_browser
    result = await service.call("read", url=base + "/frame-parent")
    assert result["status"] == "ok", result
    adapter = service.worker.adapter
    tab = adapter.sessions[service.reader_sid]["tabs"][service.reader_tid].tab
    child_url = f"http://localhost:{service.worker.fixture_server.server_port}/frame-child"
    browser = adapter.sessions[service.reader_sid]["browser"]

    def frame_ready_and_targets():
        tree = tab.run_cdp("Page.getFrameTree")["frameTree"]
        children = tree.get("childFrames", [])
        ready = any(frame["frame"]["url"] == child_url for frame in children)
        targets = browser._run_cdp("Target.getTargets")["targetInfos"]
        separate = any(info["type"] == "iframe" and info["url"] == child_url for info in targets)
        # Some Chromium versions omit remote children from the page target's tree.
        return ready or separate, separate

    deadline = time.monotonic() + 5
    while True:
        ready, separate = await asyncio.to_thread(frame_ready_and_targets)
        if ready and (separate or not service.cfg.reader_site_isolation):
            break
        if time.monotonic() >= deadline:
            break
        await asyncio.sleep(0.05)
    assert ready, "Cross-site iframe did not finish loading"
    assert separate is service.cfg.reader_site_isolation
