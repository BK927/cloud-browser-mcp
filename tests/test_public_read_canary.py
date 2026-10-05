import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from cloud_browser.models import BrowserError

_spec = importlib.util.spec_from_file_location(
    "public_read_canary", Path(__file__).parents[1] / "scripts" / "public_read_canary.py"
)
canary = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(canary)


def test_metrics_never_copy_content_images_errors_urls_or_cgroup_paths():
    sentinel = "secret-must-not-be-written"
    result = {
        "page": {"url": "https://bad.example/?token=" + sentinel, "title": sentinel},
        "_image": {"data": sentinel, "mimeType": "image/png"},
        "observation": {
            "semantic_snapshot": sentinel,
            "interactive_snapshot": json.dumps({"name": sentinel, "href": sentinel}),
            "frames": [{"readable": False, "reason": sentinel, "url": sentinel}],
            "query_empty_reason": sentinel,
            "screenshot_omitted": {"code": sentinel, "message": sentinel},
        },
    }
    row = canary.observation_metrics(result, mode="auto")
    row["memory"] = canary.numeric_memory(
        {
            "cgroup_path": sentinel,
            "accounting": sentinel,
            "available_mb": 100,
            "cgroup_used_mb": sentinel,
            "host_available_mb": float("nan"),
            "reserve_mb": True,
            "admission_mb": 2**10000,
        }
    )
    text = json.dumps(row)
    assert sentinel not in text
    assert "bad.example" not in text
    assert row["optional_image_reason"] == "UNCLASSIFIED_ERROR"
    assert row["query_empty_reason"] == "unclassified"
    assert row["memory"]["available_mb"] == 100
    assert row["memory"]["cgroup_used_mb"] is None
    assert row["memory"]["admission_mb"] is None
    assert row["unreadable_frame_count"] == 1


def test_capture_report_is_fixed_reasons_and_bounded_attempts_only():
    row = canary.capture_metrics(
        {
            "capture_attempts": 2,
            "capture_reasons": ["scroll", "privacy_history", "PRIVATE", {"token": "PRIVATE"}],
            "url": "PRIVATE",
        }
    )
    assert row == {"capture_attempts": 2, "capture_reasons": ["scroll", "privacy_history"]}
    assert "PRIVATE" not in json.dumps(row)
    assert canary.capture_metrics({"capture_attempts": True, "capture_reasons": "PRIVATE"}) == {
        "capture_attempts": None,
        "capture_reasons": [],
    }


@pytest.mark.parametrize(
    "observation,reason",
    [
        ({"semantic_snapshot": ""}, "EMPTY_SEMANTIC_TEXT"),
        ({"semantic_snapshot": "some text", "query_match_count": 0}, "SCOPED_QUERY_EMPTY"),
        ({"semantic_snapshot": "some text", "query_empty_reason": "hidden"}, "SCOPED_QUERY_EMPTY"),
        (
            {"semantic_snapshot": "some text", "query_empty_reason": {"not": "a code"}},
            "SCOPED_QUERY_EMPTY",
        ),
    ],
)
def test_missing_scope_or_text_is_not_success(observation, reason):
    result = canary.observation_metrics({"observation": observation}, mode="semantic", scoped=True)
    assert result["ok"] is False
    assert result["reason"] == reason


def test_partial_text_is_success_but_explicitly_partial_and_visual_needs_pixels():
    result = {"observation": {"semantic_snapshot": "bounded text", "truncated": True}}
    assert canary.observation_metrics(result, mode="auto")["partial"] is True
    assert canary.observation_metrics(result, mode="auto")["ok"] is True
    assert canary.observation_metrics(result, mode="visual")["ok"] is False


