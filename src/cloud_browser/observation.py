"""Compact, deterministic observation pages. No page code or browser access here."""

import json


def compact_node(node: dict) -> dict:
    """Keep actionable identity and state; omit empty/default presentation noise."""
    result = {"node_id": node["node_id"], "tag": node["tag"], "name": node.get("name", "")}
    for key, value in node.items():
        if key in result or value is None or value == "":
            continue
        if (
            key
            in (
                "disabled",
                "editable",
                "focused",
                "readonly",
                "multiple",
                "scrollable",
                "options_truncated",
            )
            and value is False
        ):
            continue
        if key == "rect":
            value = {axis: round(number, 2) for axis, number in value.items()}
        result[key] = value
    return result


def _line(node: dict, budget: int) -> str:
    def encode(item):
        return json.dumps(item, ensure_ascii=False, separators=(",", ":"))

    text = encode(node)
    if len(text) <= budget:
        return text
    # A tiny max_chars must not create broken JSON or lose the node's identity.
    # Full metadata remains server-side for exact targeting and approval review.
    reduced = {
        "node_id": node["node_id"],
        "tag": node["tag"][:64],
        "name": node.get("name", ""),
        "details_omitted": True,
    }
    while len(encode(reduced)) > budget and reduced["name"]:
        reduced["name"] = reduced["name"][: len(reduced["name"]) // 2]
    return encode(reduced)


def paginate(snapshot: dict, offsets: tuple[int, int], budget: int) -> tuple[dict, tuple[int, int]]:
    """Share one character budget fairly; interactive rows are always complete JSON.

    Offsets are independent: a long article cannot starve the visible controls.
    The caller binds this snapshot, budget and offsets to a revision-bound cursor.
    """
    semantic = snapshot["semantic"]
    nodes = snapshot["nodes"]
    so, ni = offsets
    rows = []
    used = 0
    reserve = budget if so >= len(semantic) else budget // 2
    first = _line(nodes[ni], budget) if ni < len(nodes) else None
    if first is not None:
        # Guarantee progress even when the first row exceeds the nominal share.
        rows.append(first)
        used = len(first)
        ni += 1
        while ni < len(nodes):
            candidate = _line(nodes[ni], budget)
            if used + 1 + len(candidate) > reserve:
                break
            rows.append(candidate)
            used += 1 + len(candidate)
            ni += 1
    remaining = max(0, budget - used)
    end = min(len(semantic), so + remaining)
    if end < len(semantic) and end > so:
        boundary = semantic.rfind("\n", so, end)
        if boundary >= so + remaining // 2:
            end = boundary + 1
    result = {
        "semantic_snapshot": semantic[so:end],
        "interactive_snapshot": "\n".join(rows),
        "truncated": end < len(semantic) or ni < len(nodes),
        "next_cursor": None,
        "semantic_truncated": end < len(semantic),
        "interactive_page_truncated": ni < len(nodes),
    }
    return result, (end, ni)
