import base64
import io
import json
import time

import pytest
from PIL import Image
from test_browser import browser as browser
from test_browser import node

from cloud_browser.models import BrowserError

pytestmark = pytest.mark.browser


def test_real_main_first_no_duplicate_text_and_complete_cursor_rows(browser):
    adapter, sid, tid, _ = browser
    state = adapter._tab(sid, tid)
    state.tab.run_js("""
        document.body.innerHTML = '<nav>NOISY NAVIGATION</nav><main><h1>Main story</h1>' +
            '<button>Visible action</button><p>Unique introduction sentence.</p>' +
            '<p>Useful paragraph with meaningful information.</p>'.repeat(80) +
            '</main><footer>NOISY FOOTER</footer>';
    """)
    full = adapter.observe(sid, tid, mode="semantic", max_chars=100000)["observation"][
        "semantic_snapshot"
    ]
    assert full.startswith("# Main story")
    assert full.count("Unique introduction sentence.") == 1
    assert "NOISY" not in full and "StaticText" not in full
    first = adapter.observe(sid, tid, mode="auto", max_chars=500)
    current = first
    text, nodes = [], []
    seen_cursor = None
    for index in range(100):
        obs = current["observation"]
        rows = [json.loads(line) for line in obs["interactive_snapshot"].splitlines()]
        assert len(obs["semantic_snapshot"]) + len(obs["interactive_snapshot"]) <= 500
        if index == 0:
            assert any(row["name"] == "Visible action" for row in rows)
        text.append(obs["semantic_snapshot"])
        nodes.extend(row["node_id"] for row in rows)
        cursor = obs["next_cursor"]
        if not cursor:
            break
        seen_cursor = cursor
        current = adapter.observe(sid, tid, cursor=cursor)
    else:
        pytest.fail("Cursor failed to finish")
    assert "".join(text) == full
    assert len(nodes) == len(set(nodes))
    repeated = adapter.observe(sid, tid, cursor=seen_cursor)
    assert (
        repeated["observation"]["semantic_snapshot"] == current["observation"]["semantic_snapshot"]
    )


def test_real_declared_link_and_form_destinations_are_separate_and_redacted(browser):
    adapter, sid, tid, _ = browser
    adapter._tab(sid, tid).tab.run_js("""
        document.body.innerHTML = '<a href="https://iana.org/domains/example">Learn more</a>' +
            '<form method="post" action="https://example.com/submit?token=private-value">' +
            '<input aria-label="Text"><button type="submit">Publish</button></form>';
    """)
    obs, link = node(adapter, sid, tid, "Learn more")
    result = adapter.prepare(
        sid, tid, obs["revision"], {"type": "click", "node_id": link["node_id"]}
    )
    assert result["destination"] == "https://iana.org/domains/example"
    assert result["destination"] != result["page"]["url"]
    obs, button = node(adapter, sid, tid, "Publish")
    result = adapter.prepare(
        sid, tid, obs["revision"], {"type": "click", "node_id": button["node_id"]}
    )
    assert result["destination_kind"] == "declared_form"
    assert "private-value" not in json.dumps(result)
    assert "private-value" not in json.dumps(obs)
    assert "form_action" not in json.dumps(obs)


def test_real_iframe_masking_is_opt_in_and_masked_coordinates_are_blocked(browser):
    adapter, sid, tid, _ = browser
    adapter.cfg.iframe_screenshot_policy = "block"
    adapter._tab(sid, tid).tab.run_js("""
        document.body.insertAdjacentHTML('beforeend',
            '<div style="position:fixed;left:0;top:0;width:30px;height:30px;background:lime;z-index:10"></div>' +
            '<iframe srcdoc="<body style=background:red>EMBEDDED PRIVATE</body>" ' +
            'style="position:fixed;left:650px;top:20px;width:180px;height:120px;border:0;z-index:10"></iframe>');
    """)
    time.sleep(0.2)
    with pytest.raises(BrowserError) as exc:
        adapter.observe(sid, tid, mode="visual")
    assert exc.value.code == "SENSITIVE_SCREEN"
    adapter.cfg.iframe_screenshot_policy = "mask"
    shot = adapter.observe(sid, tid, mode="visual")
    assert shot["_image"]["mimeType"] == "image/png"
    image = Image.open(io.BytesIO(base64.b64decode(shot["_image"]["data"])))
    assert image.getpixel((700, 60)) == (0, 0, 0)
    assert image.getpixel((10, 10))[1] > 200
    assert shot["observation"]["screenshot"]["masked_regions"]
    with pytest.raises(BrowserError) as exc:
        adapter.prepare(
            sid,
            tid,
            shot["revision"],
            {
                "type": "click_at",
                "x": 700,
                "y": 60,
                "screenshot_id": shot["observation"]["screenshot"]["screenshot_id"],
            },
        )
    assert exc.value.code == "SENSITIVE_SCREEN"
    full = adapter.observe(sid, tid, mode="visual", full_page=True)
    assert full["observation"]["screenshot"]["masked_regions"]
    adapter._tab(sid, tid).tab.run_js(
        "document.querySelector('iframe').style.transform='rotate(5deg)'"
    )
    with pytest.raises(BrowserError) as exc:
        adapter.observe(sid, tid, mode="visual")
    assert exc.value.code == "SENSITIVE_SCREEN"


def test_real_aria_disabled_and_native_details(browser):
    adapter, sid, tid, _ = browser
    adapter._tab(sid, tid).tab.run_js("""
        document.body.innerHTML = '<button aria-disabled="true">Disabled action</button>' +
            '<details open><summary>Read details</summary><p>Details body</p></details>';
    """)
    obs, disabled = node(adapter, sid, tid, "Disabled action")
    with pytest.raises(BrowserError) as exc:
        adapter.prepare(
            sid, tid, obs["revision"], {"type": "click", "node_id": disabled["node_id"]}
        )
    assert exc.value.code == "NODE_NOT_ACTIONABLE"
    obs, summary = node(adapter, sid, tid, "Read details")
    assert summary["expanded"] == "true"
    adapter.act(sid, tid, obs["revision"], {"type": "click", "node_id": summary["node_id"]})
    assert node(adapter, sid, tid, "Read details")[1]["expanded"] == "false"