def test_dc_article_link_numbers_counted_but_never_serialized():
    row = canary.dc_link_counts(
        "\n".join(
            json.dumps({"href": href})
            for href in (
                "https://gall.dcinside.com/board/view/?id=programming&no=2940536",
                "https://gall.dcinside.com/board/view/?id=programming&no=[REDACTED]",
                "https://bad.example/board/view/?id=programming&no=123",
                "https://gall.dcinside.com:444/board/view/?id=programming&no=123",
            )
        )
    )
    assert row == {"samples": 2, "numeric_no_preserved": 1, "all_preserved": False}
    assert "2940536" not in json.dumps(row)


def test_no_link_sample_is_explicit_failure_not_proof_of_preservation():
    row = canary.link_metrics({"observation": {"interactive_snapshot": ""}})
    assert row["ok"] is False
    assert row["reason"] == "PUBLIC_ARTICLE_LINK_NOT_OBSERVED"


@pytest.mark.parametrize(
    "case,reason,navigated",
    [
        ("success", None, True),
        ("canonical_page", None, True),
        ("no_eligible", "ELIGIBLE_PUBLIC_ARTICLE_LINK_NOT_OBSERVED", False),
        ("redacted", "ELIGIBLE_PUBLIC_ARTICLE_LINK_NOT_OBSERVED", False),
        ("unknown_query", "ELIGIBLE_PUBLIC_ARTICLE_LINK_NOT_OBSERVED", False),
        ("redacted_page", "ELIGIBLE_PUBLIC_ARTICLE_LINK_NOT_OBSERVED", False),
        ("credentials", "ELIGIBLE_PUBLIC_ARTICLE_LINK_NOT_OBSERVED", False),
        ("different_article", "OBSERVED_LINK_DIFFERENT_ARTICLE", True),
        ("no_body", "OBSERVED_LINK_BODY_NOT_READABLE", True),
        ("hidden_body", "OBSERVED_LINK_BODY_NOT_READABLE", True),
    ],
)
def test_observed_dc_link_roundtrip_uses_returned_url_and_actual_rendered_body(
    case, reason, navigated
):
    observed = "https://gall.dcinside.com/board/view/?id=programming&no=612345"
    candidate = observed
    if case == "canonical_page":
        candidate += "&page=2"
    elif case == "no_eligible":
        candidate = candidate.replace("gall.dcinside.com", "untrusted.example")
    elif case == "redacted":
        candidate = candidate.replace("612345", "[REDACTED]")
    elif case == "unknown_query":
        candidate += "&token=private-value"
    elif case == "redacted_page":
        candidate += "&page=[REDACTED]"
    elif case == "credentials":
        candidate = candidate.replace("https://", "https://private-user:private-password@")
    calls = []

    class Adapter:
        def navigate(self, sid, tid, operation, href):
            calls.append((operation, href))
            assert href == candidate
            actual = (
                observed.replace("612345", "612346") if case == "different_article" else observed
            )
            return {"page": {"url": actual, "title": "private-page-title"}}

        def observe(self, sid, tid, **kwargs):
            calls.append(("observe", kwargs))
            assert kwargs == {
                "mode": "semantic",
                "max_chars": 4000,
                "query": {"selector": ".write_div", "limit": 20},
            }
            return {
                "observation": {
                    "semantic_snapshot": "" if case == "no_body" else "private-body-not-for-report",
                    "query_match_count": 0 if case == "no_body" else 1,
                    "query_empty_reason": "hidden" if case == "hidden_body" else None,
                }
            }

    snapshot = json.dumps({"href": candidate, "name": "private-link-name"})
    if case == "no_eligible":
        snapshot = ""
    row = canary.roundtrip_observed_dc_link(
        Adapter(), "test-session", "test-tab", {"observation": {"interactive_snapshot": snapshot}}
    )
    assert row["reason"] == reason
    assert row["ok"] is (reason is None)
    assert row["observed_link_navigated"] is navigated
    assert row["eligible_link_count"] == int(navigated)
    assert bool(calls) is navigated
    assert row["same_article"] is (navigated and case != "different_article")
    if case == "different_article":
        assert len(calls) == 1
    report = json.dumps(row)
    assert "612345" not in report
    assert "612346" not in report
    assert "dcinside.com" not in report
    assert "private" not in report


