import importlib.util
import json
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "observation_benchmark", Path(__file__).parents[1] / "scripts/observation_benchmark.py"
)
benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)


def test_memory_accounting_never_uses_rss_for_missing_uss_or_pss():
    totals = benchmark.memory_totals(
        [
            {"rss_bytes": 100, "uss_bytes": 40, "pss_bytes": 60},
            {"rss_bytes": 100, "uss_bytes": 30, "pss_bytes": 50},
        ]
    )
    assert totals == {
        "process_count": 2,
        "rss_sum_bytes": 200,
        "uss_sum_bytes": 70,
        "pss_sum_bytes": 110,
    }
    missing = benchmark.memory_totals(
        [
            {"rss_bytes": 100, "uss_bytes": 40, "pss_bytes": 60},
            {"rss_bytes": 100, "uss_bytes": None, "pss_bytes": None},
        ]
    )
    assert missing["uss_sum_bytes"] is None and missing["pss_sum_bytes"] is None
    assert "NOT physical memory" in benchmark.MEMORY_NOTE
    assert json.loads(json.dumps(missing))["pss_sum_bytes"] is None


def test_summary_excludes_warmup_and_keeps_modes_separate():
    def record(phase, wall, measured=True):
        return {
            "phase": phase,
            "wall_seconds": wall,
            "measured": measured,
            "cdp": {"calls": 3, "snapshot_payload_bytes": 100},
            "cpu_seconds": {"harness": 0.2, "browser_child": 0.1},
        }

    result = benchmark.aggregate(
        [
            record("interactive", 99, False),
            record("interactive", 1),
            record("interactive", 3),
            record("semantic", 5),
        ]
    )
    assert result["interactive"]["count"] == 2
    assert result["interactive"]["wall_median_seconds"] == 2
    assert result["interactive"]["wall_total_seconds"] == 4
    assert result["interactive"]["cdp_calls"] == 6
    assert result["interactive"]["snapshot_payload_bytes"] == 200
    assert result["interactive"]["harness_cpu_seconds"] == 0.4
    assert result["semantic"]["wall_total_seconds"] == 5
    assert json.loads(json.dumps(result)) == result


def test_cdp_delta_counts_only_calls_since_boundary():
    before = {
        "calls": 10,
        "snapshot_count": 2,
        "snapshot_payload_bytes": 700,
        "instrumentation_seconds": 0.1,
        "methods": {"Runtime.evaluate": 10},
    }
    after = {
        "calls": 13,
        "snapshot_count": 3,
        "snapshot_payload_bytes": 1100,
        "instrumentation_seconds": 0.2,
        "methods": {"Runtime.evaluate": 12, "DOM.describeNode": 1},
    }
    assert benchmark.meter_delta(before, after) == {
        "calls": 3,
        "snapshot_count": 1,
        "snapshot_payload_bytes": 400,
        "instrumentation_seconds": 0.1,
        "methods": {"Runtime.evaluate": 2, "DOM.describeNode": 1},
    }


def test_fixture_is_deterministic_bounded_and_local_only():
    pages = benchmark.fixture_pages()
    assert pages == benchmark.fixture_pages()
    assert set(pages) == {"/fixture.html", "/frame.html"}
    assert pages["/fixture.html"].count(b"<button") == 48
    assert pages["/fixture.html"].count(b"<p>") == 1200
    assert b'src="/frame.html"' in pages["/fixture.html"]
    assert b"https://" not in b"".join(pages.values())


def test_cdp_meter_preserves_result_and_counts_snapshot_bytes(monkeypatch):
    from DrissionPage._base.driver import Driver

    value = {"nodes": [{"name": "Fixture"}], "semantic_text": "Synthetic prose"}
    expected = {"result": {"result": {"value": value}}}
    messages = []

    def send(driver, message, **kwargs):
        messages.append(message)
        return expected

    monkeypatch.setattr(Driver, "_send", send)
    message = {"method": "Runtime.callFunctionOn", "params": {"returnByValue": True}}
    with benchmark.CDPMeter() as meter:
        returned = Driver._send(object(), message)
        measured = meter.snapshot()
    assert returned is expected
    assert messages == [message]
    assert measured["calls"] == measured["snapshot_count"] == 1
    assert measured["snapshot_payload_bytes"] == len(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    )
    assert Driver._send is send


def test_memory_peak_is_peak_of_simultaneous_samples_not_sum_of_process_peaks():
    sampler = benchmark.ProcessSampler()
    sampler.samples = [
        {"rss_sum_bytes": 200, "uss_sum_bytes": 90, "pss_sum_bytes": None},
        {"rss_sum_bytes": 180, "uss_sum_bytes": 100, "pss_sum_bytes": None},
    ]
    result = sampler.report()
    assert result["rss_sum_peak_bytes"] == 200
    assert result["uss_sum_peak_bytes"] == 100
    assert result["pss_sum_peak_bytes"] is None


def test_disabled_cdp_meter_never_wraps_driver_or_serializes_payload(monkeypatch):
    from DrissionPage._base.driver import Driver

    response = {"result": {"result": {"value": {"nodes": [], "semantic_text": "Fixture"}}}}

    def send(driver, message):
        return response

    def reject_serialization(*args, **kwargs):
        raise AssertionError("Disabled CDP meter must not serialize payloads")

    monkeypatch.setattr(Driver, "_send", send)
    monkeypatch.setattr(benchmark.json, "dumps", reject_serialization)
    with benchmark.CDPMeter(enabled=False) as meter:
        assert not meter.enabled
        assert Driver._send is send
        assert Driver._send(object(), {"method": "Runtime.callFunctionOn"}) is response
        assert meter.patcher is None
        assert meter.snapshot() == {
            "calls": 0,
            "methods": {},
            "snapshot_count": 0,
            "snapshot_payload_bytes": 0,
            "instrumentation_seconds": 0.0,
        }
    assert Driver._send is send
