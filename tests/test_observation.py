import json

import pytest

from cloud_browser.observation import compact_node, paginate


def test_compact_nodes_preserve_action_state():
    compact = compact_node({
        "node_id": "node_one", "tag": "input", "name": "Consent", "type": "checkbox",
        "checked": False, "disabled": False, "focused": False, "value": None,
        "rect": {"x": 12.123456, "y": 0, "width": 20.5, "height": 20},
    })
    assert compact["checked"] is False
    assert "disabled" not in compact and "value" not in compact
    assert compact["rect"]["x"] == 12.12


@pytest.mark.parametrize("budget", [256, 1000, 4000, 30000])
def test_pagination_complete_json_no_text_loss_and_no_node_starvation(budget):
    text = ("# Main article\n한국어 본문 and useful information.\n" * 2000)
    nodes = [{"node_id": f"node_{i}", "tag": "button", "name": "검색 " + str(i)} for i in range(30)]
    snapshot = {"semantic": text, "nodes": nodes}
    offsets = (0, 0)
    parts, ids = [], []
    for index in range(5000):
        page, new_offsets = paginate(snapshot, offsets, budget)
        assert len(page["semantic_snapshot"]) + len(page["interactive_snapshot"]) <= budget
        decoded = [json.loads(line) for line in page["interactive_snapshot"].splitlines()]
        if index == 0:
            assert decoded and page["semantic_snapshot"]
        ids.extend(row["node_id"] for row in decoded)
        parts.append(page["semantic_snapshot"])
        assert page == paginate(snapshot, offsets, budget)[0]
        assert new_offsets != offsets
        offsets = new_offsets
        if not page["truncated"]:
            break
    else:
        pytest.fail("Pagination did not terminate")
    assert "".join(parts) == text
    assert ids == [node["node_id"] for node in nodes]


def test_tiny_budget_shortens_details_not_node_identity():
    page, _ = paginate({"semantic": "", "nodes": [{
        "node_id": "node_real", "tag": "a", "name": "\\\"긴 이름" * 1000,
        "href": "https://example.com/" + "x" * 10000,
    }]}, (0, 0), 256)
    row = json.loads(page["interactive_snapshot"])
    assert row["node_id"] == "node_real" and row["details_omitted"]
    assert len(page["interactive_snapshot"]) <= 256
    assert not page["truncated"]


def test_very_long_custom_tag_cannot_overrun_tiny_budget():
    page, _ = paginate({"semantic": "", "nodes": [{
        "node_id": "node_custom", "tag": "custom-" + "x" * 10000, "name": "Control",
    }]}, (0, 0), 256)
    assert len(page["interactive_snapshot"]) <= 256
    assert json.loads(page["interactive_snapshot"])["node_id"] == "node_custom"


def test_empty_snapshot_does_not_create_phantom_cursor():
    result, offsets = paginate({"semantic": "", "nodes": []}, (0, 0), 256)
    assert not result["truncated"] and offsets == (0, 0)