@pytest.mark.parametrize(
    "suffix",
    [
        "&no=123",
        "&page=1&page=2",
        "&page=１２",
        "&page=1#private-fragment",
        "&unknown=",
        "&password=private-password",
    ],
)
def test_dc_roundtrip_identity_rejects_ambiguous_or_untrusted_query(suffix):
    assert (
        canary.dc_article_identity(
            "https://gall.dcinside.com/board/view/?id=programming&no=612345" + suffix
        )
        is None
    )


def test_canary_roundtrip_counts_real_returned_link_but_list_visual_precedes_navigation():
    observed = "https://gall.dcinside.com/board/view/?id=programming&no=612345&page=2"
    calls = []

    class Adapter:
        def __init__(self, cfg):
            self.current = None

        def open(self, sid):
            return {"tab_id": "test-tab"}

        def navigate(self, sid, tid, operation, url):
            assert url in {target[1] for target in canary.TARGETS} | {observed}
            self.current = url
            calls.append(("navigate", url))
            return {"page": {"url": url}}

        def observe(self, sid, tid, **kwargs):
            calls.append((kwargs["mode"], self.current))
            return {
                "_image": {"data": "private-image-not-for-report"},
                "observation": {
                    "semantic_snapshot": "private-rendered-body",
                    "interactive_snapshot": json.dumps({"href": observed}),
                    "query_match_count": 1,
                },
            }

        def shutdown(self):
            pass

    report = canary.run_canary(
        "not-launched",
        repeats=1,
        visual=True,
        adapter_factory=Adapter,
        memory_provider=lambda _: {},
    )
    row = report["repeats"][0]["pages"][0]["link_roundtrip"]
    assert row["ok"] is True
    assert row["eligible_link_count"] == 1
    assert row["same_article"] is True
    assert row["body_present"] is True
    assert row["elapsed_ms"] >= 0
    assert report["ok"] is True
    assert calls.index(("visual", canary.TARGETS[0][1])) < calls.index(("navigate", observed))
    assert observed not in json.dumps(report)
    assert "612345" not in json.dumps(report)
    assert "private" not in json.dumps(report)


def test_route_comparison_checks_exact_public_fields_without_copying_extra_query():
    expected = canary.TARGETS[1][1]
    assert canary.same_public_route(expected + "&token=not-recorded", expected)
    assert not canary.same_public_route(expected.replace("2940536", "123"), expected)
    assert not canary.same_public_route(expected.replace("https://", "http://"), expected)
    assert not canary.same_public_route("https://gall.dcinside.com:444/board/view/", expected)


def test_disposable_adapter_shuts_down_after_open_error_and_settings_do_not_load_env(monkeypatch):
    instances = []
    monkeypatch.setenv("CB_ADMIN_PASSWORD_HASH", "must-not-use-real-credentials")
    monkeypatch.setenv("CB_DATA_DIR", "must-not-reuse-a-profile")
    monkeypatch.setenv("CB_BROWSER_PROXY", "http://must-not-use-production-proxy")

    class Adapter:
        def __init__(self, cfg):
            self.cfg = cfg
            self.closed = False
            instances.append(self)

        def open(self, sid):
            raise BrowserError("NAVIGATION_FAILED", "sensitive-error-value")

        def shutdown(self):
            self.closed = True

    report = canary.run_canary(
        "local-test-executable",
        repeats=1,
        adapter_factory=Adapter,
        memory_provider=lambda _: {"available_mb": 123},
    )
    assert instances[0].closed
    assert report["ok"] is False
    cleanup = report["repeats"][0]["cleanup"]
    assert cleanup["ok"] is True
    assert cleanup["browser_shutdown_returned"] is True
    assert cleanup["process_exit_verified"] is None
    assert cleanup["temporary_cleanup_attempts"] == 1
    assert report["environment"]["browser_shutdown_metric"] == "adapter-method-return-only"
    assert report["repeats"][0]["startup"]["reason"] == "NAVIGATION_FAILED"
    assert not instances[0].cfg.data_dir.exists()
    assert instances[0].cfg.admin_password_hash == ""
    assert instances[0].cfg.browser_proxy == ""
    assert instances[0].cfg.auth_rules == {}
    assert not instances[0].cfg.manual_control_enabled
    assert not instances[0].cfg.network_isolated
    assert "sensitive-error-value" not in json.dumps(report)


