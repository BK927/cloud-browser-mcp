from cloud_browser.node_registry import NodeRegistry


def remember(registry, backend, value="a", root="work-tab", owner="frame", document="doc"):
    meta = {"tag": "input", "value": value, "options": [{"value": value}]}
    node_id = registry.remember(root, owner, document, backend, value, meta)
    return node_id, meta


def test_registry_keeps_actual_handles_across_scoped_results_and_freezes_state():
    registry = NodeRegistry()
    alpha, metadata = remember(registry, 1)
    beta, _ = remember(registry, 2)
    metadata["options"][0]["value"] = "mutated outside registry"
    assert registry.get(alpha, "work-tab").metadata["options"][0]["value"] == "a"
    assert registry.get(beta, "work-tab").backend == 2
    assert registry.get(beta, "other-tab") is None
    same, _ = remember(registry, 1)
    assert same == alpha


def test_changed_backend_or_state_never_silently_retargets_an_old_handle():
    registry = NodeRegistry()
    original, _ = remember(registry, 1)
    changed_state, _ = remember(registry, 1, "b")
    replacement, _ = remember(registry, 2)
    assert len({original, changed_state, replacement}) == 3
    assert registry.get(original, "work-tab").signature == "a"
    assert registry.get(original, "work-tab").backend == 1


def test_registry_budget_is_shared_and_lru_evicts_only_primitive_metadata():
    registry = NodeRegistry(max_bytes=1200)
    old, _ = remember(registry, 1, root="first-tab")
    other, _ = remember(registry, 2, root="second-tab")
    registry.get(old, "first-tab")
    latest, _ = remember(registry, 3, root="second-tab")
    assert registry.bytes <= 1200
    assert registry.get(old, "first-tab") is not None
    assert registry.get(latest, "second-tab") is not None
    assert registry.get(other, "second-tab") is None
    assert registry.missing_reason(other) == "registry_evicted"


def test_owner_document_lifetime_invalidates_previous_handles():
    registry = NodeRegistry()
    old, _ = remember(registry, 1)
    registry.synchronize({("work-tab", "frame"): "new-document"})
    assert registry.get(old, "work-tab") is None
    new, _ = remember(registry, 1, document="new-document")
    assert new != old
    registry.synchronize({})
    assert not registry.records and registry.bytes == 0


def test_one_oversized_record_does_not_break_the_bound():
    registry = NodeRegistry(max_bytes=600)
    oversized, _ = remember(registry, 1, value="x" * 1000)
    assert registry.get(oversized, "work-tab") is None
    assert registry.bytes == 0
    assert registry.missing_reason(oversized) == "registry_evicted"


def test_inflight_target_survives_bounded_recapture_without_bypassing_the_budget():
    registry = NodeRegistry(max_bytes=1200)
    target, _ = remember(registry, 1)
    registry.pinned.add(target)
    for backend in range(2, 20):
        remember(registry, backend)
        assert registry.bytes <= 1200
    assert registry.get(target, "work-tab") is not None
    registry.pinned.clear()
    for backend in (20, 21):
        remember(registry, backend)
    assert registry.get(target, "work-tab") is None