def test_read_only_canary_reports_failed_description_and_discards_all_images():
    instances = []

    class Adapter:
        def __init__(self, cfg):
            self.calls = []
            self.closed = False
            instances.append(self)

        def open(self, sid):
            return {"tab_id": "test-tab"}

        def navigate(self, sid, tid, operation, url):
            assert url in {target[1] for target in canary.TARGETS}
            self.calls.append(operation)
            return {"page": {"url": url}}

        def observe(self, sid, tid, **kwargs):
            self.calls.append(kwargs["mode"])
            query = kwargs.get("query", {})
            return {
                "_image": {"data": "discard-this-image"},
                "observation": {
                    "semantic_snapshot": "not-stored"
                    if query.get("selector") != "#description"
                    else "",
                    "query_match_count": 0 if query.get("selector") == "#description" else 1,
                },
            }

        def shutdown(self):
            self.closed = True

    report = canary.run_canary(
        "test", repeats=1, visual=True, adapter_factory=Adapter, memory_provider=lambda _: {}
    )
    assert instances[0].closed
    assert report["ok"] is False
    assert len(report["repeats"][0]["pages"]) == 4
    assert report["repeats"][0]["pages"][3]["scoped"]["ok"] is False
    assert report["repeats"][0]["pages"][3]["scoped"]["description_candidates"] == {
        "available": False,
        "reason": "PROBE_UNAVAILABLE",
    }
    assert "not-stored" not in json.dumps(report)
    assert "discard-this-image" not in json.dumps(report)
    assert set(instances[0].calls) == {"goto", "semantic", "auto", "visual", "interactive"}


def test_description_probe_is_readonly_isolated_and_saves_only_allowed_geometry():
    sentinel = "private-text-url-style-and-message-not-for-report"
    calls = []

    class Tab:
        def run_cdp(self, method, **args):
            calls.append((method, args))
            if method == "Page.getFrameTree":
                return {"frameTree": {"frame": {"id": sentinel}}}
            if method == "Page.createIsolatedWorld":
                return {"executionContextId": 123}
            assert method == "Runtime.evaluate"
            assert args["contextId"] == 123
            assert args["returnByValue"] is True
            return {
                "result": {
                    "value": {
                        "match_count": 2,
                        "truncated": False,
                        "url": sentinel,
                        "candidates": [
                            {
                                "tag": "DIV",
                                "connected": True,
                                "display_none": True,
                                "rendered": False,
                                "rect_width": 0,
                                "rect_height": 0,
                                "text": sentinel,
                            },
                            {
                                "tag": sentinel,
                                "connected": True,
                                "display_none": False,
                                "rendered": True,
                                "rect_width": 540,
                                "rect_height": 83.25,
                                "href": sentinel,
                                "password": sentinel,
                            },
                        ],
                    }
                }
            }

    adapter = SimpleNamespace(_tab=lambda *_: SimpleNamespace(tab=Tab()))
    row = canary.description_candidate_metrics(adapter, "fixed-session", "fixed-tab")
    assert row["available"] is True
    assert row["match_count"] == 2
    assert row["candidates"][0]["display_none"] is True
    assert row["candidates"][1]["tag"] == "OTHER"
    assert row["candidates"][1]["rect_height"] == 83.25
    assert sentinel not in json.dumps(row)
    assert [call[0] for call in calls] == [
        "Page.getFrameTree",
        "Page.createIsolatedWorld",
        "Runtime.evaluate",
    ]
    assert "innerText" not in calls[2][1]["expression"]
    assert "textContent" not in calls[2][1]["expression"]
    assert "getAttribute" not in calls[2][1]["expression"]


def test_description_probe_error_has_fixed_reason_without_cdp_error_content():
    def fail(*args, **kwargs):
        raise BrowserError("AUTH_REQUIRED", "private-CDP-error-content")

    row = canary.description_candidate_metrics(SimpleNamespace(_tab=fail), "sid", "tid")
    assert row["available"] is False
    assert row["reason"] == "GEOMETRY_PROBE_FAILED"
    assert "private-CDP-error-content" not in json.dumps(row)


@pytest.mark.parametrize("raises_cleanup", [False, True])
def test_locked_temporary_directory_does_not_discard_successful_page_results(
    monkeypatch, tmp_path, raises_cleanup
):
    owned = tmp_path / "owned-disposable-canary"
    owned.mkdir()
    sentinel = "sensitive-path-and-original-error-not-for-report"
    created = []
    instances = []

    class LockedTemporaryDirectory:
        def __init__(self, *, prefix, ignore_cleanup_errors):
            assert prefix == "cloud-browser-public-read-"
            assert ignore_cleanup_errors is True
            self.name = str(owned)
            self.cleanup_calls = 0
            created.append(self)

        def cleanup(self):
            self.cleanup_calls += 1
            if raises_cleanup:
                raise OSError(32, sentinel)
            # Mimic ignore_cleanup_errors retaining a locked directory.

    class Adapter:
        def __init__(self, cfg):
            self.closed = False
            instances.append(self)

        def open(self, sid):
            return {"tab_id": "test-tab"}

        def navigate(self, sid, tid, operation, url):
            return {"page": {"url": url}}

        def observe(self, sid, tid, **kwargs):
            return {
                "observation": {
                    "semantic_snapshot": "public text not serialized",
                    "query_match_count": 1,
                    "interactive_snapshot": json.dumps(
                        {"href": "https://gall.dcinside.com/board/view/?id=programming&no=2940536"}
                    ),
                }
            }

        def shutdown(self):
            self.closed = True

    monkeypatch.setattr(canary.tempfile, "TemporaryDirectory", LockedTemporaryDirectory)
    delays = []
    monkeypatch.setattr(canary.time, "sleep", delays.append)
    report = canary.run_canary(
        "test", repeats=1, adapter_factory=Adapter, memory_provider=lambda _: {}
    )
    assert instances[0].closed
    assert created[0].cleanup_calls == len(canary.CLEANUP_RETRY_DELAYS) + 1
    assert delays == list(canary.CLEANUP_RETRY_DELAYS)
    assert len(report["repeats"][0]["pages"]) == 4
    assert all(page["ok"] for page in report["repeats"][0]["pages"])
    cleanup = report["repeats"][0]["cleanup"]
    assert cleanup["browser_shutdown_complete"] is True
    assert cleanup["browser_shutdown_returned"] is True
    assert cleanup["process_exit_verified"] is None
    assert cleanup["temporary_cleanup_attempts"] == 5
    assert cleanup["temporary_cleanup_complete"] is False
    assert cleanup["cleanup_complete"] is False
    assert cleanup["warning_count"] == 1
    assert cleanup["temporary_cleanup_error"] == (
        "TEMPORARY_CLEANUP_FAILED" if raises_cleanup else "TEMPORARY_DIRECTORY_STILL_PRESENT"
    )
    assert report["ok"] is False
    assert sentinel not in json.dumps(report)
    assert str(owned) not in json.dumps(report)


@pytest.mark.parametrize("success_attempt", [2, 5])
@pytest.mark.parametrize("raises_cleanup", [False, True])
def test_temporary_lock_retry_is_bounded_and_only_retries_owned_manager(
    monkeypatch, tmp_path, success_attempt, raises_cleanup
):
    owned = tmp_path / "owned-disposable-canary"
    owned.mkdir()
    outside = tmp_path / "not-owned-by-canary"
    outside.mkdir()
    created = []
    delays = []

    class BrieflyLockedTemporaryDirectory:
        def __init__(self, *, prefix, ignore_cleanup_errors):
            assert prefix == "cloud-browser-public-read-"
            assert ignore_cleanup_errors is True
            self.name = str(owned)
            self.cleanup_calls = 0
            created.append(self)

        def cleanup(self):
            self.cleanup_calls += 1
            if self.cleanup_calls == success_attempt:
                owned.rmdir()
            elif raises_cleanup:
                raise PermissionError("private-path-and-lock-error")

    monkeypatch.setattr(canary.tempfile, "TemporaryDirectory", BrieflyLockedTemporaryDirectory)
    monkeypatch.setattr(canary.time, "sleep", delays.append)
    cleanup = {"browser_shutdown_complete": True, "process_exit_verified": None}
    with canary.disposable_directory(cleanup) as temporary:
        assert temporary == str(owned)
    assert created[0].cleanup_calls == success_attempt
    assert delays == list(canary.CLEANUP_RETRY_DELAYS[: success_attempt - 1])
    assert sum(delays) <= 3.7
    assert cleanup["temporary_cleanup_attempts"] == success_attempt
    assert cleanup["temporary_cleanup_complete"] is True
    assert cleanup["temporary_cleanup_error"] is None
    assert cleanup["cleanup_complete"] is True
    assert cleanup["ok"] is True
    assert cleanup["warning_count"] == 0
    assert cleanup["process_exit_verified"] is None
    assert outside.exists()
    assert str(owned) not in json.dumps(cleanup)
    assert "private-path-and-lock-error" not in json.dumps(cleanup)


def test_adapter_shutdown_error_remains_failure_after_successful_directory_cleanup():
    class Adapter:
        def __init__(self, cfg):
            self.cfg = cfg

        def open(self, sid):
            raise BrowserError("NAVIGATION_FAILED", "not-for-report")

        def shutdown(self):
            raise RuntimeError("private-shutdown-error")

    report = canary.run_canary(
        "not-launched", repeats=1, adapter_factory=Adapter, memory_provider=lambda _: {}
    )
    cleanup = report["repeats"][0]["cleanup"]
    assert cleanup["browser_shutdown_returned"] is False
    assert cleanup["browser_shutdown_complete"] is False
    assert cleanup["process_exit_verified"] is None
    assert cleanup["temporary_cleanup_complete"] is True
    assert cleanup["cleanup_complete"] is False
    assert cleanup["reason"] == "BROWSER_SHUTDOWN_FAILED"
    assert cleanup["warning_count"] == 1
    assert report["ok"] is False
    assert "private-shutdown-error" not in json.dumps(report)


def test_cli_writes_failed_cleanup_report_before_returning_failure(monkeypatch, tmp_path):
    output = tmp_path / "sanitized-result.json"
    report = {"ok": False, "repeats": [{"cleanup": {"cleanup_complete": False}}]}
    monkeypatch.setattr(canary, "run_canary", lambda *args, **kwargs: report)
    result = canary.main(["--chromium-path", "test-not-launched", "--output", str(output)])
    assert result == 1
    assert json.loads(output.read_text(encoding="utf-8")) == report


@pytest.mark.parametrize("repeats", [0, 4])
def test_repetition_budget_bounded(repeats):
    with pytest.raises(ValueError, match="between 1 and 3"):
        canary.run_canary("not-used", repeats=repeats)
